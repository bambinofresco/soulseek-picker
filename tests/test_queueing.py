"""The album queue (picker/queueing.py): items move through search, pick, download and import on their own."""

from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from picker import importing, queueing
from picker.db import session
from picker.editions import Edition
from picker.models import Album, Candidate, Download, Import, Job, Playlist, PlaylistAlbum, PlaylistTrack, QueueItem, Search, utcnow
from picker.web import router

GB = Edition(id="gb", title="OK Computer", date="1997-06-16", country="GB", formats=["CD"])
JP = Edition(id="jp", title="OK Computer", date="1997-05-28", country="JP", formats=["CD"])
RM = Edition(id="rm", title="OK Computer", date="2009-03-24", country="GB", formats=["CD"], disambiguation="remastered")
EDITIONS = [GB, JP, RM]


@pytest.fixture
def world(settings, monkeypatch):
    """Albums in the database, fake search/download starters, editions without MusicBrainz."""
    with session() as s:
        s.add(Album(rg_mbid="rg-1", artist="Radiohead", title="OK Computer", min_track_count=12))
        s.add(Album(rg_mbid="rg-2", artist="Radiohead", title="Kid A", min_track_count=10))
        s.add(Album(rg_mbid="rg-va", artist="Various Artists", title="Now 44", min_track_count=20))
        s.commit()
    calls = {"search": [], "more": [], "download": []}

    def fake_start_search(rg):
        with session() as s:
            search = Search(rg_mbid=rg, state="searching")
            s.add(search)
            s.commit()
            s.refresh(search)
        calls["search"].append(rg)
        return search

    def fake_search_more(search):
        calls["more"].append(search.rg_mbid)
        with session() as s:
            row = s.get(Search, search.id)
            row.state, row.deep, row.finished_at = "searching", True, None
            s.add(row)
            s.commit()
            s.refresh(row)
        return row

    def fake_start_download(candidate, rg, release=None, upgrade_of=None, only=None):
        with session() as s:
            dl = Download(candidate_id=candidate.id, search_id=candidate.search_id, rg_mbid=rg, username=candidate.username,
                          folder=candidate.folder, release_mbid=release, destination=f"picker/{candidate.id}", state="queued")
            s.add(dl)
            s.commit()
            s.refresh(dl)
        calls["download"].append((candidate.username, release))
        return dl

    async def fake_editions(rg):
        return EDITIONS

    monkeypatch.setattr(queueing, "start_search", fake_start_search)
    monkeypatch.setattr(queueing, "search_more", fake_search_more)
    monkeypatch.setattr(queueing, "start_download", fake_start_download)
    monkeypatch.setattr(queueing.albums, "album_editions", fake_editions)
    monkeypatch.setattr(queueing.prefetching, "active_prefetches", lambda sid: False)
    monkeypatch.setattr(queueing, "SEARCH_GAP", timedelta(0))
    queueing._last_search_end = None
    return calls


def finish_search(rg, candidates):
    """The latest search for rg is done with these folders."""
    with session() as s:
        search = s.exec(select(Search).where(Search.rg_mbid == rg).order_by(Search.id.desc())).first()
        search.state, search.finished_at = "done", utcnow()
        s.add(search)
        for c in candidates:
            c.search_id = search.id
            s.add(c)
        s.commit()
        return search.id


def cand(username, tier="Exact", editions=("gb",), signals=("disc_id",), **kw) -> Candidate:
    defaults = dict(folder=rf"m\{username}\OK Computer", query="q", files=[], audio_count=12, disc_counts=[12],
                    total_size=360_000_000, audio_format="FLAC", bit_depth=16, sample_rate=44100, sidecars={"log": 1},
                    has_free_slot=True, queue_length=0, upload_speed=2_400_000, parsed={"source": "CD"},
                    match={"tier": tier, "editions": list(editions), "tracks_agree": True, "score": 10,
                           "signals": [{"name": n, "weight": 1, "detail": n} for n in signals]})
    defaults.update(kw)
    return Candidate(username=username, search_id=0, **defaults)


def item(rg="rg-1"):
    with session() as s:
        return s.exec(select(QueueItem).where(QueueItem.rg_mbid == rg).order_by(QueueItem.id.desc())).first()


def set_download(download_id, **fields):
    with session() as s:
        dl = s.get(Download, download_id)
        for k, v in fields.items():
            setattr(dl, k, v)
        s.add(dl)
        s.commit()


def add_import(download_id, state="verifying", **fields):
    with session() as s:
        dl = s.get(Download, download_id)
        imp = Import(download_id=download_id, rg_mbid=dl.rg_mbid, release_mbid=dl.release_mbid, state=state, **fields)
        s.add(imp)
        s.commit()
        s.refresh(imp)
        return imp.id


