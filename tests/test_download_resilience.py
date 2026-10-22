"""Downloads that hit errors or uploader queue limits, replayed against recorded slskd 0.26.1 states.

The transfer records follow slskd 0.26.1 (Transfers/Downloads/DownloadService.cs):
- slskd retries a failed file itself on the same record (same id): between attempts it
  reads "Queued, Locally" with `attempts` incremented and the old `exception` kept; a poll
  can briefly catch "Completed, TimedOut" before that.
- An uploader's queue limit is "Completed, Rejected" with exception
  "Transfer rejected: Too many megabytes" (or the bare reason), never retried by slskd.
- Re-enqueueing a Completed file creates a new record with a new id.
"""

from datetime import timedelta
from itertools import count

import pytest

from picker import downloading, transfers
from picker.db import session
from picker.models import Candidate, Download, Job, Search, utcnow
from picker.transfers import Observation, Window, classify, plan_requests

MB = 1024 * 1024
TIMEOUT = "The wait timed out after 15000 milliseconds"


# Recorded transfer states -------------------------------------------------------------------

QUEUED = {"state": "Queued, Remotely", "attempts": 1}
ACTIVE = {"state": "InProgress", "attempts": 1, "bytesTransferred": 1000}
DONE = {"state": "Completed, Succeeded", "attempts": 1}

# memory_leak's file: timed out on attempt 1, slskd retried it twice itself, then it arrived
# (the web UI showed "Completed, Succeeded (Retry #3)").
SLSKD_RETRIED = [
    QUEUED,
    {"state": "InProgress", "attempts": 1, "bytesTransferred": 500},
    {"state": "Completed, TimedOut", "attempts": 1, "exception": TIMEOUT},  # the brief window
    {"state": "Queued, Locally", "attempts": 2, "exception": TIMEOUT},
    {"state": "Completed, TimedOut", "attempts": 2, "exception": TIMEOUT},
    {"state": "Queued, Locally", "attempts": 3, "exception": TIMEOUT},
    {"state": "InProgress", "attempts": 3, "bytesTransferred": 900},
    {"state": "Completed, Succeeded", "attempts": 3},
]
# slskd's three attempts all failed: final.
SLSKD_GAVE_UP = [QUEUED, {"state": "Completed, Errored", "attempts": 3, "exception": "Remote connection closed"}]


def rejected(reason="Transfer rejected: Too many megabytes"):
    return [{"state": "Completed, Rejected", "attempts": 1, "exception": reason}]


ARRIVES = [QUEUED, ACTIVE, DONE]


class SimSlskd:
    """Replays a scripted life per (file, request): each poll advances every live transfer one step.

    `script(name, request_number, outstanding)` returns the states for that request of that file;
    `outstanding` is how many of this user's files the uploader already holds.
    """

    def __init__(self, script):
        self.script = script
        self.lives: dict[str, dict] = {}
        self.requests: dict[str, int] = {}
        self.enqueued: list[tuple[str, list[str], str]] = []
        self.cancelled: list[str] = []
        self.ids = count(1)
        self.max_outstanding = 0

    def outstanding(self):
        return sum(1 for life in self.lives.values() if "Completed" not in life["states"][life["step"]]["state"])

    async def retry_attempts(self):
        return 3

    async def supports_batches(self):
        return True

    async def enqueue_batch(self, username, files, destination):
        names = [f["filename"] for f in files]
        self.enqueued.append((username, names, destination))
        failures = []
        for name in names:
            life = self.lives.get(name)
            if life and "Completed" not in life["states"][life["step"]]["state"]:
                failures.append({"filename": name, "message": "Skipped: Already in progress"})
                continue
            n = self.requests[name] = self.requests.get(name, 0) + 1
            self.lives[name] = {"id": f"t{next(self.ids)}", "states": self.script(name, n, self.outstanding()),
                                "step": 0, "requested": f"2099-01-01T00:00:{n:02d}Z"}
        self.max_outstanding = max(self.max_outstanding, self.outstanding())
        return {"batch": {"id": "b"}, "failures": failures, "_status": 201 if not failures else 207, "_batch_id": "b"}

    async def user_downloads(self, username):
        out = []
        for name, life in self.lives.items():
            snap = life["states"][life["step"]]
            out.append({"id": life["id"], "filename": name, "size": 1, "requestedAt": life["requested"],
                        "bytesTransferred": 0, **snap})
            life["step"] = min(life["step"] + 1, len(life["states"]) - 1)
        return out

    async def cancel_download(self, username, transfer_id):
        self.cancelled.append(transfer_id)

    async def queue_position(self, username, transfer_id):
        return 3


