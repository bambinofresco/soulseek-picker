"""Playlists: a CSV of songs in, albums to obtain out (playlist-import-spec.md, sections 2 to 5).

The user exports a Spotify playlist to CSV (Exportify or the like; the picker never talks to
Spotify) and uploads it. A `resolve` job turns each row into a MusicBrainz recording, first by
ISRC (25 per request), then by title, artist and duration, and from the recording's releases
works out which album to get: the album the CSV names when it is an album or EP, otherwise the
earliest studio album holding the recording, otherwise whatever the CSV named. Singles are
kept aside so the row can switch between "the single" and "the album". The review page then
groups the rows by album and the user decides per album: the whole album, only these tracks,
an upgrade of the copy already held, or skip.

Nothing here downloads. Starting the run is the queue's job (section 6).
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from datetime import timedelta

from sqlmodel import select

from . import library, owned
from .clients import services
from .config import Settings, get_settings
from .db import session
from .jobs import handler, log_event, worker
from .models import Album, Job, Playlist, PlaylistAlbum, PlaylistTrack
from .queries import normalize

log = logging.getLogger(__name__)

ISRC_BATCH = 25
TRACK_MB = 30  # a FLAC CD track, for the size estimate
TEXT_STRONG, TEXT_WEAK = 90, 70  # MusicBrainz search scores
DURATION_SLACK_MS = 3000

# CSV ---------------------------------------------------------------------------------------

COLUMNS = {
    "title": ("track name", "name", "title", "track", "song"),
    "artist": ("artist name(s)", "artist names", "artist name", "artist", "artists"),
    "album_artist": ("album artist name(s)", "album artist name", "album artist", "albumartist"),
    "album": ("album name", "album"),
    "isrc": ("isrc",),
    "duration_ms": ("duration (ms)", "track duration (ms)", "duration_ms", "duration"),
    "uri": ("track uri", "spotify uri", "uri", "spotify track id", "track id", "spotify id"),
    "release_date": ("album release date", "release date"),
}
REQUIRED = ("title", "artist", "album")


def _header_map(fields: list[str]) -> dict[str, str]:
    lowered = {(f or "").strip().lower().lstrip("﻿"): f for f in fields}
    found = {}
    for column, names in COLUMNS.items():
        for name in names:
            if name in lowered:
                found[column] = lowered[name]
                break
    return found


def _duration_ms(text: str | None) -> int | None:
    text = (text or "").strip()
    if not text:
        return None
    if ":" in text:  # m:ss or h:mm:ss
        parts = text.split(":")
        try:
            seconds = sum(float(p) * 60**i for i, p in enumerate(reversed(parts)))
        except ValueError:
            return None
        return int(seconds * 1000)
    try:
        value = float(text)
    except ValueError:
        return None
    return int(value * 1000) if value < 3600 else int(value)  # seconds when it is too small to be ms


def parse_csv(text: str) -> list[dict]:
    """Rows as {title, artist, album_artist, album, isrc, duration_ms, uri, release_date, position}.

    Columns are found by name, Exportify's first; title, artist and album are required.
    """
    text = text.lstrip("﻿")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        raise ValueError("the file has no header row")
    header = _header_map(reader.fieldnames)
    missing = [c for c in REQUIRED if c not in header]
    if missing:
        raise ValueError(f"the CSV has no column for {', '.join(missing)} (found: {', '.join(reader.fieldnames)})")
    rows = []
    for raw in reader:
        def get(column: str) -> str | None:
            name = header.get(column)
            value = (raw.get(name) or "").strip() if name else ""
            return value or None

        title = get("title")
        if not title:
            continue
        isrc = (get("isrc") or "").replace("-", "").upper() or None
        rows.append({
            "position": len(rows) + 1, "title": title, "artist": get("artist") or "", "album_artist": get("album_artist"),
            "album": get("album") or "", "isrc": isrc if isrc and re.fullmatch(r"[A-Z0-9]{12}", isrc) else None,
            "duration_ms": _duration_ms(get("duration_ms")), "uri": get("uri"), "release_date": get("release_date"),
        })
    if not rows:
        raise ValueError("the CSV has no rows with a title")
    return rows


def create_playlist(name: str, text: str) -> Playlist:
    rows = parse_csv(text)
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]
    with session() as s:
        playlist = Playlist(name=name.strip() or "Playlist", csv_hash=digest)
        s.add(playlist)
        s.commit()
        s.refresh(playlist)
        for row in rows:
            s.add(PlaylistTrack(playlist_id=playlist.id, **row))
        s.commit()
    queue_resolve(playlist.id)
    return playlist


def first_artist(text: str) -> str:
    """Exportify lists every credited artist, comma-separated; the first is the main one."""
    return (text or "").split(", ")[0].strip()


def find_by_name(name: str) -> Playlist | None:
    want = (name or "").strip().lower()
    with session() as s:
        for playlist in s.exec(select(Playlist).order_by(Playlist.id.desc())):
            if playlist.name.strip().lower() == want:
                return playlist
    return None


def _row_keys(row: dict) -> list[tuple]:
    keys = []
    if row.get("uri"):
        keys.append(("uri", row["uri"]))
    if row.get("isrc"):
        keys.append(("isrc", row["isrc"]))
    keys.append(("name", normalize(row.get("title") or ""), normalize(first_artist(row.get("artist") or "")),
                 normalize(row.get("album") or "")))
    return keys


def update_playlist(playlist_id: int, text: str) -> dict:
    """A new export of the same playlist: rows are matched to the existing ones (by Spotify URI, then
    ISRC, then title, artist and album), kept with everything already decided about them, new rows
    are added and resolved, rows that left the CSV are removed. Albums' choices persist; a playlist
    that has run keeps running, and its M3U8 and Plex playlist are brought in step."""
    rows = parse_csv(text)
    with session() as s:
        existing = s.exec(select(PlaylistTrack).where(PlaylistTrack.playlist_id == playlist_id)).all()
    pools: dict[tuple, PlaylistTrack] = {}
    for track in existing:
        for key in _row_keys({"uri": track.uri, "isrc": track.isrc, "title": track.title, "artist": track.artist, "album": track.album}):
            pools.setdefault(key, track)
    kept, added, matched_ids = 0, 0, set()
    with session() as s:
        for row in rows:
            track = next((pools[k] for k in _row_keys(row) if k in pools and pools[k].id not in matched_ids), None)
            if track is not None:
                matched_ids.add(track.id)
                track.position = row["position"]
                track.uri = track.uri or row.get("uri")
                track.isrc = track.isrc or row.get("isrc")
                track.duration_ms = track.duration_ms or row.get("duration_ms")
                s.add(track)
                kept += 1
            else:
                s.add(PlaylistTrack(playlist_id=playlist_id, **row))
                added += 1
        removed = 0
        for track in existing:
            if track.id not in matched_ids:
                s.delete(track)
                removed += 1
        playlist = s.get(Playlist, playlist_id)
        playlist.csv_hash = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]
        s.add(playlist)
        s.commit()
        has_run = playlist.state in ("running", "paused", "done")
    if added:
        queue_resolve(playlist_id)
    if has_run and removed:
        from . import plexsync  # imported here: plexsync imports this module

        plexsync.queue_sync(playlist_id, timedelta(0))
    return {"kept": kept, "added": added, "removed": removed}


# Albums a recording is on --------------------------------------------------------------------

KIND_ORDER = {"album": 0, "ep": 1, "single": 2, "soundtrack": 3, "live": 4, "compilation": 5, "remix": 6, "other": 7}


@dataclass
class AlbumOption:
    rg_mbid: str
    title: str
    artist: str
    artist_mbid: str | None
    kind: str  # album | ep | single | compilation | live | soundtrack | remix | other
    primary_type: str | None
    secondary_types: list[str] = field(default_factory=list)
    date: str | None = None  # earliest release date seen
    track_count: int | None = None  # smallest release
    release_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def label(self) -> str:
        bits = [self.title]
        if self.date:
            bits.append(self.date[:4])
        kind = self.kind if self.kind != "album" else "Album"
        return f"{bits[0]} ({', '.join(bits[1:] + [kind])})"


def classify(primary: str | None, secondary: list[str]) -> str:
    secondary = [s.lower() for s in secondary or []]
    for name in ("compilation", "live", "soundtrack", "remix"):
        if name in secondary:
            return name
    if secondary:
        return "other"
    primary = (primary or "").lower()
    if primary == "album":
        return "album"
    if primary in ("single", "ep"):
        return primary
    return "other"


def _credit(entity: dict) -> tuple[str, str | None]:
    credits = entity.get("artist-credit") or []
    name = "".join((c.get("name") or "") + (c.get("joinphrase") or "") for c in credits)
    mbid = (credits[0].get("artist") or {}).get("id") if credits else None
    return name, mbid


def options_from_recording(recording: dict) -> list[AlbumOption]:
    """One option per release group the recording is on, earliest and most album-like first."""
    found: dict[str, AlbumOption] = {}
    for release in recording.get("releases") or []:
        rg = release.get("release-group") or {}
        rg_id = rg.get("id")
        if not rg_id:
            continue
        option = found.get(rg_id)
        if option is None:
            name, mbid = _credit(release)
            if not name:
                name, mbid = _credit(recording)
            option = AlbumOption(rg_mbid=rg_id, title=rg.get("title") or release.get("title") or "", artist=name,
                                 artist_mbid=mbid, kind=classify(rg.get("primary-type"), rg.get("secondary-types") or []),
                                 primary_type=rg.get("primary-type"), secondary_types=list(rg.get("secondary-types") or []))
            found[rg_id] = option
        option.release_count += 1
        date = release.get("date")
        if date and (option.date is None or date < option.date):
            option.date = date
        count = release.get("track-count")
        if count and (option.track_count is None or count < option.track_count):
            option.track_count = count
    return sorted(found.values(), key=lambda o: (o.date or "9999", KIND_ORDER.get(o.kind, 9), o.title))


def _earliest(options: list[AlbumOption]) -> AlbumOption | None:
    return min(options, key=lambda o: (o.date or "9999", KIND_ORDER.get(o.kind, 9))) if options else None


def choose_album(options: list[AlbumOption], csv_album: str, prefer_studio: bool) -> AlbumOption | None:
    """Section 3.3: the CSV's album when it is an album or EP; else the earliest studio album
    (when preferred); else the CSV's album whatever it is; else the earliest album-like thing."""
    if not options:
        return None
    name = normalize(csv_album or "")
    named = [o for o in options if normalize(o.title) == name] if name else []
    named_albums = [o for o in named if o.kind in ("album", "ep")]
    if named_albums:
        return _earliest(named_albums)
    studio = [o for o in options if o.kind == "album"]
    if prefer_studio and studio:
        return _earliest(studio)
    if named:
        return _earliest(named)
    if studio:
        return _earliest(studio)
    non_single = [o for o in options if o.kind not in ("single",)]
    return _earliest(non_single) or _earliest(options)


