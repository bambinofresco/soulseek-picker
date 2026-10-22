"""The download job: enqueue a candidate folder in slskd, track it, verify where it landed."""

from __future__ import annotations

import asyncio
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlmodel import select

from .clients import services
from .clients.http import ServiceError
from .clients.slskd import state_flags, transfer_failed, transfer_succeeded
from .config import Settings, get_settings
from .db import session
from .grouping import split_path
from .jobs import JobFailed, handler, log_event, worker
from .models import Candidate, Download, Job, utcnow

POLL_INTERVAL = 3.0
POSITION_INTERVAL = 30.0  # seconds between queue-position lookups
# Ignore transfers of the same file requested this long before we enqueued (earlier attempts).
REQUEST_SLACK = timedelta(minutes=1)


def files_to_fetch(candidate: Candidate, settings: Settings) -> list[dict]:
    """Audio plus small sidecars: logs, cues, images under the size cap; scans folders optional."""
    max_image = settings.max_sidecar_mb * 1024 * 1024
    out = []
    for f in candidate.files:
        if f.get("in_scans") and not settings.download_scans:
            continue
        kind = f["kind"]
        if kind in ("audio", "log", "cue") or (kind == "image" and f["size"] <= max_image):
            out.append({"filename": f["filename"], "size": f["size"], "disc": f.get("disc")})
    return out


def start_download(candidate: Candidate, rg_mbid: str, release_mbid: str | None = None) -> Download:
    settings = get_settings()
    files = files_to_fetch(candidate, settings)
    with session() as s:
        dl = Download(
            candidate_id=candidate.id,
            search_id=candidate.search_id,
            rg_mbid=rg_mbid,
            release_mbid=release_mbid,
            username=candidate.username,
            folder=candidate.folder,
            destination="",
            files=files,
            bytes_total=sum(f["size"] for f in files),
        )
        s.add(dl)
        s.commit()
        s.refresh(dl)
    job = worker.enqueue("download", {"download_id": dl.id}, rg_mbid=rg_mbid)
    _save(dl, job_id=job.id, destination=destination_for(f"{job.id}"))
    return dl


DESTINATION_ROOT = "picker"


def destination_for(*parts: str) -> str:
    """A slskd batch destination under picker/.

    Lidarr's slskd plugin claims, imports and deletes only batches whose destination
    starts with "lidarr/" (case-insensitive); picker downloads must never live there.
    """
    path = "/".join((DESTINATION_ROOT, *parts))
    if path.split("/")[0].lower() == "lidarr":
        raise ValueError(f"refusing slskd destination {path!r}: Lidarr's slskd plugin owns lidarr/")
    return path


def disc_batches(dl: Download) -> list[tuple[str, list[dict]]]:
    """One batch per disc folder: slskd puts a batch's files flat in its destination, so
    CD1/01.flac and CD2/01.flac would collide (slskd renames the second) in one batch."""
    groups: dict[str, list[dict]] = {}
    for f in dl.files:
        groups.setdefault(f.get("disc") or "", []).append(f)
    out = []
    for disc in sorted(groups):
        sub = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", disc).strip().rstrip(".")
        out.append((f"{dl.destination}/{sub}" if sub else dl.destination, groups[disc]))
    return out


def _save(dl: Download, **fields) -> None:
    """Write only the named columns, so a concurrent give_up from the web isn't overwritten."""
    for name, value in fields.items():
        setattr(dl, name, value)
    with session() as s:
        row = s.get(Download, dl.id)
        for name, value in fields.items():
            setattr(row, name, value)
        s.add(row)
        s.commit()


def request_give_up(download_id: int) -> None:
    with session() as s:
        row = s.get(Download, download_id)
        if row and row.state in ("queued", "enqueued", "transferring"):
            row.give_up = True
            s.add(row)
            s.commit()


def _given_up(download_id: int) -> bool:
    with session() as s:
        row = s.get(Download, download_id)
        return bool(row and row.give_up)


ACTIVE_FLAGS = {"InProgress", "Initializing"}
QUEUED_FLAGS = {"Queued", "Requested"}


