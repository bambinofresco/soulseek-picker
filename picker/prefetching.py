"""Fetch a candidate's rip logs and cue sheets before the album, and identify the edition.

Only the .log and .cue files (a few KB) are downloaded, into picker/prefetch/<candidate>/.
Each log's TOC gives a MusicBrainz disc ID; releases carrying it that belong to this
album's release group become the disc ID signal in matching. A cue's CATALOG line is a
barcode signal. Results are cached by (username, remote folder) so a repeat search does
not fetch again.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from datetime import timedelta
from pathlib import Path

from sqlmodel import select

from . import albums
from .clients import services
from .clients.http import ServiceError
from .clients.slskd import transfer_failed, transfer_succeeded
from .config import get_settings
from .db import cache_get, cache_put, session
from .downloading import destination_for, disc_batches, latest_transfers
from .jobs import JobFailed, handler, log_event, worker
from .matching import match_candidate
from .models import Candidate, Download, Job, Search, utcnow
from .riplog import parse_cue, parse_log

MAX_USERS = 5  # prefetch from at most this many users at a time
FETCH_TIMEOUT = 600  # seconds
POLL_INTERVAL = 2.0
CACHE_TTL = timedelta(days=3650)  # a folder's log does not change

_users = asyncio.Semaphore(MAX_USERS)
ACTIVE = ("queued", "fetching")


def cache_key(c: Candidate) -> str:
    return f"prefetch:{c.username}|{c.folder}"


def sidecar_files(c: Candidate) -> list[dict]:
    return [{"filename": f["filename"], "size": f["size"], "disc": f.get("disc")}
            for f in c.files if f["kind"] in ("log", "cue")]


def start_prefetch(c: Candidate, rg_mbid: str) -> None:
    if not sidecar_files(c) or (c.log_info or {}).get("state") in (*ACTIVE, "done"):
        return
    _save(c, log_info={"state": "queued"})
    worker.enqueue("prefetch", {"candidate_id": c.id}, rg_mbid=rg_mbid)


def auto_prefetch(search: Search, candidates: list[Candidate]) -> int:
    """Automatic for users with a free upload slot; everyone else gets a Fetch log button."""
    started = 0
    for c in candidates:
        if c.has_free_slot and sidecar_files(c):
            start_prefetch(c, search.rg_mbid)
            started += 1
    return started


def _save(c: Candidate, **fields) -> None:
    for name, value in fields.items():
        setattr(c, name, value)
    with session() as s:
        s.merge(c)
        s.commit()


@handler("prefetch")
async def run_prefetch(job: Job) -> None:
    with session() as s:
        c = s.get(Candidate, job.payload["candidate_id"])
        search = s.get(Search, c.search_id)
    try:
        info = cache_get(cache_key(c), CACHE_TTL)
        if info is None:
            async with _users:
                _save(c, log_info={"state": "fetching"})
                info = await _fetch_and_read(job, c)
            cache_put(cache_key(c), info)
        _save(c, log_info=info)
        await rematch(c, search.rg_mbid)
        log_event(job.id, search.rg_mbid, _summary(c, info))
    except Exception as exc:
        _save(c, log_info={"state": "failed", "error": str(exc)})
        raise


async def _fetch_and_read(job: Job, c: Candidate) -> dict:
    settings = get_settings()
    slskd = services().slskd
    files = sidecar_files(c)
    destination = destination_for("prefetch", str(c.id))
    started = utcnow()
    # One batch per disc folder, so CD1/rip.log and CD2/rip.log don't collide.
    batches = disc_batches(Download(candidate_id=c.id, search_id=c.search_id, rg_mbid="", username=c.username,
                                    folder=c.folder, destination=destination, files=files))
    for dest, group in batches:
        if await slskd.enqueue_batch(c.username, group, dest) is None:
            raise JobFailed("log prefetch needs slskd 0.26 or newer (the batch download API)")

    wanted = {f["filename"] for f in files}
    deadline = time.monotonic() + FETCH_TIMEOUT
    while True:
        transfers = latest_transfers(await slskd.user_downloads(c.username), wanted, started)
        done = [t for t in transfers.values() if transfer_succeeded(t.get("state"))]
        failed = [t for t in transfers.values() if transfer_failed(t.get("state"))]
        if len(done) + len(failed) == len(wanted):
            break
        if time.monotonic() > deadline:
            raise JobFailed("the log did not arrive within 10 minutes")
        await asyncio.sleep(POLL_INTERVAL)
    if not done:
        raise JobFailed(f"{c.username} refused the log ({failed[0].get('state')})")

    root = settings.download_dir / Path(*destination.split("/"))
    try:
        return await read_sidecars(root)
    finally:
        if root.is_dir():
            shutil.rmtree(root, ignore_errors=True)


async def read_sidecars(root: Path) -> dict:
    """Parse every fetched log and cue under root and look the disc IDs up."""
    found = sorted(p for p in root.rglob("*") if p.is_file()) if root.is_dir() else []
    logs, cues = [], []
    for path in found:
        name = path.relative_to(root).as_posix()
        if path.suffix.lower() == ".log":
            logs.append({"name": name, **parse_log(path.read_bytes()).to_dict()})
        elif path.suffix.lower() == ".cue":
            cues.append({"name": name, **parse_cue(path.read_bytes()).to_dict()})
    if not logs and not cues:
        raise JobFailed(f"nothing readable arrived at {root}")
    return {"state": "done", "logs": logs, "cues": cues, "lookups": await lookup_disc_ids(logs)}


async def lookup_disc_ids(logs: list[dict]) -> list[dict]:
    mb = services().mb
    out = []
    for log in logs:
        for disc in log.get("disc_ids", []):
            releases = await mb.discid_releases(disc["id"], disc["toc"])
            out.append({
                "disc_id": disc["id"],
                "releases": [
                    {"id": r["id"], "title": r.get("title"), "date": r.get("date"), "country": r.get("country"),
                     "rg": (r.get("release-group") or {}).get("id"),
                     "rg_title": (r.get("release-group") or {}).get("title")}
                    for r in releases
                ],
            })
    return out


def disc_id_releases(info: dict, rg_mbid: str) -> tuple[set[str], list[dict]]:
    """Releases in this album's group carrying a fetched disc ID, and others (a box set, a compilation)."""
    mine, others = set(), []
    for lookup in info.get("lookups", []):
        for r in lookup["releases"]:
            if r.get("rg") == rg_mbid:
                mine.add(r["id"])
            elif not any(o["rg"] == r.get("rg") for o in others):
                others.append(r)
    return mine, others


def cue_barcodes(info: dict) -> list[str]:
    return [c["catalog"] for c in info.get("cues", []) if c.get("catalog")]


async def rematch(c: Candidate, rg_mbid: str) -> None:
    info = c.log_info or {}
    try:
        editions = await albums.album_editions(rg_mbid)
    except ServiceError:
        return
    mine, _ = disc_id_releases(info, rg_mbid)
    match = match_candidate(c.files, c.parsed or {}, editions, disc_id_releases=mine, barcodes_extra=cue_barcodes(info))
    _save(c, match=match.to_dict())


def _summary(c: Candidate, info: dict) -> str:
    logs = info.get("logs", [])
    rippers = ", ".join(sorted({l.get("ripper") or "unknown ripper" for l in logs})) or "no log"
    tier = (c.match or {}).get("tier", "Unknown")
    return f"Read {len(logs)} log(s) ({rippers}) from {c.username}: now {tier}"


def active_prefetches(search_id: int) -> bool:
    with session() as s:
        rows = s.exec(select(Candidate.log_info).where(Candidate.search_id == search_id)).all()
    return any((r or {}).get("state") in ACTIVE for r in rows)