def single_for(options: list[AlbumOption], title: str) -> AlbumOption | None:
    """The single or EP that holds the recording: one named after the track if there is one, else the earliest."""
    singles = [o for o in options if o.kind in ("single", "ep")]
    if not singles:
        return None
    want = normalize(title or "")
    named = [o for o in singles if want and (normalize(o.title) == want or normalize(o.title).startswith(want + " "))]
    return _earliest(named) or _earliest(singles)


def rank_text_hits(recordings: list[dict], artist: str, title: str, duration_ms: int | None) -> tuple[dict | None, str]:
    """The best recording for a row without an ISRC hit, and how sure that is."""
    want_artist = normalize(first_artist(artist))
    ranked = sorted(recordings, key=lambda r: (-(r.get("score") or 0), -len(r.get("releases") or [])))
    fallback = None
    for rec in ranked:
        score = rec.get("score") or 0
        if score < TEXT_WEAK or not rec.get("releases"):
            continue
        credits = {normalize(c.get("name") or "") for c in rec.get("artist-credit") or []}
        credits.add(normalize(_credit(rec)[0]))
        artist_ok = not want_artist or want_artist in credits
        length = rec.get("length")
        dur_ok = not duration_ms or not length or abs(int(length) - duration_ms) <= DURATION_SLACK_MS
        if score >= TEXT_STRONG and artist_ok and dur_ok:
            return rec, "text-strong"
        if fallback is None and (artist_ok or dur_ok):
            fallback = rec
    return (fallback, "text-weak") if fallback else (None, "none")