def transfer_phase(transfers: dict[str, dict]) -> str:
    """active (bytes can flow) | queued (waiting on the uploader) | waiting (nothing visible yet)."""
    unfinished = [state_flags(t.get("state")) for t in transfers.values() if "Completed" not in state_flags(t.get("state"))]
    if any(f & ACTIVE_FLAGS for f in unfinished):
        return "active"
    if any(f & QUEUED_FLAGS for f in unfinished):
        return "queued"
    return "waiting"


def check_timeouts(dl: Download, phase: str, progressed: bool, now: datetime, settings: Settings) -> tuple[dict, str | None]:
    """The clock updates for this poll and, if a limit is passed, why the download gives up.

    The stall clock only runs while a transfer is active (bytes started, then stopped).
    The queue clock runs while waiting in the uploader's queue, or for slskd to show the transfer.
    """
    stall = timedelta(minutes=settings.stall_timeout_minutes)
    queue = timedelta(hours=settings.queue_timeout_hours)
    if phase == "active":
        last = now if progressed or dl.last_progress_at is None else dl.last_progress_at
        updates = {"last_progress_at": last, "queued_since": None, "queue_position": None}
        if now - last > stall:
            return updates, f"bytes stopped flowing for {int(stall.total_seconds() // 60)} minutes"
        return updates, None
    since = dl.queued_since or now
    updates = {"queued_since": since, "last_progress_at": now if progressed else dl.last_progress_at}
    if now - since > queue:
        where = f" (position {dl.queue_position})" if dl.queue_position else ""
        return updates, f"waited {settings.queue_timeout_hours} h in {dl.username}'s upload queue{where}"
    return updates, None


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ts.astimezone(timezone.utc) if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def latest_transfers(transfers: list[dict], wanted: set[str], since: datetime) -> dict[str, dict]:
    """The most recent transfer per wanted filename, ignoring ones from before this download."""
    latest: dict[str, dict] = {}
    for t in transfers:
        name = t.get("filename")
        if name not in wanted:
            continue
        requested = _parse_ts(t.get("requestedAt") or t.get("enqueuedAt"))
        if requested and requested < since - REQUEST_SLACK:
            continue
        if name not in latest or (t.get("requestedAt") or "") > (latest[name].get("requestedAt") or ""):
            latest[name] = t
    return latest


@handler("download")
async def run_download(job: Job) -> None:
    with session() as s:
        dl = s.get(Download, job.payload["download_id"])
    try:
        await _download(job, dl)
    except Exception as exc:
        _save(dl, state="failed", error=str(exc), finished_at=utcnow())
        raise


async def _download(job: Job, dl: Download) -> None:
    slskd = services().slskd
    if dl.state == "queued":
        batch_ids = []
        for destination, files in disc_batches(dl):
            result = await slskd.enqueue_batch(dl.username, files, destination)
            if result is None:
                await slskd.enqueue_legacy(dl.username, dl.files)
                _save(dl, legacy_enqueue=True)
                log_event(job.id, dl.rg_mbid, "slskd has no batch API; enqueued with the older per-file call")
                break
            batch_ids.append(str(result["_batch_id"]))
            for failure in result.get("failures") or []:
                log_event(job.id, dl.rg_mbid, f"slskd did not queue {split_path(failure.get('filename', ''))[-1]}: {failure.get('message')}")
        _save(dl, batch_id=",".join(batch_ids) or None, state="enqueued", queued_since=utcnow())
        log_event(job.id, dl.rg_mbid, f"Enqueued {len(dl.files)} files from {dl.username}")

    await _track(job, dl)
    _verify(job, dl)
    from .importing import start_import  # imported here: importing imports this module

    start_import(dl)


