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
    assert panel.headers["HX-Trigger"] == "candidates-changed"
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


def test_prefer_original_orders_within_a_tier(client, settings):
    from picker.searching import ordered_candidates
    seed()
    set_match("alice", tier="High", editions=["rm"], earliest_date="2009-01-01")
    set_match("bob", tier="High", editions=["orig"], earliest_date="1997-05-21")
    with session() as s:
        search = s.exec(select(Search)).one()
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
                     error="target exists" if state == "blocked" else None)
        s.add(imp)
        s.commit()
        s.refresh(imp)
    html = client.get(f"/imports/{imp.id}").text
    assert expect in html
    assert "1997-05-28 · JP · CD · Toshiba-EMI · TOCP-50201" in html
    assert client.get("/history").status_code == 200
    downloads = client.get("/downloads").text
    assert f"/imports/{imp.id}" in downloads
    # Only file names recorded on the import are served.
    assert client.get(f"/imports/{imp.id}/spectrogram/picker.db").status_code == 404
    assert client.get(f"/imports/{imp.id}/spectrogram/..%2F..%2Fpicker.db").status_code == 404


def test_missing_columns_are_added(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE candidate (id INTEGER PRIMARY KEY, search_id INTEGER, username VARCHAR)")
    con.commit()
    con.close()
    init_db(db)
    cols = {row[1] for row in sqlite3.connect(db).execute("PRAGMA table_info(candidate)")}
    assert {"parsed", "sidecars", "upload_speed"} <= cols
