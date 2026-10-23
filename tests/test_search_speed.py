"""Faster searches: stop once there is enough to show, wait for slskd to save results, Search more."""

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from picker import albums, searching
from picker.db import session
from picker.models import Album, Candidate, Job, Search


def folder(user, path, n, free=True, extra=()):
    files = [{"filename": rf"{path}\{i:02d}.flac", "size": 1000 + i, "bitDepth": 16, "sampleRate": 44100, "length": 200}
             for i in range(1, n + 1)]
    files += [{"filename": rf"{path}\{x}", "size": 500} for x in extra]
    return {"username": user, "files": files, "hasFreeUploadSlot": free, "queueLength": 0, "uploadSpeed": 500_000}


class SlowSlskd:
    """A search that keeps getting responses until stopped, then saves them a moment later (like slskd)."""

    def __init__(self, responses, complete_after_stop_polls=2, events=None, natural_end=None):
        self.responses = responses
        self.natural_end = natural_end  # polls after which the search ends by itself
        self.polls = 0
        self.stopped_at = None
        self.complete_after = complete_after_stop_polls
        self.events = events if events is not None else []

    async def start_search(self, text, **_):
        self.events.append("search started")
        return "s1"

    async def get_search(self, sid):
        self.polls += 1
        state = {"responseCount": self.polls * 10, "fileCount": self.polls * 100, "isComplete": False}
        if self.natural_end and self.stopped_at is None and self.polls >= self.natural_end:
            return {**state, "isComplete": True, "endedAt": "2026-10-23T10:00:00Z"}
        if self.stopped_at is not None:
            since = self.polls - self.stopped_at
            state["isComplete"] = True  # reported complete straight away...
            if since >= self.complete_after:
                state["endedAt"] = "2026-10-23T10:00:00Z"  # ...but saved a couple of polls later
        return state

    async def stop_search(self, sid):
        self.stopped_at = self.polls
        self.events.append("stopped")

    async def search_responses(self, sid):
        # Before endedAt, slskd's /responses is still empty.
        if self.natural_end and self.stopped_at is None and self.polls >= self.natural_end:
            return self.responses
        return self.responses if self.stopped_at is not None and self.polls - self.stopped_at >= self.complete_after else []


class Services:
    def __init__(self, slskd):
        self.slskd = slskd


@pytest.fixture
def album(settings, monkeypatch):
    with session() as s:
        s.add(Album(rg_mbid="rg-1", artist="Radiohead", title="OK Computer"))
        s.commit()
    monkeypatch.setattr(searching, "POLL_INTERVAL", 0)
    monkeypatch.setattr(searching, "RESULTS_WAIT", 5)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(searching.asyncio, "sleep", no_sleep)
    return settings


def new_search():
    with session() as s:
        search = Search(rg_mbid="rg-1")
        s.add(search)
        s.commit()
        s.refresh(search)
    return search


async def run(search, deep=False):
    job = Job(id=1, kind="search", payload={"search_id": search.id, "deep": deep}, rg_mbid="rg-1")
    await searching.run_search(job)
    with session() as s:
        return s.get(Search, search.id)


def editions(events=None, gate=None):
    async def fake(rg):
        if events is not None:
            events.append("editions requested")
        if gate is not None:
            await gate.wait()
        return [{"id": "uk", "media": [{"track-count": 12, "format": "CD"}], "label-info": []}]
    return fake


async def test_search_stops_once_it_has_enough_and_waits_for_saved_results(album, monkeypatch):
    album.search_quick_seconds, album.search_quick_responses = 0, 30
    slskd = SlowSlskd([folder("alice", r"m\Radiohead\OK Computer", 12)] * 6)
    monkeypatch.setattr(searching, "services", lambda: Services(slskd))
    monkeypatch.setattr(albums, "editions", editions())
    search = await run(new_search())
    assert search.state == "done" and search.stopped_early
    assert slskd.stopped_at == 3  # 30 responses after 3 polls
    with session() as s:
        rows = s.exec(select(Candidate)).all()
    assert len(rows) == 1  # results were fetched only once slskd had saved them (endedAt)


async def test_editions_load_while_slskd_searches(album, monkeypatch):
    album.search_quick_seconds, album.search_quick_responses = 0, 10
    events, gate = [], asyncio.Event()
    slskd = SlowSlskd([folder("alice", r"m\Radiohead\OK Computer", 12)], events=events)
    monkeypatch.setattr(searching, "services", lambda: Services(slskd))
    monkeypatch.setattr(albums, "editions", editions(events, gate))

    original = slskd.start_search

    async def start_and_release(text, **kw):
        sid = await original(text, **kw)
        gate.set()  # MusicBrainz can only finish once the Soulseek search has started
        return sid

    slskd.start_search = start_and_release
    search = await asyncio.wait_for(run(new_search()), 10)
    assert search.state == "done"
    assert events.index("search started") < events.index("stopped")


async def test_search_more_merges_into_the_table_and_keeps_log_results(album, monkeypatch):
    album.search_quick_seconds, album.search_quick_responses = 0, 10
    monkeypatch.setattr(albums, "editions", editions())
    first = SlowSlskd([folder("alice", r"m\Radiohead\OK Computer", 11)])
    monkeypatch.setattr(searching, "services", lambda: Services(first))
    search = await run(new_search())
    with session() as s:
        alice = s.exec(select(Candidate)).one()
        alice.log_info = {"state": "done", "logs": [], "lookups": []}
        s.add(alice)
        s.commit()
        alice_id = alice.id

    more = SlowSlskd([folder("alice", r"m\Radiohead\OK Computer", 12), folder("bob", r"m\Radiohead\OKC [CD]", 12)],
                     natural_end=8)
    monkeypatch.setattr(searching, "services", lambda: Services(more))
    searching.search_more(search)
    search = await run(search, deep=True)
    assert search.state == "done" and not search.deep and not search.stopped_early
    assert more.stopped_at is None and more.polls == 8  # past the quick threshold (1 poll), it ran until it ended
    with session() as s:
        rows = {c.username: c for c in s.exec(select(Candidate)).all()}
    assert set(rows) == {"alice", "bob"}
    assert rows["alice"].id == alice_id and rows["alice"].audio_count == 12  # merged, same row
    assert rows["alice"].log_info["state"] == "done"  # kept


def test_pick_starts_a_search_once(album, monkeypatch):
    from picker import web
    started = []
    monkeypatch.setattr(web, "start_search", lambda rg: started.append(rg) or new_search())

    async def known_album(rg):
        with session() as s:
            return s.get(Album, rg)

    monkeypatch.setattr(albums, "ensure_album", known_album)
    app = FastAPI()
    app.include_router(web.router)
    client = TestClient(app)
    resp = client.get("/album/rg-1?search=1", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/album/rg-1"
    assert started == ["rg-1"]
    client.get("/album/rg-1?search=1", follow_redirects=False)  # a search is now queued: no second one
    assert started == ["rg-1"]
