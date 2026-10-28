"""Render the pages and partials through the real routes, against a seeded database."""

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from picker.db import init_db, session
from picker.models import Album, Candidate, Search
from picker.parsing import parse_candidate
from picker.web import router


@pytest.fixture
def client(settings, monkeypatch):
    from picker import albums

    async def no_editions(rg):  # never reach MusicBrainz from tests; with_editions overrides
        return []

    monkeypatch.setattr(albums, "editions", no_editions)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def seed():
    folder = r"m\Radiohead\OK Computer (1997) [2009 Remaster] [Japan TOCP-50201] [EAC]"
    files = [
        {"filename": rf"{folder}\{i:02d}.flac", "name": f"{i:02d}.flac", "size": 30_000_000, "kind": "audio",
         "disc": None, "in_scans": False, "bitDepth": 16, "sampleRate": 44100, "length": 200}
        for i in range(1, 13)
    ]
    parsed = parse_candidate([f["filename"] for f in files], files=files, artist="Radiohead", album="OK Computer",
                             labels=["Parlophone"]).to_dict()
    with session() as s:
        s.add(Album(rg_mbid="rg-1", artist="Radiohead", title="OK Computer", min_track_count=12))
        search = Search(rg_mbid="rg-1", state="done", queries=["radiohead ok computer"])
        s.add(search)
        s.commit()
        s.refresh(search)
        s.add(Candidate(search_id=search.id, username="alice", folder=folder, query="q", files=files,
                        audio_count=12, disc_counts=[12], total_size=360_000_000, audio_format="FLAC",
                        bit_depth=16, sample_rate=44100, sidecars={"log": 1}, has_free_slot=True, parsed=parsed))
        # A row stored before parsing existed must still render.
        s.add(Candidate(search_id=search.id, username="bob", folder=r"m\OKC", query="q", files=files,
                        audio_count=12, disc_counts=[12], total_size=1, audio_format="FLAC"))
        s.commit()


def test_candidates_partial_shows_badges_and_evidence(client):
    seed()
    html = client.get("/album/rg-1/candidates").text
    assert "TOCP-50201" in html
    assert "1997 → 2009" in html
    assert ">remaster<" in html
    assert ">JP<" in html
    assert ">EAC<" in html
    assert "“TOCP-50201” → catalog number candidate" in html  # hover evidence
    assert "class=\"evidence\"" in html  # expanded-row evidence table
    assert "bob" in html


@pytest.mark.parametrize("query", ["sort=years", "sort=format&rev=true", "source=unknown", "depth=16", "all=true&sort=size"])
def test_candidates_sort_and_filter_render(client, query):
    seed()
    resp = client.get(f"/album/rg-1/candidates?{query}")
    assert resp.status_code == 200
    assert "alice" in resp.text or "match these filters" in resp.text


def test_filter_excludes(client):
    seed()
    html = client.get("/album/rg-1/candidates?depth=24").text
    assert "match these filters" in html


def test_pages_render(client):
    seed()
    for path in ("/", "/downloads", "/settings"):
        assert client.get(path).status_code == 200, path


RELEASES = [
    {"id": "uk-cd", "title": "OK Computer", "date": "1997-05-21", "country": "GB", "barcode": "",
     "label-info": [{"label": {"name": "Parlophone"}, "catalog-number": "NODATA 02"}],
     "media": [{"position": 1, "format": "CD", "tracks": [{"length": 200_000}] * 12}]},
    {"id": "jp-cd", "title": "OK Computer", "date": "1997-05-28", "country": "JP", "barcode": "",
     "label-info": [{"label": {"name": "Toshiba-EMI"}, "catalog-number": "TOCP-50201"}],
     "media": [{"position": 1, "format": "CD", "tracks": [{"length": 200_000}] * 12}]},
    {"id": "rm", "title": "OK Computer", "date": "2009-03-24", "country": "GB", "barcode": "", "disambiguation": "remastered",
     "media": [{"position": 1, "format": "CD", "tracks": [{"length": 200_000}] * 12}]},
]


@pytest.fixture
def with_editions(monkeypatch):
    from picker import albums

    async def fake(rg):
        return RELEASES

    monkeypatch.setattr(albums, "editions", fake)