def ensure_album_row(option: AlbumOption) -> None:
    """An Album row from search data, without a MusicBrainz round trip per album."""
    with session() as s:
        album = s.get(Album, option.rg_mbid)
        if album is None:
            s.add(Album(rg_mbid=option.rg_mbid, artist_mbid=option.artist_mbid, artist=option.artist, title=option.title,
                        first_release_date=option.date, primary_type=option.primary_type, min_track_count=option.track_count))
        elif album.min_track_count is None and option.track_count:
            album.min_track_count = option.track_count
            s.add(album)
        s.commit()


def apply_recording(track: PlaylistTrack, recording: dict, resolution: str, playlist: Playlist) -> None:
    options = options_from_recording(recording)
    track.recording_mbid = recording.get("id")
    track.recording_title = recording.get("title")
    track.recording_length = recording.get("length")
    track.options = [o.to_dict() for o in options]
    track.resolution = resolution
    choose_for(track, playlist, options)


def choose_for(track: PlaylistTrack, playlist: Playlist, options: list[AlbumOption] | None = None) -> None:
    options = options if options is not None else [AlbumOption(**o) for o in track.options or []]
    chosen = choose_album(options, track.album, playlist.prefer_studio_album)
    track.rg_mbid = chosen.rg_mbid if chosen else None
    single = single_for(options, track.title)
    track.single_rg_mbid = single.rg_mbid if single and chosen and single.rg_mbid != chosen.rg_mbid else None
    for option in (chosen, single):
        if option:
            ensure_album_row(option)