async def _track(job: Job, dl: Download) -> None:
    slskd = services().slskd
    settings = get_settings()
    wanted = {f["filename"] for f in dl.files}
    next_position_check = 0.0
    while True:
        transfers = latest_transfers(await slskd.user_downloads(dl.username), wanted, dl.created_at)
        if _given_up(dl.id):
            await _cancel_unfinished(dl, transfers)
            raise JobFailed("you gave up on this folder")
        bytes_done = sum(
            f["size"] if transfer_succeeded(t.get("state")) else int(t.get("bytesTransferred") or 0)
            for f in dl.files if (t := transfers.get(f["filename"]))
        )
        succeeded = sum(1 for t in transfers.values() if transfer_succeeded(t.get("state")))
        failed = [t for t in transfers.values() if transfer_failed(t.get("state"))]

        now = utcnow()
        progressed = bytes_done > dl.bytes_done or succeeded > dl.files_done
        phase = transfer_phase(transfers)
        if phase == "queued" and time.monotonic() >= next_position_check:
            next_position_check = time.monotonic() + POSITION_INTERVAL
            dl.queue_position = await _queue_position(dl, transfers)
        clocks, timed_out = check_timeouts(dl, phase, progressed, now, settings)
        state = "transferring" if bytes_done else dl.state
        _save(dl, **{"bytes_done": bytes_done, "files_done": succeeded, "state": state,
                     "queue_position": dl.queue_position, **clocks})

        if failed:
            await _cancel_unfinished(dl, transfers)
            first = failed[0]
            reason = ", ".join(sorted(state_flags(first.get("state")) - {"Completed"}))
            raise JobFailed(
                f"{len(failed)} file(s) failed ({reason}), e.g. {split_path(first['filename'])[-1]}"
                + (f": {first['exception']}" if first.get("exception") else "")
            )
        if succeeded == len(wanted):
            _save(dl, state="completed", finished_at=now)
            log_event(job.id, dl.rg_mbid, f"Download complete: {succeeded} files from {dl.username}")
            return
        if timed_out:
            await _cancel_unfinished(dl, transfers)
            raise JobFailed(timed_out)
        await asyncio.sleep(POLL_INTERVAL)


async def _queue_position(dl: Download, transfers: dict[str, dict]) -> int | None:
    """The uploader's queue position of the first file still queued."""
    queued = sorted(
        (t for t in transfers.values() if state_flags(t.get("state")) & QUEUED_FLAGS and t.get("id")),
        key=lambda t: t["filename"].lower(),
    )
    if not queued:
        return None
    try:
        return await services().slskd.queue_position(dl.username, queued[0]["id"])
    except ServiceError:
        return dl.queue_position  # keep the last known value; positions are informational


async def _cancel_unfinished(dl: Download, transfers: dict[str, dict]) -> None:
    """Cancel what is still queued or running, so it doesn't finish behind our back later.

    Partial files are left for slskd to clean up.
    """
    slskd = services().slskd
    for t in transfers.values():
        if "Completed" not in state_flags(t.get("state")) and t.get("id"):
            await slskd.cancel_download(dl.username, t["id"])


def landed_dir(dl: Download, settings: Settings) -> Path:
    if dl.legacy_enqueue:
        # Without a destination, slskd puts files under the remote folder's own name.
        return settings.download_dir / split_path(dl.folder)[-1]
    return settings.download_dir / Path(*dl.destination.split("/"))


def _verify(job: Job, dl: Download) -> None:
    """Check every enqueued file is on disk with the right size.

    This runs where the picker sees the download directory; the full integrity check
    (flac -t, mutagen) belongs to the import pipeline.
    """
    root = landed_dir(dl, get_settings())
    if not root.is_dir():
        log_event(job.id, dl.rg_mbid, f"Warning: cannot see the download at {root}; check the path mapping")
        return
    expected = Counter((split_path(f["filename"])[-1], f["size"]) for f in dl.files)
    found = Counter((p.name, p.stat().st_size) for p in root.rglob("*") if p.is_file())
    missing = expected - found
    if missing:
        names = ", ".join(sorted(name for name, _ in missing)[:5])
        _save(dl, error=f"{sum(missing.values())} file(s) missing or wrong size on disk: {names}")
        log_event(job.id, dl.rg_mbid, f"Warning: {dl.error}")
    else:
        log_event(job.id, dl.rg_mbid, f"Verified {len(dl.files)} files at {root}")


def downloads_for_search(search_id: int) -> list[Download]:
    with session() as s:
        return list(s.exec(select(Download).where(Download.search_id == search_id)))