class Clock:
    def __init__(self):
        self.now = utcnow()

    def __call__(self):
        self.now += timedelta(seconds=3)  # one poll
        return self.now


@pytest.fixture
def run(settings, monkeypatch):
    settings.retry_delay_seconds = 10
    imports = []
    monkeypatch.setattr(downloading, "POLL_INTERVAL", 0)
    monkeypatch.setattr(downloading, "utcnow", Clock())
    monkeypatch.setattr(downloading, "worker", type("W", (), {"enqueue": staticmethod(_job)})())
    import picker.importing as importing
    monkeypatch.setattr(importing, "start_import", lambda dl: imports.append(dl.id))

    async def go(sim, n_files=14, size=30 * MB):
        monkeypatch.setattr(downloading, "services", lambda: type("S", (), {"slskd": sim})())
        with session() as s:
            search = Search(rg_mbid="rg", state="done")
            s.add(search)
            s.commit()
            s.refresh(search)
            files = [{"filename": rf"m\Album\{i:02d}.flac", "size": size, "kind": "audio", "disc": None}
                     for i in range(1, n_files + 1)]
            c = Candidate(search_id=search.id, username="uploader", folder=r"m\Album", query="q", files=files)
            s.add(c)
            s.commit()
            s.refresh(c)
        dl = downloading.start_download(c, "rg")
        with session() as s:
            job = s.get(Job, dl.job_id)
        await downloading.run_download(job)
        with session() as s:
            return s.get(Download, dl.id), imports

    return go


def _job(kind, payload, rg_mbid=None):
    with session() as s:
        job = Job(kind=kind, payload=payload, rg_mbid=rg_mbid)
        s.add(job)
        s.commit()
        s.refresh(job)
    return job


# Scenario 1: transient per-file errors -------------------------------------------------------

async def test_slskd_retrying_one_file_does_not_fail_the_album(run, settings):
    """memory_leak: one file timed out and slskd retried it; the rest must not be cancelled."""
    settings.chunk_mb = 10_000  # one request, like the real download
    sim = SimSlskd(lambda name, n, out: SLSKD_RETRIED if name.endswith("07.flac") else ARRIVES)
    dl, imports = await run(sim)
    assert dl.state == "completed", dl.error
    assert sim.cancelled == []
    assert sim.requests[r"m\Album\07.flac"] == 1  # slskd's own retries were enough; no re-request
    assert imports == [dl.id]


async def test_final_failure_is_re_requested_without_cancelling_the_rest(run, settings):
    settings.chunk_mb = 10_000
    sim = SimSlskd(lambda name, n, out: SLSKD_GAVE_UP if name.endswith("05.flac") and n == 1 else ARRIVES)
    dl, imports = await run(sim)
    assert dl.state == "completed"
    assert sim.cancelled == []
    assert sim.requests[r"m\Album\05.flac"] == 2  # the picker asked again after slskd gave up
    assert dl.file_states[r"m\Album\05.flac"]["tries"] == 1
    assert imports == [dl.id]


async def test_a_file_that_never_arrives_leaves_a_partial_download(run, settings):
    settings.chunk_mb = 10_000
    settings.download_retries = 2
    sim = SimSlskd(lambda name, n, out: SLSKD_GAVE_UP if name.endswith("03.flac") else ARRIVES)
    dl, imports = await run(sim)
    assert dl.state == "partial"
    assert "1 of 14 files could not be fetched: 03.flac" in dl.error
    assert sim.requests[r"m\Album\03.flac"] == 3  # first request + 2 retries
    assert sim.cancelled == [] and imports == []  # the import waits for every file
    assert dl.files_done == 13

    again = downloading.retry_failed_files(dl.id)
    assert again.state == "transferring"
    assert again.file_states[r"m\Album\03.flac"]["status"] == "pending"
    assert again.file_states[r"m\Album\01.flac"]["status"] == "done"  # kept


# Scenario 2: uploader queue limits ---------------------------------------------------------

def limit_after(n_allowed, reason="Transfer rejected: Too many megabytes"):
    """An uploader that holds at most n_allowed of our files and rejects the rest."""
    def script(name, n, outstanding):
        return rejected(reason) if outstanding >= n_allowed else ARRIVES
    return script