def rechoose(playlist_id: int) -> None:
    """After the studio-album toggle changes: pick again for every row the user did not fix by hand."""
    with session() as s:
        playlist = s.get(Playlist, playlist_id)
        tracks = s.exec(select(PlaylistTrack).where(PlaylistTrack.playlist_id == playlist_id)).all()
        for track in tracks:
            if track.options and track.resolution != "manual":
                choose_for(track, playlist)
                s.add(track)
        s.commit()


# Owned --------------------------------------------------------------------------------------

def mark_owned(tracks: list[PlaylistTrack]) -> None:
    """Which rows the library already has: by recording ID, else by artist, title and duration."""
    by_id = library.owned_recordings([t.recording_mbid for t in tracks if t.recording_mbid])
    index = library.name_index()
    for track in tracks:
        hit = by_id.get(track.recording_mbid) if track.recording_mbid else None
        if hit:
            track.owned_path, track.owned_how = hit.path, "recording"
            continue
        hit = None
        if not track.owned_unticked:
            duration = track.duration_ms / 1000 if track.duration_ms else None
            hit = library.owned_by_name(first_artist(track.artist), track.title, duration, index)
        track.owned_path, track.owned_how = (hit.path, "name") if hit else (None, None)


# The job ------------------------------------------------------------------------------------