def set_import(import_id, **fields):
    with session() as s:
        imp = s.get(Import, import_id)
        for k, v in fields.items():
            setattr(imp, k, v)
        s.add(imp)
        s.commit()


# Adding ----------------------------------------------------------------------------------------

def test_add_dedupes_skip_and_requeue(world):
    a = queueing.add("rg-1", source="search")
    assert queueing.add("rg-1").id == a.id
    assert queueing.add("rg-1", playlist_id=7).playlist_id == 7  # the open item joins the run
    with pytest.raises(ValueError):
        queueing.add("rg-2", "bogus")
    queueing.skip(a.id)
    assert item().state == "skipped" and item().finished_at
    assert queueing.add("rg-1").id != a.id  # a closed item is not reused
    queueing.requeue(a.id)
    assert item("rg-1") is not None
    with session() as s:
        assert s.get(QueueItem, a.id).state == "queued" and s.get(QueueItem, a.id).finished_at is None
        assert s.exec(select(Job).where(Job.kind == "queue")).first() is not None


# The happy path --------------------------------------------------------------------------------

async def test_full_flow(world, settings):
    queueing.add("rg-1")
    await queueing.tick(settings)
    assert world["search"] == ["rg-1"] and item().state == "searching"
    await queueing.tick(settings)
    assert item().state == "searching"  # still running
    finish_search("rg-1", [cand("alice"), cand("bob", tier="High", signals=("durations",))])
    await queueing.tick(settings)  # the search is seen finished and the pick made in the same tick
    it = item()
    assert it.state == "downloading" and world["download"] == [("alice", "gb")] and "alice" in it.note
    assert it.candidates_tried
    await queueing.tick(settings)
    set_download(it.download_id, state="completed")
    await queueing.tick(settings)
    assert item().state == "downloading"  # the import row appears a moment later
    imp_id = add_import(it.download_id)
    await queueing.tick(settings)
    assert item().state == "importing" and item().import_id == imp_id
    set_import(imp_id, state="imported", album_dir="/music/Radiohead/OK Computer (1997)")
    await queueing.tick(settings)
    assert item().state == "done" and "OK Computer (1997)" in item().note and item().finished_at
    # the loop stops once nothing is active
    with session() as s:
        s.add(Job(kind="queue"))
        s.commit()
        job = s.exec(select(Job).where(Job.kind == "queue").order_by(Job.id.desc())).first()
    await queueing.run_queue(job)


async def test_fresh_search_is_reused_and_prefetch_is_awaited(world, settings, monkeypatch):
    with session() as s:
        search = Search(rg_mbid="rg-1", state="done", finished_at=utcnow())
        s.add(search)
        s.commit()
    queueing.add("rg-1")
    await queueing.tick(settings)
    assert world["search"] == [] and item().state == "searched"  # the fresh search is used as is
    await queueing.tick(settings)
    assert item().state == "none_found"  # it had no folders

    queueing.add("rg-2")
    await queueing.tick(settings)
    assert world["search"] == ["rg-2"]
    finish_search("rg-2", [])
    monkeypatch.setattr(queueing.prefetching, "active_prefetches", lambda sid: True)
    await queueing.tick(settings)
    assert item("rg-2").state == "searching"  # rip logs still coming in
    monkeypatch.setattr(queueing.prefetching, "active_prefetches", lambda sid: False)
    await queueing.tick(settings)
    assert item("rg-2").state == "none_found"


# Picks that wait for the user --------------------------------------------------------------------

async def test_needs_pick_cases(world, settings):
    queueing.add("rg-1")
    await queueing.tick(settings)
    finish_search("rg-1", [cand("carol", tier="Medium", signals=("year",))])
    await queueing.tick(settings)
    await queueing.tick(settings)
    it = item()
    assert it.state == "needs_pick" and "Medium" in it.note and "High or better" in it.note and world["download"] == []

    # Ambiguous between editions the preferences rank differently (a remaster among them).
    queueing.add("rg-2")
    await queueing.tick(settings)
    finish_search("rg-2", [cand("dave", editions=("gb", "rm"), signals=("catalog",))])
    await queueing.tick(settings)
    await queueing.tick(settings)
    assert item("rg-2").state == "needs_pick" and "one of 2 editions" in item("rg-2").note

    # Various Artists, never unasked.
    queueing.add("rg-va")
    await queueing.tick(settings)
    finish_search("rg-va", [cand("erin")])
    await queueing.tick(settings)
    await queueing.tick(settings)
    assert item("rg-va").state == "needs_pick" and "Various Artists" in item("rg-va").note