def set_match(username, **match):
    with session() as s:
        c = s.exec(select(Candidate).where(Candidate.username == username)).one()
        c.match = {"tier": "Unknown", "editions": [], "signals": [], "tracks_agree": False, **match}
        s.add(c)
        s.commit()
        return c.id


def test_match_column_and_confirm_form(client, with_editions):
    seed()
    alice = set_match("alice", tier="Exact", editions=["jp-cd"], matched_catalogs=["TOCP-50201"], tracks_agree=True)
    set_match("bob", tier="Medium", editions=["uk-cd", "jp-cd"])
    html = client.get("/album/rg-1/candidates").text
    assert ">Exact<" in html and "1997-05-28 · JP · CD · Toshiba-EMI · TOCP-50201" in html
    assert "one of 2" in html
    assert 'class="mono matched"' in html
    assert html.index("alice") < html.index("bob")  # tier first

    form = client.get(f"/candidates/{alice}/confirm").text
    assert '<option value="jp-cd" selected>' in form
    assert "✓ 1997-05-28" in form


def test_download_records_the_confirmed_edition(client, with_editions, monkeypatch):
    from picker import downloading, web
    seed()
    alice = set_match("alice", tier="Exact", editions=["jp-cd"])
    started = {}
    monkeypatch.setattr(web, "start_download", lambda c, rg, rel: started.update(rel=rel) or
                        type("D", (), {"state": "queued", "bytes_done": 0, "bytes_total": 1})())
    resp = client.post(f"/candidates/{alice}/download", data={"release_mbid": "jp-cd"})
    assert resp.status_code == 200 and started["rel"] == "jp-cd"
    assert downloading  # module imported for the patch target


def test_pinning_a_target_filters_the_table(client, with_editions):
    seed()
    set_match("alice", tier="Exact", editions=["jp-cd"])
    set_match("bob", tier="Medium", editions=["uk-cd"])
    panel = client.post("/album/rg-1/target/uk-cd")
    assert panel.headers["HX-Trigger"] == "candidates-reload"
    assert "★" in panel.text
    html = client.get("/album/rg-1/candidates").text
    assert "bob" in html and "alice" not in html
    assert "Showing only folders that could be the pinned edition" in html
    client.post("/album/rg-1/target/uk-cd")  # unpin
    assert "alice" in client.get("/album/rg-1/candidates").text


def test_editions_panel(client, with_editions):
    seed()
    html = client.get("/album/rg-1/editions").text
    assert "TOCP-50201" in html and "NODATA 02" in html and "☆" in html


def test_preferences_order_within_a_tier_and_preselect_the_edition(client, settings, with_editions):
    from picker.searching import ordered_candidates
    seed()
    set_match("alice", tier="High", editions=["rm"], earliest_date="2009-01-01")
    set_match("bob", tier="High", editions=["uk-cd", "jp-cd"], earliest_date="1997-05-21")
    with session() as s:
        search = s.exec(select(Search)).one()
        bob = s.exec(select(Candidate).where(Candidate.username == "bob")).one().id
    html = client.get("/album/rg-1/candidates").text
    assert html.index("bob") < html.index("alice")
    assert "pref -" in html and "− remaster" in html  # alice's folder says remaster: the badge explains
    # Preferences see the editions only when they are passed in.
    editions = {e.id: e for e in __import__("picker.editions", fromlist=["editions_from_releases"]).editions_from_releases(RELEASES)}
    assert [c.username for c in ordered_candidates(search, editions=editions)[0]] == ["bob", "alice"]
    # The confirm form preselects the preferred of bob's two possible editions (GB over JP) and warns on remasters.
    form = client.get(f"/candidates/{bob}/confirm").text
    assert 'value="uk-cd" selected' in form
    assert 'data-remaster="1"' in form and "remaster-warn\" hidden" in form
    settings.preferred_countries = "JP, GB"
    form = client.get(f"/candidates/{bob}/confirm").text
    assert 'value="jp-cd" selected' in form