def queue_resolve(playlist_id: int) -> Job | None:
    with session() as s:
        pending = s.exec(select(Job).where(Job.kind == "resolve", Job.status.in_(["queued", "running"]))).all()
        if any(j.payload.get("playlist_id") == playlist_id for j in pending):
            return None
        playlist = s.get(Playlist, playlist_id)
        if playlist.state in ("resolving", "review"):
            playlist.state = "resolving"  # a playlist that has run keeps its run state; its new rows resolve alongside
        playlist.error = None
        s.add(playlist)
        s.commit()
    return worker.enqueue("resolve", {"playlist_id": playlist_id})


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _save_tracks(tracks: list[PlaylistTrack], playlist_id: int, resolved: int | None = None) -> None:
    with session() as s:
        for track in tracks:
            s.add(track)
        if resolved is not None:
            playlist = s.get(Playlist, playlist_id)
            playlist.resolved = resolved
            s.add(playlist)
        s.commit()


@handler("resolve")
async def run_resolve(job: Job) -> None:
    playlist_id = job.payload["playlist_id"]
    with session() as s:
        playlist = s.get(Playlist, playlist_id)
        if playlist is None:
            return
        tracks = s.exec(select(PlaylistTrack).where(PlaylistTrack.playlist_id == playlist_id)
                        .order_by(PlaylistTrack.position)).all()
    todo = [t for t in tracks if not t.dropped and t.resolution is None]
    done = len(tracks) - len(todo)
    mb = services().mb
    log_event(job.id, None, f"Resolving {len(todo)} rows of playlist “{playlist.name}”")

    with_isrc = [t for t in todo if t.isrc]
    for chunk in _chunks(with_isrc, ISRC_BATCH):
        recordings = await mb.recordings_by_isrcs([t.isrc for t in chunk])
        by_isrc: dict[str, list[dict]] = {}
        for rec in recordings:
            for isrc in rec.get("isrcs") or []:
                by_isrc.setdefault(isrc.upper(), []).append(rec)
        for track in chunk:
            hits = by_isrc.get(track.isrc)
            if hits:
                best = max(hits, key=lambda r: len(r.get("releases") or []))
                apply_recording(track, best, "isrc", playlist)
                done += 1
        _save_tracks(chunk, playlist_id, done)

    for track in todo:
        if track.resolution is not None:
            continue
        recordings = await mb.search_recordings(first_artist(track.artist), track.title, track.duration_ms)
        rec, how = rank_text_hits(recordings, track.artist, track.title, track.duration_ms)
        if rec:
            apply_recording(track, rec, how, playlist)
        else:
            track.resolution = "none"
        done += 1
        _save_tracks([track], playlist_id, done)

    mark_owned(todo)
    _save_tracks(todo, playlist_id, done)
    with session() as s:
        playlist = s.get(Playlist, playlist_id)
        if playlist.state == "resolving":
            playlist.state = "review"
        s.add(playlist)
        s.commit()
    resolved = sum(1 for t in todo if t.resolution not in (None, "none"))
    log_event(job.id, None, f"Playlist “{playlist.name}”: {resolved} of {len(todo)} rows resolved")


# The review page --------------------------------------------------------------------------

def effective_rg(track: PlaylistTrack, playlist: Playlist) -> str | None:
    if track.dropped:
        return None
    if track.single_rg_mbid and (track.use_single is True or (track.use_single is None and not playlist.full_albums)):
        return track.single_rg_mbid
    return track.rg_mbid


@dataclass
class Group:
    rg_mbid: str
    album: Album | None
    tracks: list[PlaylistTrack]
    choice: PlaylistAlbum
    owned: owned.Owned | None = None
    mode: str = "full"  # effective: full | tracks | tracks_plus | upgrade | skip
    is_single: bool = False
    partial_copy: bool = False  # the library holds some of this album and the playlist wants more of it
    estimate_mb: int = 0

    @property
    def owned_tracks(self) -> int:
        return sum(1 for t in self.tracks if t.owned_path)

    @property
    def wanted_tracks(self) -> int:
        return sum(1 for t in self.tracks if not t.owned_path)