async def test_queue_limit_rejections_are_drip_fed(run, settings):
    """ginnungagap555: 9 of 14 files rejected with "Too many megabytes"; nothing is cancelled."""
    settings.chunk_mb = 10_000  # ask for everything at once, as the real download did
    sim = SimSlskd(limit_after(5))
    dl, imports = await run(sim)
    assert dl.state == "completed", dl.error
    assert dl.chunked == "limit"
    assert sim.cancelled == []
    assert sim.max_outstanding <= 5  # after the rejections, never more than the uploader accepts
    assert sum(1 for _, names, _ in sim.enqueued for _ in names) > 14  # rejected files were asked again
    assert all(st["status"] == "done" for st in dl.file_states.values())
    assert imports == [dl.id]  # only after every file


async def test_too_many_files_is_also_a_queue_limit(run, settings):
    settings.chunk_mb = 10_000
    sim = SimSlskd(limit_after(3, reason="Too many files"))  # the bare UploadDenied reason
    dl, _ = await run(sim)
    assert dl.state == "completed" and dl.chunked == "limit"


async def test_big_folders_are_requested_in_chunks_from_the_start(run, settings):
    settings.chunk_mb, settings.chunk_files = 150, 20
    sim = SimSlskd(lambda name, n, out: ARRIVES)
    dl, _ = await run(sim, n_files=14, size=30 * MB)  # 420 MB
    first_request = sim.enqueued[0][1]
    assert len(first_request) == 5  # 5 x 30 MB = 150 MB
    assert sim.max_outstanding <= 5
    assert dl.state == "completed" and dl.chunked == "planned"


async def test_small_folders_are_requested_in_one_go(run, settings):
    sim = SimSlskd(lambda name, n, out: ARRIVES)
    dl, _ = await run(sim, n_files=10, size=5 * MB)
    assert len(sim.enqueued[0][1]) == 10 and dl.chunked is None


# The rules themselves ---------------------------------------------------------------------

@pytest.mark.parametrize("record, kind", [
    ({"state": "Queued, Locally", "attempts": 2, "exception": TIMEOUT}, "queued"),  # slskd retry pending
    ({"state": "Initializing", "attempts": 1}, "active"),
    ({"state": "Completed, Succeeded", "attempts": 3}, "done"),
    ({"state": "Completed, TimedOut", "attempts": 1, "exception": TIMEOUT}, "failing"),
    ({"state": "Completed, Errored", "attempts": 3, "exception": "x"}, "failed"),  # attempts exhausted
    ({"state": "Completed, Rejected", "attempts": 1, "exception": "Transfer rejected: Too many megabytes"}, "limited"),
    ({"state": "Completed, Rejected", "attempts": 1, "exception": "Too many files"}, "limited"),
    ({"state": "Completed, Rejected", "attempts": 1, "exception": "Transfer rejected: File not shared."}, "failed"),
    ({"state": "Completed, Cancelled", "attempts": 1}, "failed"),
    ({"state": "Completed, Aborted", "attempts": 1}, "failed"),  # size mismatch; slskd never retries
    (None, "missing"),
])
def test_classify(record, kind):
    assert classify(record, 3, None, utcnow()).kind == kind


def test_a_retryable_failure_is_believed_only_after_it_persists():
    now = utcnow()
    record = {"state": "Completed, TimedOut", "attempts": 1, "exception": TIMEOUT}
    first = classify(record, 3, None, now)
    assert first == Observation("failing", TIMEOUT, now)
    assert classify(record, 3, first.failed_seen, now + timedelta(seconds=2)).kind == "failing"
    assert classify(record, 3, first.failed_seen, now + timedelta(seconds=6)).kind == "failed"


def test_plan_respects_the_window_but_always_lets_one_file_through():
    now = utcnow()
    files = [{"filename": f"f{i}", "size": 100} for i in range(5)]
    states = {f["filename"]: {"status": "pending"} for f in files}
    assert [f["filename"] for f in plan_requests(files, states, Window(2, 10_000), now)] == ["f0", "f1"]
    assert [f["filename"] for f in plan_requests(files, states, Window(5, 250), now)] == ["f0", "f1"]
    assert [f["filename"] for f in plan_requests(files, states, Window(1, 0), now)] == ["f0"]  # bigger than the window
    states["f0"]["status"] = transfers.ENQUEUED
    assert plan_requests(files, states, Window(1, 0), now) == []
    states["f1"].update(status=transfers.LIMITED, not_before=(now + timedelta(minutes=1)).isoformat())
    states["f0"]["status"] = transfers.DONE
    assert [f["filename"] for f in plan_requests(files, states, Window(1, 0), now)] == ["f2"]  # f1 waits