def test_prefer_original_orders_within_a_tier(client, settings):
    from picker.searching import ordered_candidates
    seed()
    set_match("alice", tier="High", editions=["rm"], earliest_date="2009-01-01")
    set_match("bob", tier="High", editions=["orig"], earliest_date="1997-05-21")
    with session() as s:
        search = s.exec(select(Search)).one()
    # With the edition preferences silent, prefer_original alone decides the order.
    settings.avoid_remasters, settings.preferred_countries, settings.preferred_sources = False, "", ""
    settings.prefer_original = True
    assert [c.username for c in ordered_candidates(search)[0]] == ["bob", "alice"]
    settings.prefer_original = False  # then availability: alice has the free slot
    assert [c.username for c in ordered_candidates(search)[0]] == ["alice", "bob"]


@pytest.mark.parametrize("state, expect", [
    ("review", "Import</button>"),
    ("skipped", "Force apply"),
    ("blocked", "Retry"),
    ("imported", "In the library at"),
])
def test_import_review_page(client, with_editions, state, expect):
    from picker.models import Download, Import
    seed()
    with session() as s:
        dl = Download(candidate_id=1, search_id=1, rg_mbid="rg-1", release_mbid="jp-cd", username="alice",
                      folder="m", destination="picker/1", files=[])
        s.add(dl)
        s.commit()
        s.refresh(dl)
        imp = Import(download_id=dl.id, rg_mbid="rg-1", release_mbid="jp-cd", username="alice", state=state,
                     step="beets" if state != "review" else "review", source_dir="/dl/picker/1",
                     album_dir="/music/Radiohead/OK Computer (1997)" if state == "imported" else None,
                     integrity=[{"name": "01.flac", "ok": True, "detail": "200 s"}], spectrograms=["1.png"],
                     beets_output="Skipping." if state == "skipped" else None, notes=["a note"],
                     error="target exists" if state == "blocked" else None,
                     tags={"file": "01.flac", "error": None, "tags": {"label": "Toshiba-EMI", "catalognumber": "TOCP-50201",
                                                                      "musicbrainz_albumid": "jp-cd"}} if state == "imported" else None)
        s.add(imp)
        s.commit()
        s.refresh(imp)
    html = client.get(f"/imports/{imp.id}").text
    assert expect in html
    assert "1997-05-28 · JP · CD · Toshiba-EMI · TOCP-50201" in html
    if state == "imported":
        assert "tags match the chosen edition" in html and "CATALOGNUMBER" in html
        assert "tags match the chosen edition" in client.get("/history").text
    assert client.get("/history").status_code == 200
    downloads = client.get("/downloads").text
    assert f"/imports/{imp.id}" in downloads
    # Only file names recorded on the import are served.
    assert client.get(f"/imports/{imp.id}/spectrogram/picker.db").status_code == 404
    assert client.get(f"/imports/{imp.id}/spectrogram/..%2F..%2Fpicker.db").status_code == 404


@pytest.mark.parametrize("check, expect, absent", [
    ({"kind": "different", "detail": "the files are tagged as another edition of this album: dated 1997, not 1997",
      "tagged_mbid": "uk-cd", "in_group": True, "tags": {"musicbrainz_albumid": "uk-cd"}},
     ["The uploader's tags say otherwise", "1997-05-21 · GB · CD", 'value="uk-cd"', "Use that edition instead",
      "musicbrainz_albumid=uk-cd", "Waiting for you because the files are tagged as a different edition"], []),
    ({"kind": "different", "detail": "the files are tagged as a release that is not an edition of this album",
      "tagged_mbid": "0f1e2d3c-0000-0000-0000-000000000000", "in_group": False, "tags": {}},
     ["not an edition of this album", "Reject if the folder is not this album"], ["Use that edition instead"]),
    ({"kind": "mixed", "detail": "the files name 2 different releases between them", "tagged_mbid": None, "in_group": False,
      "tags": {}, "split": [{"mbid": "uk-cd", "count": 7}, {"mbid": "0f1e2d3c-0000-0000-0000-000000000000", "count": 3}]},
     ["more than one rip", "7 tracks tagged", "1997-05-21 · GB · CD", "3 tracks tagged", "not an edition of this album",
      "Waiting for you because the files are tagged as several different releases"], ["Use that edition instead"]),
    ({"kind": "sibling", "detail": "sister", "tagged_mbid": "uk-cd", "in_group": True, "tags": {}},
     ["a sister pressing of this edition", "1997-05-21 · GB · CD"], ["Use that edition instead", "say otherwise"]),
    ({"kind": "same", "detail": "same", "tagged_mbid": "jp-cd", "in_group": True, "tags": {}},
     ["already carry this edition"], ["say otherwise"]),
    ({"kind": "untagged", "detail": "nothing", "tagged_mbid": None, "in_group": False, "tags": {}},
     [], ["say otherwise", "sister pressing", "already carry"]),
])
def test_review_page_shows_the_tag_check(client, with_editions, settings, check, expect, absent):
    from picker.models import Import
    seed()
    settings.auto_import, settings.transcode_check = True, False
    with session() as s:
        imp = Import(rg_mbid="rg-1", release_mbid="jp-cd", username="alice", state="review", step="review",
                     source_dir="/dl/picker/1", tag_check=check)
        s.add(imp)
        s.commit()
        s.refresh(imp)
    html = client.get(f"/imports/{imp.id}").text
    for text in expect:
        assert text in html, text
    for text in absent:
        assert text not in html, text
    history = client.get("/history").text
    assert ("tags differ" in history) == (check["kind"] == "different")
    assert ("mixed rips" in history) == (check["kind"] == "mixed")


