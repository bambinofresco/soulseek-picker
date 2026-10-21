"""Phase 5: log prefetch with disc ID matching, the stuck queue, and webhook retags."""

import base64
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from picker import albums, importing, lidarr_feed, prefetching
from picker.db import session
from picker.models import Album, Candidate, Import, Job, Search, utcnow

LOGS = Path(__file__).parent / "fixtures" / "logs"
RG = "rg-okc"
US_DISC = "rdosqrxASN.ZqF.d.5pPhHtgVyA-"


def release(rid, country, tracks=12):
    return {"id": rid, "title": "OK Computer", "date": "1997-07-01", "country": country, "barcode": "",
            "label-info": [], "media": [{"position": 1, "format": "CD", "tracks": [{"length": None}] * tracks}]}


@pytest.fixture
def album_env(settings, monkeypatch):
    async def fake_editions(rg):
        return [release("us-cd", "US"), release("uk-cd", "GB"), release("jp-cd", "JP")]

    monkeypatch.setattr(albums, "editions", fake_editions)
    jobs = []

    def enqueue(kind, payload, rg_mbid=None, run_after=None):
        with session() as s:
            job = Job(kind=kind, payload=payload, rg_mbid=rg_mbid, run_after=run_after)
            s.add(job)
            s.commit()
            s.refresh(job)
        jobs.append(job)
        return job

    for module in (prefetching, lidarr_feed, importing):
        monkeypatch.setattr(module.worker, "enqueue", enqueue)
    with session() as s:
        s.add(Album(rg_mbid=RG, artist="Radiohead", artist_mbid="a74b", title="OK Computer"))
        search = Search(rg_mbid=RG, state="done")
        s.add(search)
        s.commit()
        s.refresh(search)
        folder = r"m\Radiohead\OK Computer"
        files = [{"filename": rf"{folder}\{i:02d}.flac", "size": 1, "kind": "audio", "disc": None, "length": None}
                 for i in range(1, 13)]
        files += [{"filename": rf"{folder}\rip.log", "size": 9000, "kind": "log", "disc": None},
                  {"filename": rf"{folder}\album.cue", "size": 300, "kind": "cue", "disc": None}]
        c = Candidate(search_id=search.id, username="alice", folder=folder, query="q", files=files, audio_count=12,
                      disc_counts=[12], sidecars={"log": 1, "cue": 1}, has_free_slot=True, parsed={},
                      match={"tier": "Low", "editions": ["us-cd", "uk-cd", "jp-cd"]})
        s.add(c)
        s.commit()
        s.refresh(c)
    return settings, search, c, jobs


class FakeSlskd:
    """Delivers the fixture log and cue into the prefetch destination when enqueued."""

    def __init__(self, settings):
        self.settings = settings
        self.batches = []
        self.transfers = []

    async def enqueue_batch(self, username, files, destination):
        self.batches.append((username, files, destination))
        root = self.settings.download_dir / destination / "OK Computer"
        root.mkdir(parents=True)
        (root / "rip.log").write_bytes((LOGS / "eac_utf16.log").read_bytes())
        (root / "album.cue").write_bytes((LOGS / "album.cue").read_bytes())
        self.transfers = [{"id": str(i), "filename": f["filename"], "state": "Completed, Succeeded",
                           "requestedAt": "2099-01-01T00:00:00Z"} for i, f in enumerate(files)]
        return {"id": "b", "_status": 201}

    async def user_downloads(self, username):
        return self.transfers


class FakeMB:
    def __init__(self):
        self.lookups = []

    async def discid_releases(self, disc_id, toc):
        self.lookups.append((disc_id, toc))
        if disc_id != US_DISC:
            return []
        return [
            {"id": "us-cd", "title": "OK Computer", "release-group": {"id": RG, "title": "OK Computer"}},
            {"id": "box", "title": "Box", "release-group": {"id": "rg-box", "title": "XL Box Set"}},
        ]


class Services:
    def __init__(self, slskd=None, mb=None, lidarr=None, plex=None):
        self.slskd, self.mb, self.lidarr, self.plex = slskd, mb, lidarr, plex


async def run_job(job):
    from picker.jobs import HANDLERS
    await HANDLERS[job.kind](job)


