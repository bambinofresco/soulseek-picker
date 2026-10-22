"""Search and download jobs end to end, against a fake slskd."""

from datetime import timedelta

import pytest

from picker import albums, downloading, searching
from picker.clients.musicbrainz import parse_mb_url
from picker.clients.slskd import transfer_failed, transfer_succeeded
from picker.db import session
from picker.jobs import JobFailed
from picker.models import Album, Candidate, Download, Job, Search, utcnow


class FakeSlskd:
    def __init__(self, results_by_query):
        self.results_by_query = results_by_query
        self.searches = {}
        self.enqueued = []
        self.transfers = []
        self.cancelled = []

    async def start_search(self, text, **_):
        sid = f"s{len(self.searches)}"
        self.searches[sid] = text
        return sid

    async def get_search(self, sid):
        return {"isComplete": True, "state": "Completed, TimedOut", "responseCount": 2, "fileCount": 20}

    async def search_responses(self, sid):
        return self.results_by_query.get(self.searches[sid], [])

    async def stop_search(self, sid):
        pass

    async def enqueue_batch(self, username, files, destination):
        # slskd 0.26.1 replies {batch: {id, ...}, failures: [...]}; the client adds _status/_batch_id.
        self.enqueued.append((username, files, destination))
        return {"batch": {"id": f"batch-{len(self.enqueued)}"}, "failures": [], "_status": 201,
                "_batch_id": f"batch-{len(self.enqueued)}"}

    async def user_downloads(self, username):
        return self.transfers

    async def queue_position(self, username, transfer_id):
        return 42

    async def retry_attempts(self):
        return 3

    async def supports_batches(self):
        return True

    async def cancel_download(self, username, transfer_id):
        self.cancelled.append(transfer_id)


class FakeServices:
    def __init__(self, slskd):
        self.slskd = slskd


def folder(user, path, n, ext="flac", free=True):
    files = [{"filename": rf"{path}\{i:02d}.{ext}", "size": 1000 + i, "bitDepth": 16, "sampleRate": 44100, "length": 200} for i in range(1, n + 1)]
    files.append({"filename": rf"{path}\rip.log", "size": 500})
    return {"username": user, "files": files, "hasFreeUploadSlot": free, "queueLength": 0, "uploadSpeed": 500_000}


@pytest.fixture
def album(settings, monkeypatch):
    a = Album(rg_mbid="rg-1", artist="Radiohead", title="OK Computer")
    with session() as s:
        s.add(a)
        s.commit()

    async def fake_editions(rg):
        return [
            {"id": "uk", "media": [{"track-count": 12, "format": "CD"}], "label-info": [{"label": {"name": "Parlophone"}}]},
            {"id": "us", "media": [{"track-count": 13, "format": "CD"}], "label-info": [{"label": {"name": "Capitol Records"}}]},
        ]

    monkeypatch.setattr(albums, "editions", fake_editions)
    return a


def run_job(kind, payload, rg="rg-1"):
    with session() as s:
        job = Job(kind=kind, payload=payload, rg_mbid=rg)
        s.add(job)
        s.commit()
        s.refresh(job)
    return job


