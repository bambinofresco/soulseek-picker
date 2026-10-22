"""Per-file decisions for a download, from slskd 0.26.1's transfer records.

Pure functions, tested against recorded transfer states.

What slskd does (Transfers/Downloads/DownloadService.cs, Soulseek.NET 10.0.2):
- It retries a failed download itself, in place: same transfer id, up to
  `transfers.download.retry.attempts` attempts (default 3). While waiting for the next
  attempt the record reads "Queued, Locally" with `attempts` already incremented and the
  previous `exception` still set. A poll can catch a few milliseconds of
  "Completed, TimedOut"/"Completed, Errored" before that. `nextAttemptAt` is never set.
- It never retries Rejected, Cancelled or Aborted (size mismatch) transfers.
- An uploader's queue limit arrives as "Completed, Rejected" with exception
  "Transfer rejected: Too many megabytes" (or just "Too many megabytes"; "Too many files").
- Re-enqueueing a file whose transfer is Completed creates a new record with a new id.

So a file has *finally* failed when its state is Completed but not Succeeded and either
the failure is one slskd never retries or `attempts` has reached the maximum. A
retryable failure below the maximum must also stay Completed across polls a few seconds
apart before the picker believes it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .clients.slskd import state_flags

ACTIVE_FLAGS = {"InProgress", "Initializing"}
NEVER_RETRIED = {"Rejected", "Cancelled", "Aborted"}
CONFIRM_FAILURE = timedelta(seconds=5)
LIMIT_PHRASES = ("too many megabytes", "too many files")

# A file's status in Download.file_states.
PENDING = "pending"  # not requested yet (or waiting for room in the window)
ENQUEUED = "enqueued"  # requested; slskd is queueing, transferring or retrying it
DONE = "done"
LIMITED = "limited"  # refused by the uploader's queue limit; requested again when room frees up
RETRY_WAIT = "retry_wait"  # finally failed; the picker re-requests it after a delay
FAILED = "failed"  # failed after the picker's own retries
UNFINISHED = {PENDING, ENQUEUED, LIMITED, RETRY_WAIT}


def is_queue_limit(reason: str | None) -> bool:
    text = (reason or "").lower()
    return any(phrase in text for phrase in LIMIT_PHRASES)


@dataclass
class Observation:
    kind: str  # done | active | queued | failing | failed | limited | missing
    reason: str | None = None
    failed_seen: datetime | None = None  # first poll that saw this failure (for "failing")


def classify(transfer: dict | None, max_attempts: int, failed_seen: datetime | None, now: datetime) -> Observation:
    if transfer is None:
        return Observation("missing")
    flags = state_flags(transfer.get("state"))
    if "Completed" not in flags:
        return Observation("active" if flags & ACTIVE_FLAGS else "queued")
    if "Succeeded" in flags:
        return Observation("done")
    reason = transfer.get("exception") or ", ".join(sorted(flags - {"Completed"})) or "failed"
    if "Rejected" in flags and is_queue_limit(reason):
        return Observation("limited", reason)
    if flags & NEVER_RETRIED or int(transfer.get("attempts") or 1) >= max_attempts:
        return Observation("failed", reason)
    seen = failed_seen or now
    if now - seen >= CONFIRM_FAILURE:
        return Observation("failed", reason)  # slskd would have re-queued it by now
    return Observation("failing", reason, seen)


@dataclass
class Window:
    """How much to have requested from the uploader at once."""

    files: int
    bytes: int

    @classmethod
    def initial(cls, sizes: list[int], chunk_files: int, chunk_mb: int) -> tuple["Window", bool]:
        """Big folders go in chunks from the start, so most uploader queue limits are never hit."""
        limit_bytes = chunk_mb * 1024 * 1024
        if len(sizes) <= chunk_files and sum(sizes) <= limit_bytes:
            return cls(len(sizes) or 1, sum(sizes)), False
        return cls(chunk_files, limit_bytes), True

    @classmethod
    def accepted(cls, outstanding_sizes: list[int]) -> "Window":
        """After a queue-limit rejection: what the uploader did accept (at least one file)."""
        return cls(max(1, len(outstanding_sizes)), sum(outstanding_sizes))


def plan_requests(files: list[dict], states: dict[str, dict], window: Window, now: datetime) -> list[dict]:
    """Files to request now, in folder order, keeping what is outstanding within the window.

    Always lets at least one file through when nothing is outstanding, so a window smaller
    than a single file cannot stall the download.
    """
    outstanding = [f for f in files if states[f["filename"]]["status"] == ENQUEUED]
    count, size = len(outstanding), sum(f["size"] for f in outstanding)
    out = []
    for f in files:
        st = states[f["filename"]]
        if st["status"] not in (PENDING, LIMITED):
            continue
        not_before = st.get("not_before")
        if not_before and datetime.fromisoformat(not_before) > now:
            continue
        if count >= window.files or (count > 0 and size + f["size"] > window.bytes):
            break
        out.append(f)
        count += 1
        size += f["size"]
    return out


def summary(states: dict[str, dict]) -> dict[str, int]:
    counts = {s: 0 for s in (PENDING, ENQUEUED, DONE, LIMITED, RETRY_WAIT, FAILED)}
    for st in states.values():
        counts[st["status"]] = counts.get(st["status"], 0) + 1
    return counts