async def test_equal_editions_are_not_ambiguous(world, settings):
    settings.preferred_countries = ""  # GB and JP score the same
    queueing.add("rg-1")
    await queueing.tick(settings)
    finish_search("rg-1", [cand("alice", editions=("gb", "jp"), signals=("catalog",))])
    await queueing.tick(settings)
    assert item().state == "downloading" and world["download"][0][1] in ("gb", "jp")


async def test_hand_picked_download_satisfies_the_item(world, settings):
    queueing.add("rg-1")
    await queueing.tick(settings)
    finish_search("rg-1", [cand("carol", tier="Medium", signals=("year",))])
    await queueing.tick(settings)
    await queueing.tick(settings)
    assert item().state == "needs_pick"
    with session() as s:
        carol = s.exec(select(Candidate)).first()
        dl = Download(candidate_id=carol.id, search_id=carol.search_id, rg_mbid="rg-1", username="carol", folder="f",
                      release_mbid="gb", destination="picker/9", state="transferring")
        s.add(dl)
        s.commit()
        s.refresh(dl)
    await queueing.tick(settings)
    it = item()
    assert it.state == "downloading" and it.download_id == dl.id and "by hand" in it.note
    assert queueing.needs_ticking()


# Nothing found and retries ------------------------------------------------------------------------

async def test_none_found_retries_with_the_longer_search(world, settings):
    settings.queue_retry_days = 7
    queueing.add("rg-1")
    await queueing.tick(settings)
    finish_search("rg-1", [])
    await queueing.tick(settings)
    await queueing.tick(settings)
    it = item()
    assert it.state == "none_found" and it.attempts == 1 and it.next_try > utcnow() + timedelta(days=6)
    assert not queueing.needs_ticking()
    queueing._save(it, next_try=utcnow() - timedelta(seconds=1))
    assert queueing.needs_ticking()
    await queueing.tick(settings)  # due: queued again and the longer search started in one tick
    assert world["more"] == ["rg-1"] and item().state == "searching" and item().next_try is None
    finish_search("rg-1", [])
    await queueing.tick(settings)
    await queueing.tick(settings)
    assert item().attempts == 2 and item().state == "none_found"
    queueing._save(item(), next_try=utcnow() - timedelta(seconds=1))
    await queueing.tick(settings)
    await queueing.tick(settings)
    finish_search("rg-1", [])
    await queueing.tick(settings)
    await queueing.tick(settings)
    assert item().state == "failed" and "3 searches" in item().note


async def test_failed_search_is_retried_soon(world, settings):
    queueing.add("rg-1")
    await queueing.tick(settings)
    with session() as s:
        search = s.exec(select(Search)).first()
        search.state, search.error, search.finished_at = "failed", "slskd is down", utcnow()
        s.add(search)
        s.commit()
    await queueing.tick(settings)
    it = item()
    assert it.state == "queued" and it.attempts == 1 and it.next_try > utcnow() and "slskd is down" in it.note
    await queueing.tick(settings)
    assert world["search"] == ["rg-1"]  # not yet due


# Download and import outcomes ----------------------------------------------------------------------

async def test_failed_download_moves_to_the_next_folder_then_gives_up(world, settings):
    settings.queue_candidates = 2
    queueing.add("rg-1")
    await queueing.tick(settings)
    finish_search("rg-1", [cand("alice"), cand("bob", signals=("catalog",)), cand("carol", signals=("barcode",))])
    await queueing.tick(settings)
    await queueing.tick(settings)
    first = item().download_id
    set_download(first, state="failed", error="too many files")
    await queueing.tick(settings)
    it = item()
    assert it.state == "downloading" and it.download_id != first and world["download"] == [("alice", "gb"), ("bob", "gb")]
    assert "too many files" in it.note and "bob" in it.note
    set_download(it.download_id, state="partial")
    await queueing.tick(settings)
    assert item().state == "failed" and "2 folders tried" in item().note


async def test_import_review_then_done_and_reject_moves_on(world, settings):
    queueing.add("rg-1")
    await queueing.tick(settings)
    finish_search("rg-1", [cand("alice"), cand("bob", signals=("catalog",))])
    await queueing.tick(settings)
    await queueing.tick(settings)
    dl_id = item().download_id
    set_download(dl_id, state="completed")
    imp_id = add_import(dl_id, state="review", step="review", error="the files are tagged as a different edition")
    await queueing.tick(settings)
    assert item().state == "review" and "different edition" in item().note
    assert not queueing.needs_ticking()
    set_import(imp_id, state="importing", step="beets")
    assert queueing.needs_ticking()
    await queueing.tick(settings)
    assert item().state == "importing"
    set_import(imp_id, state="rejected")
    await queueing.tick(settings)
    it = item()
    assert it.state == "downloading" and world["download"][-1][0] == "bob" and "rejected" in it.note and it.import_id is None