@dataclass
class Review:
    playlist: Playlist
    tracks: list[PlaylistTrack]
    groups: list[Group]
    attention: list[PlaylistTrack]
    counts: dict
    total_mb: int
    resolving: bool = False  # rows still to be matched (the page polls while so)
    new_groups: list[Group] = field(default_factory=list)  # albums with no queue item yet, on a playlist that has run


def default_mode(playlist: Playlist, is_single: bool, album_owned: bool, all_tracks_owned: bool,
                 partial_copy: bool = False) -> str:
    if partial_copy:
        return "upgrade"  # the library holds part of the album and the playlist wants more: replace it whole
    if album_owned or all_tracks_owned:
        return "skip"
    if is_single or playlist.full_albums:
        return "full"
    return "tracks"


async def review(playlist_id: int, settings: Settings | None = None) -> Review | None:
    settings = settings or get_settings()
    with session() as s:
        playlist = s.get(Playlist, playlist_id)
        if playlist is None:
            return None
        tracks = s.exec(select(PlaylistTrack).where(PlaylistTrack.playlist_id == playlist_id)
                        .order_by(PlaylistTrack.position)).all()
        choices = {c.rg_mbid: c for c in s.exec(select(PlaylistAlbum).where(PlaylistAlbum.playlist_id == playlist_id))}
    grouped: OrderedDict[str, list[PlaylistTrack]] = OrderedDict()
    attention = []
    for track in tracks:
        if track.dropped:
            continue
        rg = effective_rg(track, playlist)
        if rg:
            grouped.setdefault(rg, []).append(track)
        if track.resolution in (None, "none", "text-weak") or not rg:
            attention.append(track)
    with session() as s:
        albums = {a.rg_mbid: a for a in s.exec(select(Album).where(Album.rg_mbid.in_(list(grouped))))} if grouped else {}
        for rg in grouped:
            if rg not in choices:
                choice = PlaylistAlbum(playlist_id=playlist_id, rg_mbid=rg)
                s.add(choice)
                choices[rg] = choice
        s.commit()
    have = await owned.owned_release_groups(list(grouped), settings) if grouped else {}
    groups, counts, total = [], {"full": 0, "tracks": 0, "tracks_plus": 0, "upgrade": 0, "skip": 0, "owned": 0}, 0
    for rg, members in grouped.items():
        album = albums.get(rg)
        is_single = bool(album and (album.primary_type or "").lower() in ("single", "ep"))
        album_owned = rg in have
        all_owned = all(t.owned_path for t in members)
        group = Group(rg_mbid=rg, album=album, tracks=members, choice=choices[rg], owned=have.get(rg), is_single=is_single)
        held = have.get(rg)
        expected = album.min_track_count if album and album.min_track_count else None
        group.partial_copy = bool(held and not all_owned and expected and held.have is not None and held.have < expected)
        group.mode = choices[rg].mode or default_mode(playlist, is_single, album_owned, all_owned, group.partial_copy)
        if group.mode in ("full", "upgrade"):
            group.estimate_mb = ((album.min_track_count if album and album.min_track_count else len(members)) or 0) * TRACK_MB
        elif group.mode == "tracks":
            group.estimate_mb = group.wanted_tracks * TRACK_MB
        elif group.mode == "tracks_plus":
            group.estimate_mb = (group.wanted_tracks + (held.have or 0 if held else 0)) * TRACK_MB
        total += group.estimate_mb
        if album_owned or all_owned:
            counts["owned"] += 1
        if group.mode in counts:
            counts[group.mode] += 1
        groups.append(group)
    resolving = playlist.state == "resolving" or any(t.resolution is None and not t.dropped for t in tracks)
    has_run = playlist.state in ("running", "paused", "done")
    new_groups = [g for g in groups if has_run and g.choice.queue_item_id is None and g.mode != "skip"]
    return Review(playlist=playlist, tracks=tracks, groups=groups, attention=attention, counts=counts, total_mb=total,
                  resolving=resolving, new_groups=new_groups)


