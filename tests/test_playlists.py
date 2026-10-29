"""Playlists (picker/playlists.py): the CSV, resolution through a fake MusicBrainz, album choice, the review page."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from picker import library, owned, playlists
from picker.db import session
from picker.models import Album, Job, Playlist, PlaylistAlbum, PlaylistTrack
from picker.web import router
from tests.test_library import make_flac

CSV = """﻿Track URI,Track Name,Album Name,Artist Name(s),Release Date,Duration (ms),ISRC,Album Artist Name(s)
spotify:track:1,Smells Like Teen Spirit,Nevermind (Remastered),Nirvana,1991-09-26,301000,USGF19942501,Nirvana
spotify:track:2,Come as You Are,Nevermind (Remastered),Nirvana,1991-09-26,219000,USGF19942502,Nirvana
spotify:track:3,Get Lucky,Random Access Memories,"Daft Punk, Pharrell Williams, Nile Rodgers",2013-05-17,369000,USQX91300108,Daft Punk
spotify:track:4,Obscure Song,Some Album,Nobody,2001-01-01,200000,,Nobody
spotify:track:5,Textual,Found By Name,Textband,1999,180000,,Textband
"""

NEVERMIND, NIRVANA_COMP, SLTS_SINGLE = "rg-nevermind", "rg-nirvana-comp", "rg-slts-single"
RAM, PIECE = "rg-ram", "rg-piece"


def release(rg_id, title, primary="Album", secondary=(), date=None, tracks=12, artist="Nirvana", artist_id="ar-nirvana"):
    return {
        "id": f"rel-{rg_id}-{date}", "title": title, "status": "Official", "date": date, "track-count": tracks,
        "artist-credit": [{"name": artist, "artist": {"id": artist_id}}],
        "release-group": {"id": rg_id, "title": title, "primary-type": primary, "secondary-types": list(secondary)},
    }


def recording(rec_id, title, isrc, length, releases, artist="Nirvana", score=100):
    return {"id": rec_id, "title": title, "isrcs": [isrc] if isrc else [], "length": length, "score": score,
            "artist-credit": [{"name": artist, "artist": {"id": "ar"}}], "releases": releases}


SLTS = recording("rec-slts", "Smells Like Teen Spirit", "USGF19942501", 301133, [
    release(NIRVANA_COMP, "Nirvana", secondary=["Compilation"], date="2002-10-29", tracks=14),
    release(NEVERMIND, "Nevermind", date="1991-09-24", tracks=12),
    release(NEVERMIND, "Nevermind", date="2011-09-26", tracks=39),  # the anniversary reissue, same group
    release(SLTS_SINGLE, "Smells Like Teen Spirit", primary="Single", date="1991-09-10", tracks=4),
])
CAYA = recording("rec-caya", "Come as You Are", "USGF19942502", 219000, [
    release(NEVERMIND, "Nevermind", date="1991-09-24", tracks=12),
])
LUCKY = recording("rec-lucky", "Get Lucky", "USQX91300108", 369000, [
    release(PIECE, "Piece by Piece", secondary=["Compilation", "Soundtrack"], date="2025-01-10", tracks=21, artist="Various Artists", artist_id="ar-va"),
    release(RAM, "Random Access Memories", date="2013-05-17", tracks=13, artist="Daft Punk", artist_id="ar-dp"),
], artist="Daft Punk")
TEXTUAL = recording("rec-textual", "Textual", None, 181000, [release("rg-textual", "Found By Name", date="1999-03-01", tracks=10, artist="Textband")],
                    artist="Textband", score=100)
TEXTUAL_LIVE = recording("rec-textual-live", "Textual", None, 240000, [release("rg-textual-live", "Live!", secondary=["Live"], date="2005", artist="Textband")],
                         artist="Textband", score=100)


# MusicBrainz keeps one recording per remaster; the one carrying this ISRC is only on a compilation.
VOYAGE = recording("rec-voyage", "Maiden Voyage (remastered 1999/Rudy Van Gelder Edition)", "USBN20400123", 474000, [
    release("rg-cozy", "Cozy Coffee Shop", secondary=["Compilation"], date="2025-03-01", tracks=43, artist="Various Artists", artist_id="ar-va"),
], artist="Herbie Hancock")
VOYAGE_GROUPS = [
    {"id": "rg-takin", "title": "Takin' Off / Maiden Voyage", "primary-type": "Album", "secondary-types": ["Compilation"],
     "first-release-date": "1996", "artist-credit": [{"name": "Herbie Hancock", "artist": {"id": "ar-hh"}}]},
    {"id": "rg-voyage", "title": "Maiden Voyage", "primary-type": "Album", "first-release-date": "1965-05-17",
     "artist-credit": [{"name": "Herbie Hancock", "artist": {"id": "ar-hh"}}]},
    {"id": "rg-other-voyage", "title": "Maiden Voyage", "primary-type": "Album", "first-release-date": "2015",
     "artist-credit": [{"name": "Simone Quatrana", "artist": {"id": "ar-sq"}}]},
]


class FakeMB:
    def __init__(self):
        self.isrc_calls, self.text_calls, self.group_calls = [], [], []

    async def recordings_by_isrcs(self, isrcs):
        self.isrc_calls.append(list(isrcs))
        return [r for r in (SLTS, CAYA, LUCKY, VOYAGE) if r["isrcs"][0] in isrcs]

    async def search_recordings(self, artist, title, duration_ms=None, limit=10):
        self.text_calls.append((artist, title, duration_ms))
        if title == "Textual":
            return [TEXTUAL_LIVE, TEXTUAL]  # the live one first: ranking must prefer the right length
        return []

    async def search_release_groups(self, artist, album, limit=25):
        self.group_calls.append((artist, album))
        if album == "Maiden Voyage":
            return VOYAGE_GROUPS
        return [{"id": "rg-manual", "title": album, "artist-credit": [{"name": artist}], "primary-type": "Album",
                 "first-release-date": "2001-01-01"}]


@pytest.fixture
def mb(monkeypatch):
    fake = FakeMB()

    class Services:
        pass

    Services.mb = fake
    Services.lidarr = None
    monkeypatch.setattr(playlists, "services", lambda: Services())
    monkeypatch.setattr(owned, "services", lambda: Services())

    async def no_beets(_settings):
        return {}

    monkeypatch.setattr(owned, "_beets_albums", no_beets)
    return fake


def tracks_of(playlist_id):
    with session() as s:
        return s.exec(select(PlaylistTrack).where(PlaylistTrack.playlist_id == playlist_id).order_by(PlaylistTrack.position)).all()


async def resolved_playlist(settings, mb, text=CSV, name="Mix"):
    playlist = playlists.create_playlist(name, text)
    with session() as s:
        job = s.exec(select(Job).where(Job.kind == "resolve")).first()
    await playlists.run_resolve(job)
    return playlist


# CSV ---------------------------------------------------------------------------------------

def test_parse_exportify_csv():
    rows = playlists.parse_csv(CSV)
    assert len(rows) == 5
    first = rows[0]
    assert first["position"] == 1 and first["title"] == "Smells Like Teen Spirit" and first["isrc"] == "USGF19942501"
    assert first["duration_ms"] == 301000 and first["album_artist"] == "Nirvana" and first["uri"] == "spotify:track:1"
    assert rows[2]["artist"] == "Daft Punk, Pharrell Williams, Nile Rodgers"
    assert rows[3]["isrc"] is None


def test_parse_csv_other_shapes():
    rows = playlists.parse_csv("Title;Artist;Album;Duration\nSong;Band;LP;3:25\n;;;\n")
    assert rows == [{"position": 1, "title": "Song", "artist": "Band", "album_artist": None, "album": "LP", "isrc": None,
                     "duration_ms": 205000, "uri": None, "release_date": None}]
    assert playlists.parse_csv("name,artist,album,duration\nS,B,A,200\n")[0]["duration_ms"] == 200000  # seconds
    with pytest.raises(ValueError, match="no column for album"):
        playlists.parse_csv("Track Name,Artist Name(s)\nS,B\n")
    with pytest.raises(ValueError, match="no rows"):
        playlists.parse_csv("Track Name,Artist Name(s),Album Name\n")


def test_first_artist():
    assert playlists.first_artist("Daft Punk, Pharrell Williams") == "Daft Punk"
    assert playlists.first_artist("Nirvana") == "Nirvana"


# Album choice --------------------------------------------------------------------------------

def test_options_and_choice():
    options = playlists.options_from_recording(SLTS)
    by_id = {o.rg_mbid: o for o in options}
    assert by_id[NEVERMIND].kind == "album" and by_id[NEVERMIND].date == "1991-09-24" and by_id[NEVERMIND].track_count == 12
    assert by_id[NEVERMIND].release_count == 2
    assert by_id[NIRVANA_COMP].kind == "compilation" and by_id[SLTS_SINGLE].kind == "single"
    assert [o.rg_mbid for o in options][:2] == [SLTS_SINGLE, NEVERMIND]  # earliest first

    # The CSV names the album (with a bracketed suffix): that album.
    assert playlists.choose_album(options, "Nevermind (Remastered)", True).rg_mbid == NEVERMIND
    # The CSV names the compilation: the studio album wins when preferred, the compilation otherwise.
    assert playlists.choose_album(options, "Nirvana", True).rg_mbid == NEVERMIND
    assert playlists.choose_album(options, "Nirvana", False).rg_mbid == NIRVANA_COMP
    # Unknown album name: earliest studio album.
    assert playlists.choose_album(options, "Something Else", False).rg_mbid == NEVERMIND
    assert playlists.single_for(options, "Smells Like Teen Spirit").rg_mbid == SLTS_SINGLE
    assert playlists.single_for(playlists.options_from_recording(CAYA), "Come as You Are") is None

    lucky = playlists.options_from_recording(LUCKY)
    assert playlists.choose_album(lucky, "Random Access Memories", True).rg_mbid == RAM
    assert playlists.choose_album(lucky, "Piece by Piece", True).rg_mbid == RAM  # soundtrack compilation → studio album
    assert playlists.choose_album(lucky, "Piece by Piece", False).rg_mbid == PIECE


def test_classify():
    assert playlists.classify("Album", []) == "album"
    assert playlists.classify("Album", ["Compilation"]) == "compilation"
    assert playlists.classify("Album", ["Live"]) == "live"
    assert playlists.classify("Single", []) == "single"
    assert playlists.classify("EP", []) == "ep"
    assert playlists.classify("Album", ["Demo"]) == "other"
    assert playlists.classify(None, []) == "other"


def test_rank_text_hits_prefers_the_right_length_and_artist():
    rec, how = playlists.rank_text_hits([TEXTUAL_LIVE, TEXTUAL], "Textband", "Textual", 180000)
    assert rec["id"] == "rec-textual" and how == "text-strong"
    rec, how = playlists.rank_text_hits([TEXTUAL_LIVE, TEXTUAL], "Someone Else", "Textual", 180000)
    assert rec is not None and how == "text-weak"
    weak = {**TEXTUAL, "score": 60}
    assert playlists.rank_text_hits([weak], "Textband", "Textual", None) == (None, "none")
    no_releases = {**TEXTUAL, "releases": []}
    assert playlists.rank_text_hits([no_releases], "Textband", "Textual", None) == (None, "none")


# Resolution ----------------------------------------------------------------------------------

async def test_resolve_by_isrc_then_text(settings, mb):
    playlist = await resolved_playlist(settings, mb)
    assert mb.isrc_calls == [["USGF19942501", "USGF19942502", "USQX91300108"]]  # one batched request
    assert [c[1] for c in mb.text_calls] == ["Obscure Song", "Textual"]
    assert mb.text_calls[1][0] == "Textband"
    tracks = tracks_of(playlist.id)
    assert [t.resolution for t in tracks] == ["isrc", "isrc", "isrc", "none", "text-strong"]
    assert tracks[0].rg_mbid == NEVERMIND and tracks[0].single_rg_mbid == SLTS_SINGLE and tracks[0].recording_mbid == "rec-slts"
    assert tracks[1].rg_mbid == NEVERMIND and tracks[1].single_rg_mbid is None
    assert tracks[2].rg_mbid == RAM
    assert tracks[4].rg_mbid == "rg-textual"
    with session() as s:
        playlist = s.get(Playlist, playlist.id)
        albums = {a.rg_mbid: a for a in s.exec(select(Album))}
    assert playlist.state == "review" and playlist.resolved == 5
    assert albums[NEVERMIND].artist == "Nirvana" and albums[NEVERMIND].min_track_count == 12
    assert albums[NEVERMIND].first_release_date == "1991-09-24" and albums[RAM].artist == "Daft Punk"
    assert SLTS_SINGLE in albums and NIRVANA_COMP not in albums  # only chosen albums and singles get rows


async def test_resolve_marks_owned_tracks(settings, mb):
    folder = settings.music_dir / "Nirvana" / "Nevermind (1991)"
    make_flac(folder / "01 Smells Like Teen Spirit.flac", 1, seconds=301, title="Smells Like Teen Spirit", artist="Nirvana",
              musicbrainz_trackid="99999999-0000-0000-0000-000000000001")
    make_flac(folder / "03 Come as You Are.flac", 3, seconds=219, title="Come as You Are", artist="Nirvana")
    library.scan(settings.music_dir, settings)
    # The first file carries a recording ID that is not SLTS's: owned only by name.
    playlist = await resolved_playlist(settings, mb)
    tracks = tracks_of(playlist.id)
    assert tracks[0].owned_how == "name" and tracks[0].owned_path.endswith("01 Smells Like Teen Spirit.flac")
    assert tracks[1].owned_how == "name"
    assert tracks[2].owned_path is None
    playlists.toggle_track(tracks[0].id, "owned")  # "wrong?"
    t0 = tracks_of(playlist.id)[0]
    assert t0.owned_unticked and t0.owned_path is None
    playlists.toggle_track(tracks[0].id, "owned")
    assert tracks_of(playlist.id)[0].owned_path is not None


# Review ----------------------------------------------------------------------------------------

async def test_review_groups_modes_and_toggles(settings, mb):
    playlist = await resolved_playlist(settings, mb)
    r = await playlists.review(playlist.id, settings)
    assert [g.rg_mbid for g in r.groups] == [NEVERMIND, RAM, "rg-textual"]
    assert [len(g.tracks) for g in r.groups] == [2, 1, 1]
    assert [g.mode for g in r.groups] == ["full", "full", "full"]
    assert r.groups[0].estimate_mb == 12 * 30 and r.total_mb == (12 + 13 + 10) * 30
    assert [t.position for t in r.attention] == [4]
    assert r.counts == {"full": 3, "tracks": 0, "tracks_plus": 0, "upgrade": 0, "skip": 0, "owned": 0}

    # Tracks-only: Smells Like Teen Spirit moves to its single (in full), Come as You Are stays tracks-only.
    playlists.set_options(playlist.id, full_albums=False)
    r = await playlists.review(playlist.id, settings)
    assert [g.rg_mbid for g in r.groups] == [SLTS_SINGLE, NEVERMIND, RAM, "rg-textual"]
    assert [g.mode for g in r.groups] == ["full", "tracks", "tracks", "tracks"]
    assert r.groups[1].estimate_mb == 30 and r.groups[0].is_single

    # The row can insist on the album even with the toggle off; per-album modes override the toggle.
    playlists.set_track_album(tracks_of(playlist.id)[0].id, NEVERMIND)
    playlists.set_mode(playlist.id, RAM, "full")
    r = await playlists.review(playlist.id, settings)
    assert [g.rg_mbid for g in r.groups] == [NEVERMIND, RAM, "rg-textual"]
    assert [g.mode for g in r.groups] == ["tracks", "full", "tracks"]
    playlists.set_mode(playlist.id, RAM, None)
    r = await playlists.review(playlist.id, settings)
    assert r.groups[1].mode == "tracks"
    with pytest.raises(ValueError):
        playlists.set_mode(playlist.id, RAM, "bogus")

    # Dropping a row removes its album when it was the only one.
    textual = tracks_of(playlist.id)[4]
    playlists.toggle_track(textual.id, "drop")
    r = await playlists.review(playlist.id, settings)
    assert [g.rg_mbid for g in r.groups] == [NEVERMIND, RAM]
    assert [t.position for t in r.attention] == [4]


async def test_prefer_studio_toggle_rechooses(settings, mb):
    text = CSV.replace("Nevermind (Remastered)", "Nirvana")  # the CSV names the compilation
    playlist = await resolved_playlist(settings, mb, text)
    assert tracks_of(playlist.id)[0].rg_mbid == NEVERMIND
    playlists.set_options(playlist.id, prefer_studio=False)
    assert tracks_of(playlist.id)[0].rg_mbid == NIRVANA_COMP
    playlists.set_options(playlist.id, prefer_studio=True)
    assert tracks_of(playlist.id)[0].rg_mbid == NEVERMIND


async def test_owned_album_defaults_to_skip(settings, mb):
    folder = settings.music_dir / "Daft Punk" / "Random Access Memories (2013)"
    for n in range(1, 4):
        make_flac(folder / f"{n:02d}.flac", n, musicbrainz_albumid="11111111-2222-3333-4444-555555555555",
                  musicbrainz_releasegroupid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    library.scan(settings.music_dir, settings)
    ram_real = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    LUCKY["releases"][1]["release-group"]["id"] = ram_real
    try:
        playlist = await resolved_playlist(settings, mb)
    finally:
        LUCKY["releases"][1]["release-group"]["id"] = RAM
    r = await playlists.review(playlist.id, settings)
    group = next(g for g in r.groups if g.rg_mbid == ram_real)
    # Three files of a thirteen-track album, and the playlist wants a fourth: a partial copy, replaced whole by default.
    assert group.owned and group.owned.source == "index" and group.partial_copy and group.mode == "upgrade"
    assert r.counts["owned"] == 1 and r.counts["upgrade"] == 1 and r.counts["skip"] == 0
    playlists.set_mode(playlist.id, ram_real, "skip")
    r = await playlists.review(playlist.id, settings)
    assert next(g for g in r.groups if g.rg_mbid == ram_real).mode == "skip"
    # A complete copy is simply owned.
    with session() as s:
        album = s.get(Album, ram_real)
        album.min_track_count = 3
        s.add(album)
        s.commit()
    playlists.set_mode(playlist.id, ram_real, None)
    r = await playlists.review(playlist.id, settings)
    group = next(g for g in r.groups if g.rg_mbid == ram_real)
    assert not group.partial_copy and group.mode == "skip"


async def test_manual_resolution_and_delete(settings, mb):
    playlist = await resolved_playlist(settings, mb)
    obscure = tracks_of(playlist.id)[3]
    with session() as s:
        s.add(Album(rg_mbid="rg-manual", artist="Nobody", title="Some Album"))
        s.commit()
    playlists.set_track_manual(obscure.id, "rg-manual")
    r = await playlists.review(playlist.id, settings)
    assert "rg-manual" in [g.rg_mbid for g in r.groups] and r.attention == []
    assert tracks_of(playlist.id)[3].resolution == "manual"
    playlists.set_options(playlist.id, prefer_studio=False)  # rechoose leaves manual rows alone
    assert tracks_of(playlist.id)[3].rg_mbid == "rg-manual"
    assert playlists.all_playlists()[0][1] == 5
    playlists.delete_playlist(playlist.id)
    assert playlists.all_playlists() == []
    with session() as s:
        assert s.exec(select(PlaylistTrack)).all() == [] and s.exec(select(PlaylistAlbum)).all() == []


def test_queue_resolve_dedupes(settings):
    playlist = playlists.create_playlist("P", "Track Name,Artist Name(s),Album Name\nS,B,A\n")
    assert playlists.queue_resolve(playlist.id) is None
    with session() as s:
        assert len(s.exec(select(Job).where(Job.kind == "resolve")).all()) == 1


# Web -------------------------------------------------------------------------------------------

@pytest.fixture
def client(settings, mb, monkeypatch):
    from picker import web

    monkeypatch.setattr(web, "services", playlists.services)  # the same fake MusicBrainz for the find route
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


async def test_pages(client, settings, mb):
    assert "Upload a playlist" in client.get("/playlists").text
    response = client.post("/playlists", files={"file": ("mix.csv", CSV.encode("utf-8"), "text/csv")}, follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    html = client.get(location).text
    assert "resolving" in html and "0 of 5 rows" in html and 'hx-trigger="every 3s"' in html
    with session() as s:
        job = s.exec(select(Job).where(Job.kind == "resolve")).first()
    await playlists.run_resolve(job)
    html = client.get(location).text
    assert "Nirvana — Nevermind" in html and "Daft Punk — Random Access Memories" in html
    assert "Needs attention" in html and "Obscure Song" in html
    assert "3 albums" in html and 'hx-trigger="every 3s"' not in html
    playlist_id = int(location.rsplit("/", 1)[1])
    # HTMX actions answer with the partial; plain forms get redirected.
    partial = client.post(f"/playlists/{playlist_id}/options", data={"full_albums": "0"}, headers={"HX-Request": "true"}).text
    assert "Only these 1" in partial and "<html" not in partial
    assert client.post(f"/playlists/{playlist_id}/options", data={"full_albums": "1", "prefer_studio_album": "1"},
                       follow_redirects=False).status_code == 303
    rg_mode = client.post(f"/playlists/{playlist_id}/albums/{RAM}/mode", data={"mode": "skip"}, headers={"HX-Request": "true"})
    assert rg_mode.status_code == 200 and "1 skipped" in rg_mode.text
    assert client.post(f"/playlists/{playlist_id}/albums/{RAM}/mode", data={"mode": "bogus"}).status_code == 400
    obscure = tracks_of(playlist_id)[3]
    found = client.post(f"/playlists/{playlist_id}/tracks/{obscure.id}/find", data={"artist": "Nobody", "album": "Some Album"}).text
    assert "Nobody — Some Album" in found and "Use" in found
    bad = client.post("/playlists", files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")})
    assert "Could not read the CSV" in bad.text
    assert client.post(f"/playlists/{playlist_id}/delete", follow_redirects=False).status_code == 303
    assert client.get(f"/playlists/{playlist_id}").status_code == 404


async def test_a_crashed_resolve_leaves_the_playlist_in_review_with_an_error(settings, mb):
    import pytest

    from picker.clients.http import ServiceError

    async def boom(isrcs):
        raise ServiceError("musicbrainz", "503 service unavailable")

    mb.recordings_by_isrcs = boom
    playlist = playlists.create_playlist("Mix", CSV)
    with session() as s:
        job = s.exec(select(Job).where(Job.kind == "resolve")).first()
    with pytest.raises(ServiceError):
        await playlists.run_resolve(job)
    with session() as s:
        playlist = s.get(Playlist, playlist.id)
    assert playlist.state == "review" and "Resolving stopped" in playlist.error
    assert all(t.resolution is None for t in tracks_of(playlist.id))  # untried, so Resolve the untried rows retries them
    r = await playlists.review(playlist.id, settings)
    assert r.resolving  # the Start button stays disabled until the rows are resolved or dropped


def test_album_key_drops_edition_suffixes():
    assert playlists.album_key("Maiden Voyage - Remastered 1999/Rudy Van Gelder Edition") == "maiden voyage"
    assert playlists.album_key("Nevermind (Remastered)") == "nevermind"
    assert playlists.album_key("Abbey Road (Super Deluxe Edition)") == "abbey road"
    assert playlists.album_key("Takin' Off / Maiden Voyage") == "takin off maiden voyage"
    assert playlists.album_key("Monika / Zabawa w ciemności") == "monika zabawa w ciemnosci"  # not an edition suffix


async def test_a_recording_only_on_compilations_finds_the_named_studio_album(settings, mb):
    text = CSV + "spotify:track:7,Maiden Voyage - Remastered 1999/Rudy Van Gelder Edition,Maiden Voyage - Remastered 1999/Rudy Van Gelder Edition,Herbie Hancock,1999-01-01,474000,USBN20400123,Herbie Hancock\n"
    playlist = await resolved_playlist(settings, mb, text=text)
    assert mb.group_calls == [("Herbie Hancock", "Maiden Voyage")]  # one lookup, for the one row that needed it
    voyage = [t for t in tracks_of(playlist.id) if t.title.startswith("Maiden")][0]
    assert voyage.resolution == "isrc" and voyage.rg_mbid == "rg-voyage"  # the 1965 album, not the compilation or the other artist's
    assert [o["rg_mbid"] for o in voyage.options] == ["rg-cozy", "rg-voyage"]
    r = await playlists.review(playlist.id, settings)
    group = [g for g in r.groups if g.rg_mbid == "rg-voyage"][0]
    assert group.album.artist == "Herbie Hancock" and group.album.first_release_date == "1965-05-17"
    assert not [g for g in r.groups if g.rg_mbid == "rg-cozy"]
    # The compilation stays available in the row's From list, and picking it by hand sticks.
    playlists.set_track_album(voyage.id, "rg-cozy")
    r = await playlists.review(playlist.id, settings)
    assert [g.rg_mbid for g in r.groups if g.rg_mbid in ("rg-cozy", "rg-voyage")] == ["rg-cozy"]


async def test_resolve_all_rows_again_keeps_hand_fixes_and_modes(settings, mb):
    playlist = await resolved_playlist(settings, mb)
    before = {t.title: t for t in tracks_of(playlist.id)}
    playlists.set_mode(playlist.id, RAM, "skip")
    playlists.set_track_manual(before["Textual"].id, "rg-manual")
    playlists.toggle_track(before["Come as You Are"].id, "drop")
    mb.isrc_calls.clear()
    assert playlists.reset_resolution(playlist.id) == 3  # Smells Like Teen Spirit, Get Lucky, Obscure Song; not the manual or dropped row
    rows = {t.title: t for t in tracks_of(playlist.id)}
    assert rows["Smells Like Teen Spirit"].resolution is None and rows["Smells Like Teen Spirit"].rg_mbid is None
    assert rows["Textual"].resolution == "manual" and rows["Textual"].rg_mbid == "rg-manual"
    assert rows["Come as You Are"].dropped and rows["Come as You Are"].rg_mbid == NEVERMIND
    with session() as s:
        assert s.get(Playlist, playlist.id).state == "resolving"
        job = s.exec(select(Job).where(Job.kind == "resolve", Job.status == "queued")).first()
    await playlists.run_resolve(job)
    rows = {t.title: t for t in tracks_of(playlist.id)}
    assert rows["Smells Like Teen Spirit"].resolution == "isrc" and rows["Get Lucky"].rg_mbid == RAM
    assert mb.isrc_calls == [["USGF19942501", "USQX91300108"]]
    r = await playlists.review(playlist.id, settings)
    assert not r.resolving and [g.mode for g in r.groups if g.rg_mbid == RAM] == ["skip"]  # the mode survived


def test_static_files_are_versioned():
    from picker import web

    assert len(web._static_version()) == 10
    page = web.templates.get_template("base.html").render(request=None)
    assert f"/static/style.css?v={web._static_version()}" in page
