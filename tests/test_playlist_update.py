"""Playlist Update (spec section 2): a new export of the same playlist keeps decisions, adds and removes rows."""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from picker import playlists, queueing
from picker.db import session
from picker.models import Job, Playlist, PlaylistAlbum, PlaylistTrack, QueueItem
from picker.web import router
from tests.test_playlists import CSV, NEVERMIND, RAM, mb, resolved_playlist, tracks_of  # noqa: F401  (fixture)

CSV_V2 = """Track URI,Track Name,Album Name,Artist Name(s),Release Date,Duration (ms),ISRC,Album Artist Name(s)
spotify:track:3,Get Lucky,Random Access Memories,"Daft Punk, Pharrell Williams, Nile Rodgers",2013-05-17,369000,USQX91300108,Daft Punk
spotify:track:1,Smells Like Teen Spirit,Nevermind (Remastered),Nirvana,1991-09-26,301000,USGF19942501,Nirvana
,Textual,Found By Name,Textband,1999,180000,,Textband
spotify:track:9,Brand New Song,Brand New Album,Newcomer,2026-01-01,200000,,Newcomer
"""


async def test_update_keeps_matches_and_choices(settings, mb):
    playlist = await resolved_playlist(settings, mb)
    before = {t.title: t for t in tracks_of(playlist.id)}
    playlists.set_track_album(before["Smells Like Teen Spirit"].id, NEVERMIND)  # a decision to keep
    playlists.set_mode(playlist.id, RAM, "skip")
    playlists.toggle_track(before["Textual"].id, "drop")
    result = playlists.update_playlist(playlist.id, CSV_V2)
    assert result == {"kept": 3, "added": 1, "removed": 2}  # Come as You Are and Obscure Song left the file
    after = tracks_of(playlist.id)
    assert [t.title for t in after] == ["Get Lucky", "Smells Like Teen Spirit", "Textual", "Brand New Song"]
    assert [t.position for t in after] == [1, 2, 3, 4]
    slts = after[1]
    assert slts.id == before["Smells Like Teen Spirit"].id and slts.use_single is False and slts.resolution == "isrc"
    assert after[2].id == before["Textual"].id and after[2].dropped  # matched by name (no URI, no ISRC) and still dropped
    assert after[3].resolution is None  # new: to be resolved
    with session() as s:
        assert s.exec(select(PlaylistAlbum).where(PlaylistAlbum.rg_mbid == RAM)).first().mode == "skip"
        jobs = s.exec(select(Job).where(Job.kind == "resolve", Job.status == "queued")).all()
    assert len(jobs) == 1  # the new row is resolved
    r = await playlists.review(playlist.id, settings)
    assert r.resolving and [t.title for t in r.attention] == ["Brand New Song"]


async def test_update_on_a_run_playlist_keeps_running_and_only_queues_new_albums(settings, mb, monkeypatch):
    playlist = await resolved_playlist(settings, mb)
    monkeypatch.setattr(queueing, "ensure_running", lambda: None)
    synced = []
    monkeypatch.setattr(queueing.plexsync, "queue_sync", lambda pid, *a, **k: synced.append(pid))
    monkeypatch.setattr(playlists, "queue_resolve", lambda pid: synced.append(("resolve", pid)))
    assert await queueing.start_run(playlist.id, settings) == 3
    with session() as s:
        for it in s.exec(select(QueueItem)):
            it.state = "done"
            s.add(it)
        s.commit()
    result = playlists.update_playlist(playlist.id, CSV_V2)
    assert result["added"] == 1 and result["removed"] == 2
    with session() as s:
        assert s.get(Playlist, playlist.id).state == "running"  # not back to review
    assert synced[-1] == playlist.id  # rows left: the Plex playlist and M3U8 are brought in step
    # The new row resolves to a new album; only that album gets a queue item.
    with session() as s:
        new = s.exec(select(PlaylistTrack).where(PlaylistTrack.title == "Brand New Song")).first()
        new.rg_mbid, new.recording_mbid, new.resolution = "rg-new", "rec-new", "manual"
        s.add(new)
        from picker.models import Album

        s.add(Album(rg_mbid="rg-new", artist="Newcomer", title="Brand New Album", min_track_count=9))
        s.commit()
    r = await playlists.review(playlist.id, settings)
    assert [g.rg_mbid for g in r.new_groups] == ["rg-new"] and not r.resolving
    assert await queueing.start_run(playlist.id, settings) == 1
    with session() as s:
        items = s.exec(select(QueueItem)).all()
    assert len(items) == 4 and sum(1 for i in items if i.state == "queued") == 1
    assert queueing.active_item("rg-new") is not None


async def test_upload_prompts_for_update_when_the_name_exists(settings, mb, monkeypatch):
    from picker import web

    monkeypatch.setattr(web, "services", playlists.services)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    first = client.post("/playlists", files={"file": ("mix.csv", CSV.encode("utf-8"), "text/csv")}, follow_redirects=False)
    playlist_id = int(first.headers["location"].rsplit("/", 1)[1])
    with session() as s:
        job = s.exec(select(Job).where(Job.kind == "resolve")).first()
    await playlists.run_resolve(job)

    prompt = client.post("/playlists", files={"file": ("mix.csv", CSV_V2.encode("utf-8"), "text/csv")})
    assert prompt.status_code == 200 and "is already here" in prompt.text and "Update it" in prompt.text
    token = prompt.text.split("/playlists/pending/")[1].split('"')[0]
    updated = client.post(f"/playlists/pending/{token}", data={"action": "update"}, follow_redirects=False)
    assert updated.status_code == 303 and updated.headers["location"].startswith(f"/playlists/{playlist_id}?kept=3")
    page = client.get(updated.headers["location"]).text
    assert "3 rows kept, 1 added, 2 removed" in page
    assert len(playlists.all_playlists()) == 1

    prompt = client.post("/playlists", files={"file": ("mix.csv", CSV.encode("utf-8"), "text/csv")})
    token = prompt.text.split("/playlists/pending/")[1].split('"')[0]
    made = client.post(f"/playlists/pending/{token}", data={"action": "new"}, follow_redirects=False)
    assert made.status_code == 303 and len(playlists.all_playlists()) == 2
    assert "expired" in client.post("/playlists/pending/nope", data={"action": "update"}).text
