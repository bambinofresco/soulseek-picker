"""The download job: enqueue a candidate folder in slskd, track it, verify where it landed."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlmodel import select

from .clients import services
from .clients.slskd import state_flags, transfer_failed, transfer_succeeded
from .config import Settings, get_settings
from .db import session
from .grouping import split_path
from .jobs import JobFailed, handler, log_event, worker
from .models import Candidate, Download, Job, utcnow

POLL_INTERVAL = 3.0
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
            out.append({"filename": f["filename"], "size": f["size"]})
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
    dl.job_id = job.id
    dl.destination = f"picker/{job.id}"
    _save(dl)
    return dl


def _save(dl: Download, **fields) -> None:
    for name, value in fields.items():
        setattr(dl, name, value)
    with session() as s:
        s.merge(dl)
        s.commit()


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
        result = await slskd.enqueue_batch(dl.username, dl.files, dl.destination)
        if result is None:
            await slskd.enqueue_legacy(dl.username, dl.files)
            _save(dl, legacy_enqueue=True)
            log_event(job.id, dl.rg_mbid, "slskd has no batch API; enqueued with the older per-file call")
        else:
            batch_id = result.get("id") or (result.get("batch") or {}).get("id")
            _save(dl, batch_id=str(batch_id) if batch_id else None)
            if result["_status"] == 207:
                log_event(job.id, dl.rg_mbid, "slskd queued only part of the folder; the rest will show as failed")
        _save(dl, state="enqueued", last_progress_at=utcnow())
        log_event(job.id, dl.rg_mbid, f"Enqueued {len(dl.files)} files from {dl.username}")

    await _track(job, dl)
    _verify(job, dl)
    from .importing import start_import  # imported here: importing imports this module

    start_import(dl)


async def _track(job: Job, dl: Download) -> None:
    slskd = services().slskd
    stall_after = timedelta(minutes=get_settings().stall_timeout_minutes)
    wanted = {f["filename"] for f in dl.files}
    while True:
        transfers = latest_transfers(await slskd.user_downloads(dl.username), wanted, dl.created_at)
        bytes_done = sum(
            f["size"] if transfer_succeeded(t.get("state")) else int(t.get("bytesTransferred") or 0)
            for f in dl.files if (t := transfers.get(f["filename"]))
        )
        succeeded = sum(1 for t in transfers.values() if transfer_succeeded(t.get("state")))
        failed = [t for t in transfers.values() if transfer_failed(t.get("state"))]

        now = utcnow()
        if bytes_done > dl.bytes_done or succeeded > dl.files_done:
            dl.last_progress_at = now
        state = "transferring" if bytes_done else dl.state
        _save(dl, bytes_done=bytes_done, files_done=succeeded, state=state)

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
        if dl.last_progress_at and now - dl.last_progress_at > stall_after:
            await _cancel_unfinished(dl, transfers)
            raise JobFailed(f"no progress for {int(stall_after.total_seconds() // 60)} minutes; the user may be offline or queued")
        await asyncio.sleep(POLL_INTERVAL)


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