async def test_concurrency_and_pause(world, settings):
    settings.queue_concurrency = 1
    with session() as s:
        s.add(Playlist(name="P", state="running"))
        s.commit()
        pid = s.exec(select(Playlist)).first().id
    queueing.add("rg-1", playlist_id=pid)
    queueing.add("rg-2", playlist_id=pid)
    await queueing.tick(settings)
    assert world["search"] == ["rg-1"]  # one search at a time
    await queueing.tick(settings)
    assert world["search"] == ["rg-1"]
    finish_search("rg-1", [cand("alice")])
    await queueing.tick(settings)  # rg-1 picked; with no search running, rg-2's starts in the same tick
    assert item("rg-1").state == "downloading" and world["search"] == ["rg-1", "rg-2"]
    finish_search("rg-2", [cand("bob")])
    await queueing.tick(settings)
    await queueing.tick(settings)
    assert item("rg-2").state == "searched"  # one download at a time
    queueing.set_playlist_state(pid, "paused")
    set_download(item("rg-1").download_id, state="completed")
    imp = add_import(item("rg-1").download_id, state="imported")
    await queueing.tick(settings)
    await queueing.tick(settings)
    assert item("rg-1").state == "done" and item("rg-2").state == "searched"  # paused: nothing new starts
    queueing.set_playlist_state(pid, "running")
    await queueing.tick(settings)
    assert item("rg-2").state == "downloading"
    queueing.set_playlist_state(pid, "cancelled")
    with session() as s:
        assert s.get(Playlist, pid).state == "done"
    assert item("rg-2").state == "downloading"  # a running download finishes


async def test_search_gap(world, settings, monkeypatch):
    monkeypatch.setattr(queueing, "SEARCH_GAP", timedelta(minutes=5))
    queueing._last_search_end = utcnow()
    queueing.add("rg-1")
    await queueing.tick(settings)
    assert world["search"] == []
    queueing._last_search_end = utcnow() - timedelta(minutes=6)
    await queueing.tick(settings)
    assert world["search"] == ["rg-1"]


# Runs from a playlist ----------------------------------------------------------------------------

async def test_start_run_from_a_playlist(world, settings, monkeypatch):
    async def no_owned(rgs, _settings):
        return {}

    from picker import owned

    monkeypatch.setattr(owned, "owned_release_groups", no_owned)
    with session() as s:
        playlist = Playlist(name="Mix", state="review")
        s.add(playlist)
        s.commit()
        s.refresh(playlist)
        s.add(PlaylistTrack(playlist_id=playlist.id, position=1, title="Airbag", artist="Radiohead", album="OK Computer",
                            rg_mbid="rg-1", recording_mbid="rec-a", resolution="isrc"))
        s.add(PlaylistTrack(playlist_id=playlist.id, position=2, title="Karma Police", artist="Radiohead", album="OK Computer",
                            rg_mbid="rg-1", recording_mbid="rec-k", resolution="isrc", owned_path="/music/x.flac", owned_how="recording"))
        s.add(PlaylistTrack(playlist_id=playlist.id, position=3, title="Idioteque", artist="Radiohead", album="Kid A",
                            rg_mbid="rg-2", recording_mbid="rec-i", resolution="isrc"))
        s.add(PlaylistAlbum(playlist_id=playlist.id, rg_mbid="rg-2", mode="skip"))
        s.commit()
    created = await queueing.start_run(playlist.id, settings)
    assert created == 1
    it = item("rg-1")
    assert it.source == "playlist" and it.playlist_id == playlist.id and it.wanted == ["rec-a"] and it.mode == "full"
    assert item("rg-2") is None
    with session() as s:
        assert s.get(Playlist, playlist.id).state == "running"
        tracks = s.exec(select(PlaylistTrack).order_by(PlaylistTrack.position)).all()
        assert [t.queue_item_id for t in tracks] == [it.id, it.id, None]
        assert s.exec(select(PlaylistAlbum).where(PlaylistAlbum.rg_mbid == "rg-1")).first().queue_item_id == it.id
    run = queueing.summary(playlist.id)
    assert run.counts == {"queued": 1} and run.open == 1 and run.albums["rg-1"].title == "OK Computer"
    queueing.skip(it.id)
    await queueing.tick(settings)
    with session() as s:
        assert s.get(Playlist, playlist.id).state == "done"


