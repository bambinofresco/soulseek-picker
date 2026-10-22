"""What the picker takes from Lidarr: the stuck-wanted queue, and retags of Lidarr's own imports.

Retags: Lidarr's Webhook connection posts to /hooks/lidarr on import and upgrade. The
payload has the album's Lidarr ID and the track file paths but not the release MBID, so
the release comes from the album's single monitored release. beets then tags the folder in
place, pinned to it; nothing is moved. Events are debounced per album for 60 seconds so a
multi-disc import that fires twice is retagged once.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlmodel import select

from .clients import services
from .config import get_settings
from .db import cache_get, cache_put, session
from .importing import enqueue as enqueue_import
from .importing import within
from .jobs import JobFailed, handler, log_event, worker
from .models import Album, Import, Job, utcnow

log = logging.getLogger(__name__)

DEBOUNCE = timedelta(seconds=60)
STUCK_KEY = "lidarr:stuck"
STUCK_EVERY = timedelta(hours=1)


# Stuck-wanted queue ----------------------------------------------------------------------

def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@handler("lidarr_stuck")
async def refresh_stuck(job: Job) -> None:
    settings = get_settings()
    lidarr = services().lidarr
    cutoff = utcnow() - timedelta(days=settings.stuck_days)
    stuck, page = [], 1
    while True:
        data = await lidarr.wanted_missing(page)
        records = data.get("records", []) or []
        for album in records:
            released = _parse_date(album.get("releaseDate"))
            if released is None or released > cutoff:
                continue
            artist = album.get("artist") or {}
            stuck.append({
                "album_id": album.get("id"),
                "rg_mbid": album.get("foreignAlbumId"),
                "title": album.get("title"),
                "artist": artist.get("artistName"),
                "artist_mbid": artist.get("foreignArtistId"),
                "release_date": released.date().isoformat(),
            })
        if not records or page * (data.get("pageSize") or 100) >= (data.get("totalRecords") or 0):
            break
        page += 1
    stuck.sort(key=lambda a: a["release_date"], reverse=True)
    cache_put(STUCK_KEY, {"albums": stuck, "checked": utcnow().isoformat()})


def stuck_albums() -> dict:
    return cache_get(STUCK_KEY, timedelta(days=30)) or {"albums": [], "checked": None}


def queue_stuck_refresh() -> None:
    with session() as s:
        pending = s.exec(select(Job).where(Job.kind == "lidarr_stuck", Job.status.in_(["queued", "running"]))).first()
    if pending is None:
        worker.enqueue("lidarr_stuck", {})


async def stuck_scheduler() -> None:
    """Refresh the stuck list at start and then hourly, while Lidarr is configured."""
    while True:
        if get_settings().lidarr_api_key:
            try:
                queue_stuck_refresh()
            except Exception:  # noqa: BLE001 - keep the scheduler alive
                log.exception("could not queue the stuck-albums refresh")
        await asyncio.sleep(STUCK_EVERY.total_seconds())


# Webhook retags --------------------------------------------------------------------------

def receive_webhook(payload: dict) -> str:
    """Queue (or push back) the retag for this album. Returns what was done, for the response."""
    event = str(payload.get("eventType") or "")
    kind = event.lower()  # Lidarr serialises enums in camelCase ("download", "test")
    if kind == "test":
        return "test ok"
    if kind != "download":
        return f"ignored {event}"
    album = payload.get("album") or {}
    album_id = album.get("id")
    if not album_id:
        return "ignored: no album id"
    paths = [f["path"] for f in payload.get("trackFiles", []) or [] if f.get("path")]
    run_after = utcnow() + DEBOUNCE

    with session() as s:
        for job in s.exec(select(Job).where(Job.kind == "retag", Job.status == "queued")):
            if job.payload.get("album_id") == album_id:
                job.payload = {**job.payload, "paths": sorted(set(job.payload.get("paths", [])) | set(paths))}
                job.run_after = run_after
                s.add(job)
                s.commit()
                return f"merged into retag job {job.id}"
    job = worker.enqueue("retag", {"album_id": album_id, "rg_mbid": album.get("mbId"), "paths": paths},
                         rg_mbid=album.get("mbId"), run_after=run_after)
    return f"retag job {job.id} queued"


def album_folder(paths: list[str]) -> Path:
    """The folder holding the album's track files (their common directory)."""
    dirs = {str(Path(p).parent) for p in paths}
    return Path(os.path.commonpath(sorted(dirs)))


@handler("retag")
async def run_retag(job: Job) -> None:
    settings = get_settings()
    lidarr = services().lidarr
    album = await lidarr.album(job.payload["album_id"])
    monitored = [r for r in album.get("releases", []) or [] if r.get("monitored")]
    if len(monitored) != 1:
        raise JobFailed(f"Lidarr album {album.get('id')} has {len(monitored)} monitored releases; expected exactly one")
    release_mbid = monitored[0]["foreignReleaseId"]
    rg_mbid = album.get("foreignAlbumId") or job.payload.get("rg_mbid")

    paths = job.payload.get("paths") or [f["path"] for f in await lidarr.track_files(album["id"]) if f.get("path")]
    if not paths:
        raise JobFailed("Lidarr reported no track files for this album")
    folder = album_folder(paths)
    if not within(folder, settings.music_dir) or folder.resolve() == settings.music_dir.resolve():
        raise JobFailed(f"{folder} is not an album folder inside {settings.music_dir}")

    with session() as s:
        if s.get(Album, rg_mbid) is None:
            artist = album.get("artist") or {}
            s.add(Album(rg_mbid=rg_mbid, artist=artist.get("artistName") or "", artist_mbid=artist.get("foreignArtistId"),
                        title=album.get("title") or "", first_release_date=(album.get("releaseDate") or "")[:10] or None))
            s.commit()
        imp = Import(source="lidarr", rg_mbid=rg_mbid, release_mbid=release_mbid, album_dir=str(folder),
                     step="beets", state="importing")
        s.add(imp)
        s.commit()
        s.refresh(imp)
    log_event(job.id, rg_mbid, f"Lidarr imported {album.get('title')}; retagging {folder} in place")
    enqueue_import(imp)