async def test_search_runs_fallbacks_and_stores_candidates(album, monkeypatch):
    fake = FakeSlskd({
        "radiohead ok computer": [folder("alice", r"m\Radiohead\OK Computer", 12)],
        "radiohead computer": [
            folder("bob", r"m\Radiohead\1997 OK Computer [Parlophone] [Japan TOCP-50201]", 12, free=False),
            folder("carol", r"m\Radiohead\OKC", 12, ext="mp3"),
        ],
    })
    monkeypatch.setattr(searching, "services", lambda: FakeServices(fake))
    monkeypatch.setattr(searching, "POLL_INTERVAL", 0)

    with session() as s:
        search = Search(rg_mbid="rg-1")
        s.add(search)
        s.commit()
        s.refresh(search)
    await searching.run_search(run_job("search", {"search_id": search.id}))

    with session() as s:
        search = s.get(Search, search.id)
    assert search.state == "done"
    assert search.queries == ["radiohead ok computer", "radiohead computer"]
    assert list(fake.searches.values()) == search.queries

    visible, hidden = searching.ordered_candidates(search)
    # Tier before availability: bob (tracks + label) outranks alice (tracks only, free slot). MP3 hidden.
    assert [c.username for c in visible] == ["bob", "alice"]
    assert hidden == 1
    alice_row, bob_row = visible[1], visible[0]
    assert alice_row.sidecars == {"log": 1}
    with session() as s:
        assert s.get(Album, "rg-1").min_track_count == 12

    assert bob_row.match["editions"] == ["uk"]
    assert bob_row.match["tier"] == "Medium"  # no durations in these editions
    assert alice_row.match["tier"] == "Low"
    assert alice_row.match["editions"] == ["uk"]

    bob = bob_row.parsed
    assert bob["original_year"] == 1997
    assert bob["label"] == "Parlophone"  # label names come from the editions
    assert bob["catalog_numbers"] == ["TOCP-50201"]
    assert bob["territories"] == ["JP"]
    assert bob["evidence"]["label"][0]["token"] == "parlophone"

    by_year, _ = searching.ordered_candidates(search, sort="years")
    assert [c.username for c in by_year] == ["bob", "alice"]  # alice has no year
    only_jp, _ = searching.ordered_candidates(search, depth=24)
    assert only_jp == []
    sixteen, _ = searching.ordered_candidates(search, depth=16, source="unknown")
    assert [c.username for c in sixteen] == ["bob", "alice"]
    by_availability, _ = searching.ordered_candidates(search, sort="availability")
    assert [c.username for c in by_availability] == ["alice", "bob"]
    pinned, _ = searching.ordered_candidates(search, target="us")
    assert pinned == []


async def test_search_failure_marks_search_failed(album, monkeypatch):
    class Broken(FakeSlskd):
        async def start_search(self, text, **_):
            raise JobFailed("slskd down")

    monkeypatch.setattr(searching, "services", lambda: FakeServices(Broken({})))
    with session() as s:
        search = Search(rg_mbid="rg-1")
        s.add(search)
        s.commit()
        s.refresh(search)
    with pytest.raises(JobFailed):
        await searching.run_search(run_job("search", {"search_id": search.id}))
    with session() as s:
        assert s.get(Search, search.id).state == "failed"


def make_candidate(settings):
    with session() as s:
        search = Search(rg_mbid="rg-1", state="done")
        s.add(search)
        s.commit()
        s.refresh(search)
        files = [
            {"filename": r"m\Album\01.flac", "size": 10, "kind": "audio"},
            {"filename": r"m\Album\02.flac", "size": 20, "kind": "audio"},
            {"filename": r"m\Album\rip.log", "size": 3, "kind": "log"},
            {"filename": r"m\Album\huge.png", "size": 50 * 1024 * 1024, "kind": "image"},
            {"filename": r"m\Album\Scans\back.jpg", "size": 5, "kind": "image", "in_scans": True},
            {"filename": r"m\Album\info.nfo", "size": 1, "kind": "text"},
        ]
        c = Candidate(search_id=search.id, username="alice", folder=r"m\Album", query="q", files=files, audio_count=2)
        s.add(c)
        s.commit()
        s.refresh(c)
    return c


def test_files_to_fetch_respects_sidecar_rules(settings):
    c = make_candidate(settings)
    names = [f["filename"].split("\\")[-1] for f in downloading.files_to_fetch(c, settings)]
    assert names == ["01.flac", "02.flac", "rip.log"]
    settings.download_scans = True
    names = [f["filename"].split("\\")[-1] for f in downloading.files_to_fetch(c, settings)]
    assert "back.jpg" in names


def transfer(name, state, done, tid, requested="2099-01-01T00:00:00.0000000Z"):
    return {"id": tid, "filename": name, "state": state, "bytesTransferred": done, "requestedAt": requested}


