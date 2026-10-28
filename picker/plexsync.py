"""Building the playlist as files land: an M3U8 file and a Plex playlist kept in step (spec section 8).

The playlist's tracks are looked up in the library index (by recording ID, else by name),
so a track counts the moment its album is imported and tagged. The M3U8 under playlist_dir
is rewritten in full in the CSV's order, with commented lines holding the place of tracks
not yet obtained. The Plex playlist the picker created (its rating key is remembered) is
brought to the same list: items added, items for tracks that left the playlist removed,
order fixed by moving items. Plex cannot be asked for a MusicBrainz ID, so every track is
found by its file path, translated with plex_music_path.

Plex learns about new files only when it scans, and the scan the import triggered runs in the
background, so a sync that cannot find a file yet asks for itself again a minute later, up to
ten times. The same sync runs after each import of a run, when a run starts (for the tracks
already owned), nightly, and from the Sync to Plex button; it is idempotent.
"""

from __future__ import annotations

import asyncio
import logging
import os
import posixpath
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from sqlmodel import col, select

from . import library
from .clients import services
from .clients.http import ServiceError
from .config import Settings, get_settings
from .db import session
from .jobs import handler, log_event, worker
from .models import Import, Job, Playlist, PlaylistTrack, QueueItem, utcnow
from .playlists import first_artist, mark_owned

log = logging.getLogger(__name__)

SYNC_DEBOUNCE = timedelta(seconds=60)
RESCAN_WAIT = timedelta(seconds=60)
RESCAN_RETRIES = 10  # about ten minutes for Plex to see a new album
NIGHTLY_HOUR = 4


def safe_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", name).strip(" .")
    return re.sub(r"\s+", " ", cleaned) or "Playlist"


def m3u_path(playlist: Playlist, settings: Settings) -> Path:
    return settings.playlist_dir / f"{safe_name(playlist.name)}.m3u8"


def plex_file_path(path: str, settings: Settings) -> str:
    """The file's path as Plex sees it (plex_music_path replaces music_dir)."""
    try:
        rel = Path(path).resolve().relative_to(settings.music_dir.resolve()).as_posix()
    except ValueError:
        return Path(path).as_posix()
    root = settings.plex_music_path or settings.music_dir.as_posix()
    return posixpath.join(root, rel)


# What the playlist holds now --------------------------------------------------------------------

def _refresh_index_for_done_items(tracks: list[PlaylistTrack], settings: Settings) -> None:
    """An import that just finished may not be in the index yet: scan its folder first."""
    item_ids = {t.queue_item_id for t in tracks if t.queue_item_id and not t.owned_path}
    if not item_ids:
        return
    with session() as s:
        items = s.exec(select(QueueItem).where(col(QueueItem.id).in_(item_ids), QueueItem.state == "done")).all()
        import_ids = [i.import_id for i in items if i.import_id]
        folders = {imp.album_dir for imp in s.exec(select(Import).where(col(Import.id).in_(import_ids))).all()
                   if imp.album_dir} if import_ids else set()
    for folder in folders:
        if Path(folder).is_dir():
            library.scan(Path(folder), settings)


def current_tracks(playlist_id: int, settings: Settings) -> list[PlaylistTrack]:
    """The playlist's rows in order, each with the library file that is the track when there is one."""
    with session() as s:
        tracks = s.exec(select(PlaylistTrack).where(PlaylistTrack.playlist_id == playlist_id, PlaylistTrack.dropped == False)  # noqa: E712
                        .order_by(PlaylistTrack.position)).all()
    _refresh_index_for_done_items(tracks, settings)
    mark_owned(tracks)
    with session() as s:
        for track in tracks:
            s.add(track)
        s.commit()
    return tracks