async def test_log_prefetch_matches_exact_by_disc_id_before_download(album_env, monkeypatch):
    settings, search, c, jobs = album_env
    slskd, mb = FakeSlskd(settings), FakeMB()
    monkeypatch.setattr(prefetching, "services", lambda: Services(slskd=slskd, mb=mb))
    monkeypatch.setattr(prefetching, "POLL_INTERVAL", 0)

    assert prefetching.auto_prefetch(search, [c]) == 1
    await run_job(jobs[-1])

    with session() as s:
        c = s.get(Candidate, c.id)
    assert slskd.batches[0][2] == f"picker/prefetch/{c.id}"
    assert [f["filename"].rsplit("\\", 1)[-1] for f in slskd.batches[0][1]] == ["rip.log", "album.cue"]
    assert c.log_info["state"] == "done"
    assert c.log_info["logs"][0]["disc_ids"][0]["id"] == US_DISC
    assert mb.lookups[0][1].startswith("1 12 240687 150")
    # Exact by disc ID, narrowed from three CDs to the one carrying that disc ID.
    assert c.match["tier"] == "Exact" and c.match["editions"] == ["us-cd"]
    assert any(s["name"] == "disc_id" for s in c.match["signals"])
    mine, others = prefetching.disc_id_releases(c.log_info, RG)
    assert mine == {"us-cd"} and [o["rg"] for o in others] == ["rg-box"]
    assert c.log_info["cues"][0]["catalog"] == "0724385522925"
    assert not (settings.download_dir / "picker" / "prefetch" / str(c.id)).exists()  # cleaned up

    # Cached by (username, folder): a new search's candidate reads it without fetching again.
    with session() as s:
        twin = Candidate(**{**c.model_dump(), "id": None, "log_info": None, "match": None})
        s.add(twin)
        s.commit()
        s.refresh(twin)
    prefetching.start_prefetch(twin, RG)
    await run_job(jobs[-1])
    assert len(slskd.batches) == 1
    with session() as s:
        assert s.get(Candidate, twin.id).match["tier"] == "Exact"


async def test_busy_users_are_not_prefetched_automatically(album_env):
    _, search, c, jobs = album_env
    c.has_free_slot = False
    assert prefetching.auto_prefetch(search, [c]) == 0 and jobs == []


# Webhook ------------------------------------------------------------------------------

@pytest.fixture
def client(settings):
    from picker.web import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def auth(user, password):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


DOWNLOAD_EVENT = {
    "eventType": "Download",
    "album": {"id": 42, "title": "OK Computer", "mbId": RG},
    "trackFiles": [{"path": "/music/Radiohead/OK Computer (1997)/01 Airbag.flac"}],
}


def test_webhook_auth(client, settings):
    assert client.post("/hooks/lidarr", json={"eventType": "Test"}).status_code == 503  # no password set
    settings.webhook_password = "s3cret"
    assert client.post("/hooks/lidarr", json={"eventType": "Test"}).status_code == 401
    assert client.post("/hooks/lidarr", json={"eventType": "Test"}, headers=auth("lidarr", "nope")).status_code == 401
    ok = client.post("/hooks/lidarr", json={"eventType": "Test"}, headers=auth("lidarr", "s3cret"))
    assert ok.status_code == 200 and ok.json() == {"result": "test ok"}


def test_webhook_debounces_per_album(client, settings, album_env):
    settings.webhook_password = "s3cret"
    h = auth("lidarr", "s3cret")
    first = client.post("/hooks/lidarr", json=DOWNLOAD_EVENT, headers=h).json()["result"]
    assert first.startswith("retag job")
    second_event = {**DOWNLOAD_EVENT, "trackFiles": [{"path": "/music/Radiohead/OK Computer (1997)/02 Paranoid.flac"}]}
    assert client.post("/hooks/lidarr", json=second_event, headers=h).json()["result"].startswith("merged")
    with session() as s:
        jobs = s.exec(select(Job).where(Job.kind == "retag")).all()
    assert len(jobs) == 1
    assert len(jobs[0].payload["paths"]) == 2
    assert jobs[0].run_after > utcnow() + timedelta(seconds=50)
    assert client.post("/hooks/lidarr", json={"eventType": "Grab"}, headers=h).json()["result"] == "ignored Grab"