def test_missing_columns_are_added(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE candidate (id INTEGER PRIMARY KEY, search_id INTEGER, username VARCHAR)")
    con.commit()
    con.close()
    init_db(db)
    cols = {row[1] for row in sqlite3.connect(db).execute("PRAGMA table_info(candidate)")}
    assert {"parsed", "sidecars", "upload_speed"} <= cols


def test_album_page_and_results_say_when_you_already_have_it(client, with_editions, monkeypatch, settings):
    from picker import owned
    from picker.models import Import
    seed()
    with session() as s:
        imp = Import(rg_mbid="rg-1", release_mbid="jp-cd", state="imported", step="done", album_dir=str(settings.music_dir))
        s.add(imp)
        s.commit()
        s.refresh(imp)

    async def no_library(s):
        return {}

    monkeypatch.setattr("picker.beets.album_list", no_library)
    html = client.get("/album/rg-1").text
    assert "You already have this album" in html and "1997-05-28 · JP · CD · Toshiba-EMI · TOCP-50201" in html
    assert "was imported by the picker" in html and f"/imports/{imp.id}" in html

    async def fake_search(artist, album, limit=25):
        return [{"id": "rg-1", "title": "OK Computer", "artist-credit": [{"name": "Radiohead"}]},
                {"id": "rg-9", "title": "Kid A", "artist-credit": [{"name": "Radiohead"}]}]

    class MB:
        search_release_groups = staticmethod(fake_search)

    class S:
        mb = MB()

    monkeypatch.setattr("picker.web.services", lambda: S())
    html = client.get("/search?artist=Radiohead&album=").text
    assert html.count("in library") == 1 and html.index("OK Computer") < html.index("in library") < html.index("Kid A")


def test_review_list_shows_everything_waiting_with_inline_buttons(client, with_editions, settings):
    from picker.models import Import
    seed()
    settings.auto_import, settings.transcode_check = True, False
    with session() as s:
        rows = [
            Import(rg_mbid="rg-1", release_mbid="jp-cd", username="alice", state="review", step="review", source_dir="/dl/1",
                   integrity=[{"name": "01.flac", "ok": True, "detail": ""}], transcode={"album": {"kind": "full", "detail": "fine"}, "files": []},
                   tag_check={"kind": "different", "detail": "the files are tagged as another edition", "tagged_mbid": "uk-cd", "in_group": True, "tags": {}}),
            Import(rg_mbid="rg-1", release_mbid="jp-cd", username="bob", state="skipped", step="beets", album_dir="/music/x",
                   beets_match={"similarity": 0.827, "threshold": 0.9, "penalties": [], "tracks": [], "missing": [], "unmatched": [], "album": "a", "release": "jp-cd"}),
            Import(rg_mbid="rg-1", release_mbid="jp-cd", username="carol", state="blocked", step="place", error="target exists", source_dir="/dl/3"),
            Import(rg_mbid="rg-1", release_mbid="jp-cd", username="dave", state="failed", step="verify", error="verification failed: 1 damaged", source_dir="/dl/4",
                   integrity=[{"name": "01.flac", "ok": False, "detail": "FLAC decoder aborted", "path": "/dl/4/01.flac"}]),
            Import(rg_mbid="rg-1", release_mbid="jp-cd", username="erin", state="failed", step="beets", error="beets exited 2", album_dir="/music/y"),
            Import(rg_mbid="rg-1", release_mbid="jp-cd", username="frank", state="imported", step="done", album_dir="/music/z"),
        ]
        s.add_all(rows)
        s.commit()
    html = client.get("/review").text
    assert "4 imports" in html
    assert "alice" in html and "bob" in html and "carol" in html and "dave" in html
    assert "erin" not in html and "frank" not in html  # a failure past verify is not a check to review; imported is done
    assert "tags differ" in html and "full-range" in html and "1 files ok" in html and "beets 83%" in html and "1 damaged" in html
    assert "the files are tagged as a different edition" in html and "target exists" in html
    assert html.count('name="back" value="review"') >= 6
    assert "Import</button>" in html and "Force apply" in html and "Retry" in html and "Reject" in html
    assert 'href="/review">Review <span class="badge warn">4</span>' in html
    assert "4 imports waiting for you" in client.get("/").text

    # Actions started from the list come back to it.
    resp = client.post(f"/imports/{rows[2].id}/retry", data={"back": "review"}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/review"
    resp = client.post(f"/imports/{rows[2].id}/retry", data={}, follow_redirects=False)
    assert resp.headers["location"] == f"/imports/{rows[2].id}"


def test_review_page_shows_rip_verification(client, with_editions, settings):
    from picker.models import Import
    seed()
    settings.auto_import, settings.transcode_check = True, False
    rows = [{"n": 1, "file": "01 Airbag.flac", "status": "ok", "confidence": 20, "total": 20, "v1": "0", "v2": "0"},
            {"n": 2, "file": "02 Paranoid Android.flac", "status": "mismatch", "confidence": 0, "total": 20, "v1": "0", "v2": "0"}]
    crows = [{"n": 1, "file": "01 Airbag.flac", "status": "ok", "confidence": 40, "total": 40, "crc": "0"},
             {"n": 2, "file": "02 Paranoid Android.flac", "status": "mismatch", "confidence": 0, "total": 40, "crc": "0"}]
    check = {"applicable": True, "mismatched": ["02 Paranoid Android.flac"], "summary": "CTDB 1/2 tracks match, AccurateRip 1/2 tracks accurate",
             "discs": [{"applicable": True, "toc_source": "log", "files": 2,
                        "accuraterip": {"status": "mismatch", "offset": 0, "confidence": 0, "matched": 1, "known": 2, "tracks": rows},
                        "ctdb": {"status": "mismatch", "offset": 0, "confidence": 0, "entries": 1, "tracks": crows}}]}
    with session() as s:
        imp = Import(rg_mbid="rg-1", release_mbid="jp-cd", username="alice", state="review", step="review", source_dir="/dl/picker/1",
                     download_id=1, integrity=[{"name": "02 Paranoid Android.flac", "ok": True, "path": "/dl/picker/1/02 Paranoid Android.flac"}],
                     rip_check=check)
        s.add(imp)
        s.commit()
        s.refresh(imp)
    html = client.get(f"/imports/{imp.id}").text
    assert "1 track differs from the verified rip" in html and "02 Paranoid Android.flac" in html
    assert "Re-download the 1 differing track" in html and "TOC from the rip log" in html
    assert "Waiting for you because a track differs from the verified rip" in html
    assert "1 track differ" in client.get("/review").text and "rip differs" in client.get("/history").text

    with session() as s:
        row = s.get(Import, imp.id)
        row.rip_check = {**check, "mismatched": [], "summary": "CTDB verified (confidence 40), AccurateRip 2/2 (confidence 20)",
                         "discs": [{**check["discs"][0], "accuraterip": {**check["discs"][0]["accuraterip"], "status": "verified", "tracks": [rows[0], {**rows[1], "status": "ok", "confidence": 20}]},
                                    "ctdb": {**check["discs"][0]["ctdb"], "status": "verified", "confidence": 40, "tracks": [crows[0], {**crows[1], "status": "ok", "confidence": 40}]}}]}
        s.add(row)
        s.commit()
    html = client.get(f"/imports/{imp.id}").text
    assert "verified rip" in html and "Re-download" not in html and "<table class=\"grid small rip-check\">" not in html
    assert "verified rip" in client.get("/history").text


def test_review_page_explains_a_beets_skip(client, with_editions):
    from picker.models import Import
    seed()
    match = {
        "release": "jp-cd", "album": "Radiohead - OK Computer", "similarity": 0.827, "threshold": 0.9,
        "penalties": [{"key": "tracks", "name": "track differences (below)", "distance": 0.173}],
        "tracks": [
            {"file": "01 Airbag.flac", "file_title": "Airbag", "file_length": 284.7, "index": 1, "medium": 1, "title": "Airbag",
             "length": 284.6, "length_diff": 0.1, "distance": 0.0, "penalties": []},
            {"file": "02 Paranoid Android.flac", "file_title": "Paranoid Android (live)", "file_length": 390.2, "index": 2, "medium": 1,
             "title": "Paranoid Android", "length": 383.5, "length_diff": 6.7, "distance": 0.41,
             "penalties": [{"key": "track_title", "name": "title", "distance": 0.3}, {"key": "track_length", "name": "length", "distance": 0.11}]},
        ],
        "missing": [{"index": 12, "medium": 1, "title": "The Tourist", "length": 324.0}],
        "unmatched": ["13 Bonus.flac"],
    }
    with session() as s:
        imp = Import(rg_mbid="rg-1", release_mbid="jp-cd", username="alice", state="skipped", step="beets",
                     album_dir="/music/Radiohead/OK Computer (1997)", beets_output="Skipping.", beets_match=match)
        s.add(imp)
        s.commit()
        s.refresh(imp)
    html = client.get(f"/imports/{imp.id}").text
    assert "Why beets skipped it" in html and "beets rated the match 82.7%" in html and "from 90.0%" in html
    assert "02 Paranoid Android.flac" in html and "tagged “Paranoid Android (live)”" in html and "(+6.7 s)" in html
    assert "01 Airbag.flac" not in html  # paired cleanly: not listed
    assert "The Tourist" in html and "missing from the folder" in html
    assert "13 Bonus.flac" in html and "matches nothing on the release" in html
    assert "Force apply" in html


def test_backup_copies_the_database_and_keeps_the_newest(tmp_path, monkeypatch):
    from picker.db import backup_db
    db = tmp_path / "picker.db"
    assert backup_db(db) is None  # nothing to back up yet
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t (x)")
    con.execute("INSERT INTO t VALUES (42)")
    con.commit()
    con.close()
    stamps = iter(["20260101-000000", "20260102-000000", "20260103-000000"])

    class Clock:
        @staticmethod
        def now():
            class Stamp:
                @staticmethod
                def strftime(fmt):
                    return next(stamps)
            return Stamp()

    monkeypatch.setattr("picker.db.datetime", Clock)
    copies = [backup_db(db, keep=2) for _ in range(3)]
    assert copies[0].name == "picker-20260101-000000.db"
    assert sorted(p.name for p in (tmp_path / "backups").iterdir()) == ["picker-20260102-000000.db", "picker-20260103-000000.db"]
    assert sqlite3.connect(copies[-1]).execute("SELECT x FROM t").fetchone() == (42,)


def test_folder_preview_renders_the_template(client, settings):
    html = client.post("/settings/folder-preview", data={"album_folder_template": "{Album Title} ({Release Year}){ [Catalog Number]}"}).text
    assert "Radiohead / OK Computer (1997) [CDP 7243 8 55229 2 5]" in html and "example" in html
    assert "Unknown token" in client.post("/settings/folder-preview", data={"album_folder_template": "{Nope}"}).text
    assert "OK Computer (1997)" in client.post("/settings/folder-preview", data={}).text  # blank: the default
    assert 'hx-post="/settings/folder-preview"' in client.get("/settings").text


def test_folder_preview_uses_recent_imports(client, settings, with_editions):
    from picker.models import Import
    seed()
    with session() as s:
        album = s.get(Album, "rg-1")
        album.first_release_date = "1997-05-21"
        s.add(album)
        s.add(Import(rg_mbid="rg-1", release_mbid="jp-cd", state="imported", step="done",
                     album_dir="/music/Radiohead/OK Computer (1997)"))
        s.commit()
    html = client.post("/settings/folder-preview", data={"album_folder_template": "{Album Title} ({Release Year})"}).text
    assert "Radiohead / OK Computer (1997)" in html and "would stay" in html
    html = client.post("/settings/folder-preview", data={"album_folder_template": "{Album Title} [{Country}]"}).text
    assert "Radiohead / OK Computer [JP]" in html and "would change" in html


def test_give_up_starts_the_next_candidate(client, monkeypatch):
    from picker import web
    from picker.models import Download
    seed()
    with session() as s:
        alice = s.exec(select(Candidate).where(Candidate.username == "alice")).one()
        dl = Download(candidate_id=alice.id, search_id=alice.search_id, rg_mbid="rg-1", username="alice", folder="m",
                      destination="picker/1", files=[], state="enqueued")
        s.add(dl)
        s.commit()
        s.refresh(dl)
    started = []
    monkeypatch.setattr(web, "start_download", lambda c, rg, rel: started.append(c.username))
    resp = client.post(f"/downloads/{dl.id}/giveup", follow_redirects=False)
    assert resp.status_code == 303
    with session() as s:
        assert s.get(Download, dl.id).give_up is True
    assert started == ["bob"]


def test_recommendation_card_once_settled(client, with_editions):
    seed()
    set_match("alice", tier="Exact", editions=["jp-cd"], matched_catalogs=["TOCP-50201"], tracks_agree=True,
              signals=[{"name": "catalog", "weight": 8, "detail": "catalog number"}])
    set_match("bob", tier="Medium", editions=["uk-cd", "jp-cd"])
    html = client.get("/album/rg-1/candidates").text
    assert 'id="recommendation"' in html and "Best pick:</strong> alice" in html
    assert "Exact by catalog number" in html and "Why not the others?" in html and "<strong>bob</strong>: Medium match" in html
    assert "Download as 1997-05-28 · JP · CD · Toshiba-EMI · TOCP-50201" in html
    assert "~" in html and 'data-settled="true"' in html
    state = client.get("/album/rg-1/candidates/state").json()
    assert state["settled"] is True


def test_candidates_poll_state_and_rows(client):
    seed()
    state = client.get("/album/rg-1/candidates/state").json()
    assert state["phase"] == "rows" and state["active"] is False
    ids = [row[0] for row in state["rows"]]
    assert len(ids) == 2 and all(len(row[1]) == 12 for row in state["rows"])
    # Versions are stable while nothing changes...
    assert client.get("/album/rg-1/candidates/state").json()["rows"] == state["rows"]
    # ...and only the requested rows come back, each in its own <tbody> keyed by id.
    html = client.get(f"/album/rg-1/candidates/rows?ids={ids[0]}").text
    assert html.count('class="cand-group"') == 1 and f'id="cand-{ids[0]}"' in html
    with session() as s:
        c = s.get(Candidate, ids[0])
        c.match = {"tier": "Exact", "editions": []}
        s.add(c)
        s.commit()
    changed = dict(client.get("/album/rg-1/candidates/state").json()["rows"])
    before = dict(state["rows"])
    assert changed[ids[0]] != before[ids[0]] and changed[ids[1]] == before[ids[1]]


def test_full_section_has_sticky_actions_and_poll_hooks(client):
    seed()
    html = client.get("/album/rg-1/candidates").text
    assert 'data-phase="rows"' in html and "data-poll" not in html  # nothing active: no polling
    assert '<th class="actions">' in html and 'class="detail-actions"' in html  # Download also in the expanded row
    assert 'class="path"' in html and "text-overflow" not in html


@pytest.mark.parametrize("state, extra, expect", [
    ("transferring", {"chunked": "limit", "window_files": 3,
                      "file_states": {"a": {"status": "done"}, "b": {"status": "limited"}}},
     "Uploader queue limit: sending in chunks of 3 files (1 waiting)"),
    ("transferring", {"file_states": {"a": {"status": "retry_wait", "error": "timed out"}}},
     "1 file will be requested again after an error"),
    ("partial", {"error": "1 of 14 files could not be fetched: 03.flac",
                 "file_states": {"a": {"status": "failed"}}}, "Retry failed files"),
])
def test_downloads_page_explains_progress(client, state, extra, expect):
    from picker.models import Download
    seed()
    with session() as s:
        s.add(Download(candidate_id=1, search_id=1, rg_mbid="rg-1", username="u", folder=r"m\x", destination="picker/1",
                       files=[{"filename": "a", "size": 1}], state=state, **extra))
        s.commit()
    html = client.get("/downloads").text
    assert expect in html
    if state == "partial":
        assert "Try next candidate" in html


def test_review_page_offers_redownload_after_a_failed_check(client, with_editions):
    from picker.models import Download, Import
    seed()
    with session() as s:
        dl = Download(candidate_id=1, search_id=1, rg_mbid="rg-1", username="jamjam", folder="m", destination="picker/32", files=[])
        s.add(dl)
        s.commit()
        s.refresh(dl)
        imp = Import(download_id=dl.id, rg_mbid="rg-1", release_mbid="jp-cd", username="jamjam", state="failed", step="verify",
                     refetches=1, error="verification failed: 1 file(s) failed the integrity check",
                     integrity=[{"name": "03.flac", "path": "/x/03.flac", "ok": False, "detail": "state = FLAC__STREAM_DECODER_ABORTED"},
                                {"name": "01.flac", "path": "/x/01.flac", "ok": True, "detail": "300 s"}])
        s.add(imp)
        s.commit()
        s.refresh(imp)
    html = client.get(f"/imports/{imp.id}").text
    assert "Re-download failed tracks" in html and "already tried 1 time" in html
    assert f'action="/imports/{imp.id}/retry"' in html and "Checks the folder again" in html
    assert "Check failed: review" in client.get("/downloads").text


@pytest.mark.parametrize("kind, expect, history", [
    ("lossy", "Possible transcode.", "transcode?"),
    ("upsampled", "Possibly upsampled.", "upsampled?"),
    ("full", "full-range spectrum", None),
])
def test_review_page_shows_the_spectrum_check(client, with_editions, kind, expect, history):
    from picker.models import Import
    seed()
    detail = {"lossy": "spectrum ends at 16.0 kHz (62 dB cliff), consistent with a ~128 kbps lossy source",
              "upsampled": "96 kHz file with no content above ~22 kHz: probably upsampled from 44.1/48 kHz",
              "full": "energy continues to the top of the spectrum"}[kind]
    verdict = {"kind": kind, "cutoff_hz": 16000 if kind == "lossy" else None, "drop_db": None, "detail": detail}
    with session() as s:
        imp = Import(rg_mbid="rg-1", release_mbid="jp-cd", username="u", state="review", step="review",
                     transcode={"album": verdict, "files": [{"name": "01.flac", **verdict}, {"name": "07.flac", **verdict}]})
        s.add(imp)
        s.commit()
        s.refresh(imp)
    html = client.get(f"/imports/{imp.id}").text
    assert expect in html and "Spectrum check" in html
    if history:
        assert history in client.get("/history").text


def test_review_page_shows_the_chosen_edition_without_asking(client, with_editions, settings):
    from picker.models import Import
    seed()
    settings.auto_import = True
    with session() as s:
        imp = Import(rg_mbid="rg-1", release_mbid="jp-cd", username="u", state="review", step="review",
                     transcode={"album": {"kind": "lossy", "cutoff_hz": 16000, "drop_db": 60, "detail": "spectrum ends at 16.0 kHz"},
                                "files": []})
        s.add(imp)
        s.commit()
        s.refresh(imp)
    html = client.get(f"/imports/{imp.id}").text
    assert "Importing as" in html and "(chosen when you downloaded it)" in html
    assert '<details class="change-edition" >' in html or '<details class="change-edition">' in html  # collapsed
    assert "Waiting for you because the spectrum check suggests a transcode" in html
    assert 'action="/imports/' in html and "disabled" not in html.split("/import\"")[1][:80]  # Import is enabled


def test_review_page_says_when_automatic_import_is_off(client, with_editions, settings):
    from picker.models import Import
    seed()
    settings.auto_import = False
    with session() as s:
        imp = Import(rg_mbid="rg-1", release_mbid="jp-cd", username="u", state="review", step="review")
        s.add(imp)
        s.commit()
        s.refresh(imp)
    assert "automatic import is off" in client.get(f"/imports/{imp.id}").text