def write_m3u(playlist: Playlist, tracks: list[PlaylistTrack], settings: Settings) -> Path:
    path = m3u_path(playlist, settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["#EXTM3U", f"#PLAYLIST:{playlist.name}"]
    for track in tracks:
        label = f"{first_artist(track.artist)} - {track.title}"
        if track.owned_path:
            seconds = int((track.duration_ms or 0) / 1000) or -1
            rel = os.path.relpath(track.owned_path, path.parent).replace(os.sep, "/")
            lines += [f"#EXTINF:{seconds},{label}", rel]
        else:
            lines.append(f"# {track.position}. {label} (not in the library yet)")
    tmp = path.with_suffix(".m3u8.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


# Plex ----------------------------------------------------------------------------------------------

@dataclass
class PlexResult:
    state: str = "off"  # off | ok | partial | error | empty
    added: int = 0
    removed: int = 0
    moved: int = 0
    missing: list[str] = field(default_factory=list)  # tracks on disk Plex does not know yet
    error: str | None = None
    created: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _part_files(track: dict) -> set[str]:
    files = set()
    for media in track.get("Media") or []:
        for part in media.get("Part") or []:
            if part.get("file"):
                files.add(part["file"])
    return files


async def find_track(plex, plex_path: str, title: str, album: str, cache: dict[str, str | None]) -> str | None:
    """The Plex rating key of the track at this path: by title first, then through its album."""
    if plex_path in cache:
        return cache[plex_path]
    key = None
    try:
        for track in await plex.tracks_titled(title):
            if plex_path in _part_files(track):
                key = str(track["ratingKey"])
                break
        if key is None and album:
            for found in await plex.albums_titled(album):
                for track in await plex.children(str(found["ratingKey"])):
                    files = _part_files(track)
                    cache.setdefault(next(iter(files), ""), str(track["ratingKey"]))
                    if plex_path in files:
                        key = str(track["ratingKey"])
                if key:
                    break
    except ServiceError as exc:
        log.warning("Plex lookup for %s: %s", plex_path, exc.message)
    cache[plex_path] = key
    return key


async def sync_plex(playlist: Playlist, tracks: list[PlaylistTrack], settings: Settings) -> PlexResult:
    result = PlexResult()
    if not (settings.plex_url and settings.plex_token and settings.plex_section_id):
        return result
    plex = services().plex
    cache: dict[str, str | None] = {}
    desired: list[tuple[PlaylistTrack, str]] = []
    for track in tracks:
        if not track.owned_path:
            continue
        key = await find_track(plex, plex_file_path(track.owned_path, settings), track.title, track.album, cache)
        if key:
            desired.append((track, key))
        else:
            result.missing.append(f"{first_artist(track.artist)} - {track.title}")
    try:
        if not desired:
            result.state = "empty" if not result.missing else "partial"
            return result
        existing = None
        if playlist.plex_rating_key:
            existing = next((p for p in await plex.playlists() if str(p.get("ratingKey")) == str(playlist.plex_rating_key)), None)
        if existing is None:
            created = await plex.create_playlist(playlist.name, [k for _, k in desired])
            playlist.plex_rating_key = str(created["ratingKey"])
            result.created, result.added = True, len(desired)
            items = await plex.playlist_items(playlist.plex_rating_key)
        else:
            items = await plex.playlist_items(playlist.plex_rating_key)
            wanted = {}
            for _, k in desired:
                wanted[k] = wanted.get(k, 0) + 1
            keep = []
            for item in items:
                k = str(item.get("ratingKey"))
                if wanted.get(k, 0) > 0:
                    wanted[k] -= 1
                    keep.append(item)
                else:
                    await plex.remove_item(playlist.plex_rating_key, str(item["playlistItemID"]))
                    result.removed += 1
            to_add = [k for k, n in wanted.items() for _ in range(n)]
            if to_add:
                await plex.add_items(playlist.plex_rating_key, to_add)
                result.added = len(to_add)
            items = await plex.playlist_items(playlist.plex_rating_key) if (to_add or result.removed) else keep
        # Order: walk the wanted order, moving each item into place behind the previous one.
        order = [str(i.get("ratingKey")) for i in items]
        slots = {str(i.get("ratingKey")): [str(x["playlistItemID"]) for x in items if str(x.get("ratingKey")) == str(i.get("ratingKey"))]
                 for i in items}
        placed: list[str] = []
        pool = {k: list(v) for k, v in slots.items()}
        current = [str(i["playlistItemID"]) for i in items]
        for track, key in desired:
            if not pool.get(key):
                continue
            item_id = pool[key].pop(0)
            track.plex_item_id = item_id
            position = len(placed)
            if current.index(item_id) != position:
                await plex.move_item(playlist.plex_rating_key, item_id, placed[-1] if placed else None)
                current.remove(item_id)
                current.insert(position, item_id)
                result.moved += 1
            placed.append(item_id)
        del order
        result.state = "partial" if result.missing else "ok"
    except ServiceError as exc:
        result.state, result.error = "error", exc.message
    return result


# The job -----------------------------------------------------------------------------------------

@dataclass
class SyncResult:
    m3u: str
    tracks: int
    have: int
    plex: PlexResult


async def sync(playlist_id: int, settings: Settings | None = None) -> SyncResult | None:
    settings = settings or get_settings()
    with session() as s:
        playlist = s.get(Playlist, playlist_id)
    if playlist is None:
        return None
    tracks = current_tracks(playlist_id, settings)
    path = write_m3u(playlist, tracks, settings)
    plex = await sync_plex(playlist, tracks, settings)
    have = sum(1 for t in tracks if t.owned_path)
    note = f"{have} of {len(tracks)} tracks in the library; M3U8 written"
    if plex.state == "off":
        note += "; Plex not configured"
    elif plex.state == "error":
        note += f"; Plex: {plex.error}"
    elif plex.state == "empty":
        note += "; nothing in Plex yet"
    else:
        bits = [f"{plex.added} added"] if plex.added else []
        bits += [f"{plex.removed} removed"] if plex.removed else []
        bits += [f"{plex.moved} moved"] if plex.moved else []
        note += "; Plex playlist " + ("created" if plex.created else "in step") + (f" ({', '.join(bits)})" if bits else "")
        if plex.missing:
            note += f"; {len(plex.missing)} not in Plex yet"
    with session() as s:
        row = s.get(Playlist, playlist_id)
        row.m3u_path, row.plex_rating_key, row.last_sync, row.sync_note = str(path), playlist.plex_rating_key, utcnow(), note
        s.add(row)
        for track in tracks:
            s.add(track)
        s.commit()
    return SyncResult(m3u=str(path), tracks=len(tracks), have=have, plex=plex)


def queue_sync(playlist_id: int, delay: timedelta = SYNC_DEBOUNCE, retries: int = 0) -> Job | None:
    """One sync per playlist at a time; a second request within the delay just pushes it back."""
    run_after = utcnow() + delay if delay else None
    with session() as s:
        for job in s.exec(select(Job).where(Job.kind == "plexsync", Job.status == "queued")):
            if job.payload.get("playlist_id") == playlist_id:
                job.run_after = run_after if run_after and (job.run_after is None or run_after > job.run_after) else job.run_after
                job.payload = {**job.payload, "retries": retries}
                s.add(job)
                s.commit()
                return None
    return worker.enqueue("plexsync", {"playlist_id": playlist_id, "retries": retries}, run_after=run_after)


@handler("plexsync")
async def run_sync(job: Job) -> None:
    playlist_id = job.payload["playlist_id"]
    result = await sync(playlist_id, get_settings())
    if result is None:
        return
    log_event(job.id, None, f"Playlist sync: {result.have} of {result.tracks} tracks; Plex {result.plex.state}"
                            + (f", {len(result.plex.missing)} not in Plex yet" if result.plex.missing else ""))
    retries = int(job.payload.get("retries") or 0)
    if result.plex.missing and result.plex.state != "error" and retries < RESCAN_RETRIES:
        queue_sync(playlist_id, RESCAN_WAIT, retries + 1)  # Plex may still be scanning the new folder


def _seconds_until(hour: int) -> float:
    now = datetime.now()
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def scheduler() -> None:
    """Nightly: every playlist that has run is brought back in step (files moved, Plex rescanned)."""
    while True:
        await asyncio.sleep(_seconds_until(NIGHTLY_HOUR))
        try:
            with session() as s:
                ids = s.exec(select(Playlist.id).where(col(Playlist.state).in_(("running", "paused", "done")))).all()
            for playlist_id in ids:
                queue_sync(playlist_id, timedelta(0))
        except Exception:  # noqa: BLE001 - keep the scheduler alive
            log.exception("could not queue the nightly playlist sync")