class FakeLidarr:
    def __init__(self, music):
        self.music = music
        self.commands = []

    async def album(self, album_id):
        return {"id": album_id, "title": "OK Computer", "foreignAlbumId": RG, "releaseDate": "1997-05-21T00:00:00Z",
                "artist": {"artistName": "Radiohead", "foreignArtistId": "a74b"},
                "releases": [{"foreignReleaseId": "uk-cd", "monitored": False},
                             {"foreignReleaseId": "us-cd", "monitored": True}]}

    async def track_files(self, album_id):
        return []

    async def command(self, name, **body):
        self.commands.append(name)
        return {"id": 1}


class FakePlex:
    def __init__(self):
        self.scanned = []

    async def scan_path(self, path):
        self.scanned.append(path)


async def test_retag_runs_beets_in_place_without_moving(album_env, monkeypatch):
    settings, *_ = album_env
    settings.beets_pretend = True
    settings.lidarr_api_key = "k"
    settings.plex_token, settings.plex_section_id = "t", "1"
    folder = settings.music_dir / "Radiohead" / "OK Computer (1997)"
    folder.mkdir(parents=True)
    from tests.test_importing import make_flac
    for n in (1, 2):
        make_flac(folder / f"{n:02d}.flac", n)
    lidarr, plex = FakeLidarr(settings.music_dir), FakePlex()
    for module in (lidarr_feed, importing):
        monkeypatch.setattr(module, "services", lambda: Services(lidarr=lidarr, plex=plex))

    job = lidarr_feed.worker.enqueue("retag", {"album_id": 42, "paths": [str(folder / "01.flac"), str(folder / "02.flac")]})
    await run_job(job)
    with session() as s:
        imp = s.exec(select(Import).where(Import.source == "lidarr")).one()
        import_job = s.get(Job, imp.job_id)
    assert imp.release_mbid == "us-cd" and Path(imp.album_dir) == folder and imp.step == "beets"

    await run_job(import_job)
    with session() as s:
        imp = s.get(Import, imp.id)
    assert imp.state == "imported", imp.error
    assert sorted(p.name for p in folder.iterdir()) == ["01.flac", "02.flac"]  # not moved
    assert "01.flac" in imp.beets_output
    assert lidarr.commands == []  # Lidarr's own edition choice is left alone
    assert len(plex.scanned) == 1


async def test_retag_refuses_paths_outside_the_library(album_env, monkeypatch):
    settings, *_ = album_env
    lidarr = FakeLidarr(settings.music_dir)
    monkeypatch.setattr(lidarr_feed, "services", lambda: Services(lidarr=lidarr))
    job = lidarr_feed.worker.enqueue("retag", {"album_id": 42, "paths": [str(settings.download_dir / "x.flac")]})
    with pytest.raises(Exception, match="not an album folder"):
        await run_job(job)


async def test_stuck_queue_keeps_old_monitored_albums(settings, monkeypatch):
    settings.stuck_days = 3
    now = utcnow()

    class Lidarr:
        async def wanted_missing(self, page, page_size=100):
            records = [
                {"id": 1, "title": "Old", "foreignAlbumId": "rg-old", "releaseDate": (now - timedelta(days=10)).isoformat(),
                 "artist": {"artistName": "A", "foreignArtistId": "a"}},
                {"id": 2, "title": "New", "foreignAlbumId": "rg-new", "releaseDate": (now - timedelta(days=1)).isoformat(),
                 "artist": {"artistName": "B", "foreignArtistId": "b"}},
            ] if page == 1 else [
                {"id": 3, "title": "Older", "foreignAlbumId": "rg-older", "releaseDate": "2001-01-01T00:00:00Z",
                 "artist": {"artistName": "C", "foreignArtistId": "c"}},
            ]
            return {"page": page, "pageSize": 2, "totalRecords": 3, "records": records}

    monkeypatch.setattr(lidarr_feed, "services", lambda: Services(lidarr=Lidarr()))
    await lidarr_feed.refresh_stuck(Job(kind="lidarr_stuck", payload={}))
    stuck = lidarr_feed.stuck_albums()
    assert [a["rg_mbid"] for a in stuck["albums"]] == ["rg-old", "rg-older"]


def test_worker_waits_for_run_after(settings):
    from picker.jobs import ready_jobs, worker
    later = worker.enqueue("noop", {}, run_after=utcnow() + timedelta(minutes=1))
    now = worker.enqueue("noop", {})
    past = worker.enqueue("noop", {}, run_after=utcnow() - timedelta(seconds=1))
    with session() as s:
        assert [j.id for j in ready_jobs(s)] == [now.id, past.id]
    assert later.id not in (now.id, past.id)