async def test_download_completes_and_verifies(settings, monkeypatch):
    c = make_candidate(settings)
    fake = FakeSlskd({})
    monkeypatch.setattr(downloading, "services", lambda: FakeServices(fake))
    monkeypatch.setattr(downloading, "worker", type("W", (), {"enqueue": staticmethod(lambda *a, **k: run_job(a[0], a[1]))})())
    monkeypatch.setattr(downloading, "POLL_INTERVAL", 0)
    dl = downloading.start_download(c, "rg-1")
    assert dl.destination == f"picker/{dl.job_id}"

    fake.transfers = [
        transfer(r"m\Album\01.flac", "Completed, Succeeded", 10, "t1"),
        transfer(r"m\Album\02.flac", "Completed, Succeeded", 20, "t2"),
        transfer(r"m\Album\rip.log", "Completed, Succeeded", 3, "t3"),
        # An older failed attempt at the same file is ignored.
        transfer(r"m\Album\02.flac", "Completed, Errored", 0, "old", requested="2000-01-01T00:00:00Z"),
    ]
    landed = settings.download_dir / "picker" / str(dl.job_id)
    landed.mkdir(parents=True)
    (landed / "01.flac").write_bytes(b"x" * 10)
    (landed / "02.flac").write_bytes(b"x" * 20)
    (landed / "rip.log").write_bytes(b"x" * 3)

    with session() as s:
        job = s.get(Job, dl.job_id)
    await downloading.run_download(job)

    with session() as s:
        dl = s.get(Download, dl.id)
    assert fake.enqueued[0][2] == dl.destination
    assert len(fake.enqueued[0][1]) == 3
    assert dl.state == "completed"
    assert dl.bytes_done == dl.bytes_total == 33
    assert dl.error is None


def _started(settings, monkeypatch, fake):
    c = make_candidate(settings)
    monkeypatch.setattr(downloading, "services", lambda: FakeServices(fake))
    monkeypatch.setattr(downloading, "worker", type("W", (), {"enqueue": staticmethod(lambda *a, **k: run_job(a[0], a[1]))})())
    monkeypatch.setattr(downloading, "POLL_INTERVAL", 0)
    dl = downloading.start_download(c, "rg-1")
    with session() as s:
        return dl, s.get(Job, dl.job_id)


async def test_long_queue_wait_is_not_a_stall(settings, monkeypatch):
    """An hour in the uploader's queue, well past the 30 min stall limit, is fine under the 6 h queue limit."""
    fake = FakeSlskd({})
    dl, job = _started(settings, monkeypatch, fake)
    downloading._save(dl, state="enqueued", queued_since=utcnow() - timedelta(hours=1))
    fake.transfers = [transfer(r"m\Album\01.flac", "Queued, Remotely", 0, "t1")]
    polls = 0

    async def user_downloads(username):
        nonlocal polls
        polls += 1
        if polls == 3:  # the uploader finally sends everything
            return [transfer(rf"m\Album\{n}", "Completed, Succeeded", 0, f"t{n}") for n in ("01.flac", "02.flac", "rip.log")]
        return fake.transfers

    fake.user_downloads = user_downloads
    await downloading.run_download(job)
    with session() as s:
        assert s.get(Download, dl.id).state == "completed"


async def test_queue_timeout_gives_up_and_shows_position(settings, monkeypatch):
    fake = FakeSlskd({})
    dl, job = _started(settings, monkeypatch, fake)
    # Resuming after a restart: every file was already requested.
    downloading._save(dl, state="enqueued", queued_since=utcnow() - timedelta(hours=7),
                      file_states={f["filename"]: {"status": "enqueued", "tries": 0} for f in dl.files},
                      window_files=3, window_bytes=33)
    fake.transfers = [transfer(r"m\Album\01.flac", "Queued, Remotely", 0, "t1")]
    with pytest.raises(JobFailed, match=r"waited 6 h in alice's upload queue \(position 42\)"):
        await downloading.run_download(job)
    assert fake.enqueued == []  # a resumed job does not enqueue twice
    assert fake.cancelled == ["t1"]