# Hooks into the import pipeline --------------------------------------------------------------------

def test_exact_match_beats_different_tags(world, settings):
    settings.auto_import, settings.transcode_check, settings.rip_verify = True, False, False
    with session() as s:
        exact = cand("alice")
        exact.search_id = 1
        weak = cand("bob", tier="High", signals=("durations",))
        weak.search_id = 1
        s.add_all([exact, weak])
        s.commit()
        s.refresh(exact)
        s.refresh(weak)
        d1 = Download(candidate_id=exact.id, search_id=1, rg_mbid="rg-1", username="alice", folder="f", destination="p/1")
        d2 = Download(candidate_id=weak.id, search_id=1, rg_mbid="rg-1", username="bob", folder="f", destination="p/2")
        s.add_all([d1, d2])
        s.commit()
        s.refresh(d1)
        s.refresh(d2)
    different = {"kind": "different", "detail": "tags name the 2009 remaster"}
    assert importing.review_reason(Import(download_id=d1.id, rg_mbid="rg-1", release_mbid="gb", tag_check=different), settings) is None
    assert importing.review_reason(Import(download_id=d2.id, rg_mbid="rg-1", release_mbid="gb", tag_check=different), settings) \
        == "the files are tagged as a different edition"
    assert importing.review_reason(Import(rg_mbid="rg-1", release_mbid="gb", tag_check=different), settings) \
        == "the files are tagged as a different edition"  # Lidarr's own imports have no folder match
    mixed = {"kind": "mixed", "detail": ""}
    assert importing.review_reason(Import(download_id=d1.id, rg_mbid="rg-1", release_mbid="gb", tag_check=mixed), settings) \
        == "the files are tagged as several different releases"


def test_run_imports_add_artists_with_new_items_none(world, settings):
    settings.lidarr_monitor_new_items = "new"
    with session() as s:
        dl = Download(candidate_id=1, search_id=1, rg_mbid="rg-1", username="a", folder="f", destination="p/1")
        s.add(dl)
        s.commit()
        s.refresh(dl)
        s.add(QueueItem(rg_mbid="rg-1", source="playlist", playlist_id=1, download_id=dl.id, state="downloading"))
        s.commit()
    assert importing.new_items_policy(Import(download_id=dl.id, rg_mbid="rg-1"), settings) == "none"
    assert importing.new_items_policy(Import(download_id=999, rg_mbid="rg-1"), settings) == "new"
    assert importing.new_items_policy(Import(rg_mbid="rg-1"), settings) == "new"


# Pages ---------------------------------------------------------------------------------------------

def test_queue_pages(world, settings, monkeypatch):
    async def ensure_album(rg):
        with session() as s:
            return s.get(Album, rg)

    from picker import albums as albums_module

    monkeypatch.setattr(albums_module, "ensure_album", ensure_album)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    assert "Nothing queued yet" in client.get("/queue").text
    response = client.post("/queue/add", data={"rg_mbid": ["rg-1", "rg-2"], "source": "stuck", "back": "/queue"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/queue"
    html = client.get("/queue").text
    assert "OK Computer" in html and "Kid A" in html and "stuck" in html
    it = item("rg-1")
    assert client.post(f"/queue/{it.id}/skip", follow_redirects=False).status_code == 303
    assert item("rg-1").state == "skipped"
    assert client.post(f"/queue/{it.id}/retry", follow_redirects=False).status_code == 303
    with session() as s:
        assert s.get(QueueItem, it.id).state == "queued"
        playlist = Playlist(name="P", state="running")
        s.add(playlist)
        s.commit()
        s.refresh(playlist)
        s.add(QueueItem(rg_mbid="rg-2", playlist_id=playlist.id, state="needs_pick", note="best folder is Medium"))
        s.commit()
    panel = client.get(f"/playlists/{playlist.id}/run").text
    assert "Waiting for you" in panel and "Kid A" in panel and "best folder is Medium" in panel and "Pause" in panel
    assert client.post(f"/playlists/{playlist.id}/run/pause", follow_redirects=False).status_code == 303
    with session() as s:
        assert s.get(Playlist, playlist.id).state == "paused"
    assert "Resume" in client.get(f"/playlists/{playlist.id}/run").text
    assert client.post(f"/playlists/{playlist.id}/run/cancel", follow_redirects=False).status_code == 303
    with session() as s:
        assert s.get(Playlist, playlist.id).state == "done"
        assert s.exec(select(QueueItem).where(QueueItem.rg_mbid == "rg-2", QueueItem.playlist_id == playlist.id)).first().state == "cancelled"
