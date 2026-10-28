"""The download job: enqueue a candidate folder in slskd, track it, verify where it landed."""

from __future__ import annotations

import asyncio
import copy
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlmodel import select

from .clients import services
from .clients.http import ServiceError
from .clients.slskd import state_flags, transfer_succeeded
from .config import Settings, get_settings
from .db import session
from .grouping import split_path
from .jobs import JobFailed, handler, log_event, worker
from .models import Candidate, Download, Job, utcnow
from .transfers import (
    DONE,
    ENQUEUED,
    FAILED,
    LIMITED,
    PENDING,
    RETRY_WAIT,
    UNFINISHED,
    Window,
    classify,
    plan_requests,
    summary,
)

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


def start_download(candidate: Candidate, rg_mbid: str, release_mbid: str | None = None,
                   upgrade_of: str | None = None) -> Download:
    """`upgrade_of`: the library folder this download replaces once it passes (spec 7.2)."""
    settings = get_settings()
    files = files_to_fetch(candidate, settings)
    with session() as s:
        dl = Download(
            candidate_id=candidate.id,
            search_id=candidate.search_id,
            rg_mbid=rg_mbid,
            release_mbid=release_mbid,
            upgrade_of=upgrade_of,
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


def disc_batches(dl: Download, files: list[dict] | None = None) -> list[tuple[str, list[dict]]]:
    """One batch per disc folder: slskd puts a batch's files flat in its destination, so
    CD1/01.flac and CD2/01.flac would collide (slskd renames the second) in one batch."""
    groups: dict[str, list[dict]] = {}
    for f in dl.files if files is None else files:
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
        if dl.refetch_import_id:
            from .importing import refetch_failed

            refetch_failed(dl.refetch_import_id, str(exc))
            _save(dl, refetch_import_id=None)
        raise


async def _download(job: Job, dl: Download) -> None:
    from . import importing  # imported here: importing imports this module

    outcome = await _track(job, dl)
    if dl.refetch_import_id:
        # Re-downloading files that failed an import's integrity check: hand back to that import.
        import_id = dl.refetch_import_id
        _save(dl, refetch_import_id=None)
        if outcome == "completed":
            importing.resume_after_refetch(import_id)
        else:
            importing.refetch_failed(import_id, dl.error or "the files could not be downloaded again")
        return
    if outcome != "completed":
        return  # partial: the Downloads page offers retrying the failed files or the next candidate
    _verify(job, dl)
    importing.start_import(dl)


def local_path(dl: Download, remote: dict, settings: Settings) -> Path:
    """Where a requested file lands: <destination>[/<disc folder>]/<file name> (see disc_batches)."""
    destination = next(dest for dest, group in disc_batches(dl, [remote]))
    return settings.download_dir / Path(*destination.split("/")) / split_path(remote["filename"])[-1]


def refetch_files(download_id: int, remote_names: list[str], import_id: int) -> Download:
    """Download these files of a finished download again (their damaged copies already deleted)."""
    with session() as s:
        dl = s.get(Download, download_id)
    states = copy.deepcopy(dl.file_states or {f["filename"]: {"status": DONE, "tries": 0} for f in dl.files})
    for name in remote_names:
        states[name].update(status=PENDING, tries=0, not_before=None, failed_seen=None,
                            error="failed the integrity check")
    job = worker.enqueue("download", {"download_id": dl.id}, rg_mbid=dl.rg_mbid)
    _save(dl, file_states=states, state="transferring", error=None, finished_at=None, job_id=job.id,
          queued_since=utcnow(), last_progress_at=None, refetch_import_id=import_id)
    return dl


def _initial_states(dl: Download, settings: Settings) -> tuple[dict, Window, str | None]:
    states = {f["filename"]: {"status": PENDING, "tries": 0} for f in dl.files}
    window, chunked = Window.initial([f["size"] for f in dl.files], settings.chunk_files, settings.chunk_mb)
    return states, window, "planned" if chunked else None


async def _request(job: Job, dl: Download, files: list[dict], states: dict, legacy: bool) -> None:
    """Ask slskd for these files. Files slskd says are already in progress stay enqueued."""
    slskd = services().slskd
    if legacy:
        await slskd.enqueue_legacy(dl.username, files)
    else:
        batch_ids = [b for b in (dl.batch_id or "").split(",") if b]
        for destination, group in disc_batches(dl, files):
            result = await slskd.enqueue_batch(dl.username, group, destination)
            if result is None:  # slskd lost the batch API mid-download; should not happen
                raise JobFailed("slskd stopped accepting batch downloads")
            batch_ids.append(str(result["_batch_id"]))
            for failure in result.get("failures") or []:
                message = str(failure.get("message") or "")
                if "already in progress" not in message.lower():
                    name = split_path(failure.get("filename", ""))[-1]
                    log_event(job.id, dl.rg_mbid, f"slskd did not queue {name}: {message}")
        dl.batch_id = ",".join(batch_ids)
    for f in files:
        st = states[f["filename"]]
        st.update(status=ENQUEUED, failed_seen=None, not_before=None, requests=st.get("requests", 0) + 1)


def _observe(job: Job, dl: Download, states: dict, transfers: dict, max_attempts: int, now: datetime,
             settings: Settings) -> bool:
    """Update each requested file from its transfer. Returns True if the uploader's queue limit was hit."""
    limited = False
    for name, st in states.items():
        if st["status"] != ENQUEUED:
            continue
        seen = datetime.fromisoformat(st["failed_seen"]) if st.get("failed_seen") else None
        obs = classify(transfers.get(name), max_attempts, seen, now)
        st["failed_seen"] = obs.failed_seen.isoformat() if obs.failed_seen else None
        short = split_path(name)[-1]
        if obs.kind == "done":
            st.update(status=DONE, error=None)
        elif obs.kind == "limited":
            limited = True
            st.update(status=LIMITED, error=obs.reason)
        elif obs.kind == "failed":
            st["tries"] = st.get("tries", 0) + 1
            st["error"] = obs.reason
            if st["tries"] > settings.download_retries:
                st["status"] = FAILED
                log_event(job.id, dl.rg_mbid, f"{short} failed {st['tries']} times; giving up on it ({obs.reason})")
            else:
                wait = settings.retry_delay_seconds * st["tries"]
                st.update(status=RETRY_WAIT, not_before=(now + timedelta(seconds=wait)).isoformat())
                log_event(job.id, dl.rg_mbid, f"{short} failed ({obs.reason}); requesting it again in {wait} s "
                                              f"(retry {st['tries']} of {settings.download_retries})")
    for st in states.values():  # retry delays that have passed
        if st["status"] == RETRY_WAIT and datetime.fromisoformat(st["not_before"]) <= now:
            st["status"] = PENDING
    return limited


async def _track(job: Job, dl: Download) -> str:
    """Request, follow and re-request files until every one is done ("completed") or
    nothing more can be fetched ("partial"). One failing file never cancels the others."""
    slskd = services().slskd
    settings = get_settings()
    max_attempts = await slskd.retry_attempts()
    legacy = not await slskd.supports_batches()
    sizes = {f["filename"]: f["size"] for f in dl.files}
    if dl.file_states:
        states = copy.deepcopy(dl.file_states)
        window = Window(dl.window_files or len(dl.files), dl.window_bytes or sum(sizes.values()))
    else:
        states, window, chunked = _initial_states(dl, settings)
        if legacy:  # the old per-file call can't set a destination; request everything at once
            window, chunked = Window(len(dl.files), sum(sizes.values())), None
        _save(dl, file_states=copy.deepcopy(states), window_files=window.files, window_bytes=window.bytes,
              chunked=chunked, legacy_enqueue=legacy, queued_since=dl.queued_since or utcnow())
        if chunked:
            log_event(job.id, dl.rg_mbid, f"Requesting {len(dl.files)} files from {dl.username} in chunks of "
                                          f"{window.files} files / {window.bytes // 1024 // 1024} MB")
    wanted = set(sizes)
    next_position_check = 0.0
    while True:
        now = utcnow()
        transfers = latest_transfers(await slskd.user_downloads(dl.username), wanted, dl.created_at)
        if _given_up(dl.id):
            await _cancel_unfinished(dl, transfers)
            raise JobFailed("you gave up on this folder")

        if _observe(job, dl, states, transfers, max_attempts, now, settings):
            outstanding = [sizes[n] for n, st in states.items() if st["status"] == ENQUEUED]
            window = Window.accepted(outstanding)
            if dl.chunked != "limit":
                log_event(job.id, dl.rg_mbid, f"{dl.username}'s queue limit refused some files; sending them in "
                                              f"chunks of {window.files} file(s) as earlier ones finish")
            dl.chunked = "limit"
            if not outstanding:  # nothing accepted at all: wait before asking again
                later = (now + timedelta(seconds=settings.retry_delay_seconds)).isoformat()
                for st in states.values():
                    if st["status"] == LIMITED:
                        st["not_before"] = later

        to_request = plan_requests(dl.files, states, window, now)
        if to_request:
            await _request(job, dl, to_request, states, legacy)

        live = {n: t for n, t in transfers.items() if states[n]["status"] == ENQUEUED}
        bytes_done = sum(sizes[n] for n, st in states.items() if st["status"] == DONE) + sum(
            int(t.get("bytesTransferred") or 0) for t in live.values() if not transfer_succeeded(t.get("state"))
        )
        counts = summary(states)
        progressed = bytes_done > dl.bytes_done or counts[DONE] > dl.files_done
        phase = transfer_phase(live)
        if phase != "active" and time.monotonic() >= next_position_check:
            next_position_check = time.monotonic() + POSITION_INTERVAL
            dl.queue_position = await _queue_position(dl, live)
        clocks, timed_out = check_timeouts(dl, phase, progressed, now, settings)
        state = "transferring" if bytes_done else ("enqueued" if dl.state == "queued" else dl.state)
        _save(dl, **{
            "bytes_done": bytes_done, "files_done": counts[DONE], "state": state, "queue_position": dl.queue_position,
            "file_states": copy.deepcopy(states), "window_files": window.files, "window_bytes": window.bytes,
            "chunked": dl.chunked, "batch_id": dl.batch_id, **clocks,
        })

        if counts[DONE] == len(states):
            _save(dl, state="completed", finished_at=now, error=None)
            log_event(job.id, dl.rg_mbid, f"Download complete: {counts[DONE]} files from {dl.username}")
            return "completed"
        if not any(st["status"] in UNFINISHED for st in states.values()):
            failed = [split_path(n)[-1] for n, st in states.items() if st["status"] == FAILED]
            message = f"{len(failed)} of {len(states)} files could not be fetched: {', '.join(failed[:5])}"
            _save(dl, state="partial", finished_at=now, error=message)
            log_event(job.id, dl.rg_mbid, f"Download incomplete: {message}")
            return "partial"
        if timed_out:
            await _cancel_unfinished(dl, transfers)
            raise JobFailed(timed_out)
        await asyncio.sleep(POLL_INTERVAL)


def retry_failed_files(download_id: int) -> Download | None:
    """Give a partial download's failed files a fresh set of retries."""
    with session() as s:
        dl = s.get(Download, download_id)
        if dl is None or dl.state != "partial":
            return None
        states = copy.deepcopy(dl.file_states or {})
    for st in states.values():
        if st["status"] == FAILED:
            st.update(status=PENDING, tries=0, not_before=None)
    job = worker.enqueue("download", {"download_id": dl.id}, rg_mbid=dl.rg_mbid)
    _save(dl, file_states=states, state="transferring", error=None, finished_at=None, job_id=job.id,
          queued_since=utcnow(), last_progress_at=None)
    return dl


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