async def test_stall_counts_only_once_bytes_flow(settings, monkeypatch):
    fake = FakeSlskd({})
    dl, job = _started(settings, monkeypatch, fake)
    downloading._save(dl, state="transferring", bytes_done=5, last_progress_at=utcnow() - timedelta(minutes=31))
    fake.transfers = [transfer(r"m\Album\01.flac", "InProgress", 5, "t1")]
    with pytest.raises(JobFailed, match="bytes stopped flowing for 30 minutes"):
        await downloading.run_download(job)


async def test_give_up_stops_the_job(settings, monkeypatch):
    fake = FakeSlskd({})
    dl, job = _started(settings, monkeypatch, fake)
    downloading._save(dl, state="enqueued", queued_since=utcnow())
    fake.transfers = [transfer(r"m\Album\01.flac", "Queued, Remotely", 0, "t1")]
    downloading.request_give_up(dl.id)
    with pytest.raises(JobFailed, match="gave up"):
        await downloading.run_download(job)
    assert fake.cancelled == ["t1"]
    with session() as s:
        assert s.get(Download, dl.id).state == "failed"


def test_check_timeouts_phases(settings):
    now = utcnow()
    dl = Download(candidate_id=1, search_id=1, rg_mbid="r", username="u", folder="f", destination="d",
                  queued_since=now - timedelta(hours=5))
    clocks, why = downloading.check_timeouts(dl, "queued", False, now, settings)
    assert why is None and clocks["queued_since"] == dl.queued_since
    clocks, why = downloading.check_timeouts(dl, "active", False, now, settings)
    assert why is None and clocks["queued_since"] is None and clocks["last_progress_at"] == now  # first bytes


def test_transfer_phase():
    assert downloading.transfer_phase({"a": {"state": "Queued, Remotely"}, "b": {"state": "Completed, Succeeded"}}) == "queued"
    assert downloading.transfer_phase({"a": {"state": "Initializing"}, "b": {"state": "Queued, Remotely"}}) == "active"
    assert downloading.transfer_phase({}) == "waiting"


def test_one_batch_per_disc_folder(settings):
    dl = Download(candidate_id=1, search_id=1, rg_mbid="r", username="u", folder="f", destination="picker/7", files=[
        {"filename": r"f\CD1\01.flac", "size": 1, "disc": "CD1"},
        {"filename": r"f\CD2\01.flac", "size": 1, "disc": "CD2"},
        {"filename": r"f\folder.jpg", "size": 1, "disc": None},
    ])
    assert [(d, len(fs)) for d, fs in downloading.disc_batches(dl)] == [
        ("picker/7", 1), ("picker/7/CD1", 1), ("picker/7/CD2", 1),
    ]


def test_destinations_never_enter_lidarrs_folder():
    """Lidarr's slskd plugin claims and deletes batches under lidarr/; picker batches live under picker/."""
    assert downloading.destination_for("12") == "picker/12"
    assert downloading.destination_for("prefetch", "3") == "picker/prefetch/3"
    old = downloading.DESTINATION_ROOT
    try:
        downloading.DESTINATION_ROOT = "Lidarr"
        with pytest.raises(ValueError, match="lidarr"):
            downloading.destination_for("1")
    finally:
        downloading.DESTINATION_ROOT = old


def test_transfer_states():
    assert transfer_succeeded("Completed, Succeeded")
    assert not transfer_succeeded("InProgress")
    assert transfer_failed("Completed, TimedOut")
    assert transfer_failed("Completed, Errored")
    assert not transfer_failed("Queued, Remotely")
    assert not transfer_failed("Completed, Succeeded")


def test_parse_mb_url():
    rg = "6d4e5d43-3e4a-3d06-a3f3-5b0e4c1a3b7e"
    assert parse_mb_url(f"https://musicbrainz.org/release-group/{rg}") == ("release-group", rg)
    assert parse_mb_url(f"https://musicbrainz.org/release/{rg.upper()}/discids") == ("release", rg)
    assert parse_mb_url("OK Computer") is None