# Actions ------------------------------------------------------------------------------------

def set_options(playlist_id: int, *, full_albums: bool | None = None, prefer_studio: bool | None = None) -> None:
    with session() as s:
        playlist = s.get(Playlist, playlist_id)
        changed_studio = prefer_studio is not None and prefer_studio != playlist.prefer_studio_album
        if full_albums is not None:
            playlist.full_albums = full_albums
        if prefer_studio is not None:
            playlist.prefer_studio_album = prefer_studio
        s.add(playlist)
        s.commit()
    if changed_studio:
        rechoose(playlist_id)


def set_mode(playlist_id: int, rg_mbid: str, mode: str | None) -> None:
    if mode not in (None, "full", "tracks", "tracks_plus", "upgrade", "skip"):
        raise ValueError(f"unknown mode {mode!r}")
    with session() as s:
        choice = s.exec(select(PlaylistAlbum).where(PlaylistAlbum.playlist_id == playlist_id,
                                                    PlaylistAlbum.rg_mbid == rg_mbid)).first()
        if choice is None:
            choice = PlaylistAlbum(playlist_id=playlist_id, rg_mbid=rg_mbid)
        choice.mode = mode
        s.add(choice)
        s.commit()


def set_track_album(track_id: int, rg_mbid: str) -> None:
    """The user picked another of the row's albums (or its single)."""
    with session() as s:
        track = s.get(PlaylistTrack, track_id)
        if rg_mbid == track.single_rg_mbid:
            track.use_single = True
        else:
            options = {o["rg_mbid"]: AlbumOption(**o) for o in track.options or []}
            if rg_mbid in options:
                ensure_album_row(options[rg_mbid])
            track.rg_mbid = rg_mbid
            track.use_single = False
            if track.resolution in (None, "none", "text-weak"):
                track.resolution = "manual"
        s.add(track)
        s.commit()


def set_track_manual(track_id: int, rg_mbid: str) -> None:
    """Resolved by hand to a release group the user searched for; its Album row already exists."""
    with session() as s:
        track = s.get(PlaylistTrack, track_id)
        track.rg_mbid, track.single_rg_mbid, track.use_single = rg_mbid, None, None
        track.resolution = "manual"
        s.add(track)
        s.commit()


def toggle_track(track_id: int, what: str) -> None:
    with session() as s:
        track = s.get(PlaylistTrack, track_id)
        if what == "drop":
            track.dropped = not track.dropped
        elif what == "owned":
            track.owned_unticked = not track.owned_unticked
            if track.owned_unticked and track.owned_how == "name":
                track.owned_path, track.owned_how = None, None
            elif not track.owned_unticked:
                mark_owned([track])
        else:
            raise ValueError(what)
        s.add(track)
        s.commit()


def delete_playlist(playlist_id: int) -> None:
    """Rows only; nothing on disk, in Plex or in the queue is touched."""
    with session() as s:
        for track in s.exec(select(PlaylistTrack).where(PlaylistTrack.playlist_id == playlist_id)):
            s.delete(track)
        for choice in s.exec(select(PlaylistAlbum).where(PlaylistAlbum.playlist_id == playlist_id)):
            s.delete(choice)
        playlist = s.get(Playlist, playlist_id)
        if playlist:
            s.delete(playlist)
        s.commit()


def all_playlists() -> list[tuple[Playlist, int]]:
    with session() as s:
        playlists = s.exec(select(Playlist).order_by(Playlist.id.desc())).all()
        counts = {}
        for pid in s.exec(select(PlaylistTrack.playlist_id)).all():
            counts[pid] = counts.get(pid, 0) + 1
    return [(p, counts.get(p.id, 0)) for p in playlists]
