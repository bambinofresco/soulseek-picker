"""Upgrades (spec 7.2): the old copy moves to the replaced folder, old and new are compared, undo puts it back."""

import os
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from picker import beets, downloading, importing, library
from picker.db import session
from picker.editions import Edition
from picker.models import Album, Candidate, Download, Import, Job, utcnow
from picker.web import router
from tests.test_library import make_flac

GB = Edition(id="gb", title="OK Computer", date="1997-06-16", country="GB", formats=["CD"])
RM = Edition(id="rm", title="OK Computer", date="2009-03-24", country="GB", formats=["CD"], disambiguation="remastered")
REL_GB, REL_RM = "11111111-0000-0000-0000-00000000000b", "11111111-0000-0000-0000-00000000000e"
RG = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@pytest.fixture
def world(settings, monkeypatch):
    settings.replaced_dir = settings.config_dir / "replaced"
    settings.beets_pretend = True
    with session() as s:
        s.add(Album(rg_mbid=RG, artist="Radiohead", title="OK Computer", min_track_count=12))
        s.commit()

    async def fake_editions(rg):
        return [Edition(id=REL_GB, title="OK Computer", date="1997-06-16", country="GB", formats=["CD"]),
                Edition(id=REL_RM, title="OK Computer", date="2009-03-24", country="GB", formats=["CD"], disambiguation="remastered")]

    monkeypatch.setattr(importing.albums, "album_editions", fake_editions)
    return settings


def old_copy(settings, tracks=10, release=REL_RM, depth_tag=None) -> Path:
    folder = settings.music_dir / "Radiohead" / "OK Computer (1997)"
    for n in range(1, tracks + 1):
        make_flac(folder / f"{n:02d} Track.flac", n, musicbrainz_albumid=release, musicbrainz_releasegroupid=RG)
    library.scan(settings.music_dir, settings)
    return folder


def new_download(settings, tracks=12, depth=16, release=REL_GB, upgrade_of=None):
    src = settings.download_dir / "picker" / "7" / "OK Computer [EAC]"
    for n in range(1, tracks + 1):
        make_flac(src / f"{n:02d} Track.flac", n)
    with session() as s:
        cand = Candidate(search_id=1, username="alice", folder="f", query="q", audio_count=tracks, audio_format="FLAC",
                         bit_depth=depth, sample_rate=44100, match={"tier": "Exact", "editions": [release]})
        s.add(cand)
        s.commit()
        s.refresh(cand)
        dl = Download(candidate_id=cand.id, search_id=1, rg_mbid=RG, username="alice", folder="f", destination="picker/7",
                      release_mbid=release, upgrade_of=upgrade_of, bytes_total=tracks * 30_000_000, state="completed")
        s.add(dl)
        s.commit()
        s.refresh(dl)
        imp = Import(download_id=dl.id, rg_mbid=RG, release_mbid=release, username="alice", source_dir=str(src),
                     upgrade_of=upgrade_of, step="place", state="importing")
        s.add(imp)
        s.commit()
        s.refresh(imp)
    return imp, src


def test_start_download_and_import_carry_upgrade_of(world):
    with session() as s:
        cand = Candidate(search_id=1, username="alice", folder="f", query="q", files=[{"filename": "f\\01.flac", "size": 1, "kind": "audio"}])
        s.add(cand)
        s.commit()
        s.refresh(cand)
    dl = downloading.start_download(cand, RG, REL_GB, upgrade_of="/music/Radiohead/OK Computer (1997)")
    assert dl.upgrade_of == "/music/Radiohead/OK Computer (1997)"
    imp = importing.start_import(dl)
    assert imp.upgrade_of == dl.upgrade_of


async def test_compare_old_and_new(world):
    settings = world
    old = old_copy(settings, tracks=10, release=REL_RM)
    imp, _ = new_download(settings, tracks=12, depth=24, upgrade_of=str(old))
    check = await importing.compare_upgrade(imp, settings)
    assert check["old"]["tracks"] == 10 and check["new"]["tracks"] == 12
    assert check["old"]["depth"] == 16 and check["new"]["depth"] == 24
    assert check["old"]["pref"] < check["new"]["pref"]  # the remaster loses points
    assert check["better"] and "complete (12 tracks vs 10)" in check["reasons"] and "24-bit vs 16-bit" in check["reasons"]
    assert not check["worse"]

    # The other way round: fewer tracks and a remaster is not an upgrade.
    worse_imp, _ = new_download(settings, tracks=8, depth=16, release=REL_RM, upgrade_of=str(old))
    check = await importing.compare_upgrade(worse_imp, settings)
    assert not check["better"] and any("fewer tracks" in w for w in check["worse"])

    # An old copy without MusicBrainz tags cannot be judged.
    untagged = settings.music_dir / "Radiohead" / "Untagged"
    make_flac(untagged / "01.flac", 1)
    library.scan(settings.music_dir, settings)
    imp3, _ = new_download(settings, upgrade_of=str(untagged))
    check = await importing.compare_upgrade(imp3, settings)
    assert not check["better"] and any("unknown" in w for w in check["worse"])


def test_review_reason_for_upgrades(world):
    settings = world
    settings.auto_import, settings.transcode_check, settings.rip_verify = True, False, False
    imp = Import(rg_mbid=RG, release_mbid=REL_GB, upgrade_of="/x", upgrade_check={"better": True})
    settings.auto_upgrade = False
    assert "auto_upgrade is off" in importing.review_reason(imp, settings)
    settings.auto_upgrade = True
    assert importing.review_reason(imp, settings) is None
    imp.upgrade_check = {"better": False}
    assert "not clearly better" in importing.review_reason(imp, settings)
    assert importing.review_reason(Import(rg_mbid=RG, release_mbid=REL_GB), settings) is None


def test_place_retires_the_old_copy(world):
    settings = world
    old = old_copy(settings, tracks=10)
    imp, src = new_download(settings, upgrade_of=str(old))
    assert library.folder_files(old)
    importing._place(imp, old)  # the template names the same folder
    with session() as s:
        imp = s.get(Import, imp.id)
    assert imp.album_dir == str(old) and Path(old).is_dir() and not src.exists()
    assert len(list(Path(old).glob("*.flac"))) == 12  # the new copy
    parked = Path(imp.replaced_dir)
    assert parked.parent == settings.replaced_dir / "Radiohead" and parked.name.startswith("OK Computer (1997) (")
    assert len(list(parked.glob("*.flac"))) == 10  # the old one, intact
    assert library.folder_files(old) == []  # the index forgot the old files; the import's own scan re-adds the new ones
    assert imp.step == "beets"


def test_place_refuses_without_space_and_outside_the_library(world, monkeypatch):
    settings = world
    old = old_copy(settings)
    imp, src = new_download(settings, upgrade_of=str(old))

    class Usage:
        free = 10

    monkeypatch.setattr(importing.shutil, "disk_usage", lambda p: Usage)
    with pytest.raises(importing.Blocked, match="not enough free space"):
        importing._place(imp, old)
    assert old.is_dir() and src.is_dir() and len(list(old.glob("*.flac"))) == 10  # nothing moved
    monkeypatch.undo()
    monkeypatch.setattr(importing.albums, "album_editions", lambda rg: None)
    outside = settings.config_dir / "elsewhere"
    outside.mkdir()
    imp2, _ = new_download(settings, upgrade_of=str(outside))
    with pytest.raises(importing.Blocked, match="not an album folder inside"):
        importing._place(imp2, settings.music_dir / "Radiohead" / "X")
    assert outside.is_dir()


def test_place_handles_an_old_copy_that_vanished(world):
    settings = world
    imp, src = new_download(settings, upgrade_of=str(settings.music_dir / "Radiohead" / "Gone (1990)"))
    importing._place(imp, settings.music_dir / "Radiohead" / "OK Computer (1997)")
    with session() as s:
        imp = s.get(Import, imp.id)
    assert imp.replaced_dir is None and any("already gone" in n for n in imp.notes)


async def test_undo_puts_the_old_copy_back(world, monkeypatch):
    settings = world
    settings.lidarr_api_key = ""
    old = old_copy(settings, tracks=10)
    imp, src = new_download(settings, upgrade_of=str(old))
    importing._place(imp, old)
    with session() as s:
        imp = s.get(Import, imp.id)
        imp.state, imp.step = "imported", "done"
        s.add(imp)
        s.commit()
    library.scan(settings.music_dir, settings)
    assert len(library.folder_files(old)) == 12

    async def no_plex(_imp, _settings):
        return None

    monkeypatch.setattr(importing, "_scan_plex", no_plex)
    importing.request_undo(imp)
    with session() as s:
        job = s.exec(select(Job).where(Job.kind == "undo_upgrade")).first()
    assert job is not None
    await importing.run_undo(job)
    with session() as s:
        imp = s.get(Import, imp.id)
    assert imp.upgrade_undone and imp.state == "undone" and imp.album_dir == str(old)
    assert len(list(old.glob("*.flac"))) == 10  # the old copy is back
    new_parked = Path(imp.replaced_dir)
    assert "undone" in new_parked.name and len(list(new_parked.glob("*.flac"))) == 12
    assert len(library.folder_files(old)) == 10
    with pytest.raises(importing.Blocked):
        importing.request_undo(imp)  # only once


def test_request_undo_blocks_without_a_replaced_copy(world):
    with pytest.raises(importing.Blocked):
        importing.request_undo(Import(rg_mbid=RG))
    with pytest.raises(importing.Blocked, match="no longer at"):
        importing.request_undo(Import(rg_mbid=RG, upgrade_of="/a", replaced_dir="/nope", album_dir="/b"))


def test_replaced_summary_and_delete(world):
    settings = world
    assert importing.replaced_summary(settings) == {"path": str(settings.replaced_dir), "folders": 0, "bytes": 0, "oldest": None}
    recent = settings.replaced_dir / "Radiohead" / "A (2026-01-01 1200)"
    stale = settings.replaced_dir / "Blur" / "B (2025-01-01 1200)"
    for folder in (recent, stale):
        make_flac(folder / "01.flac", 1)
    old_time = time.time() - 40 * 86400
    os.utime(stale, (old_time, old_time))
    summary = importing.replaced_summary(settings)
    assert summary["folders"] == 2 and summary["bytes"] > 0 and summary["oldest"] is not None
    assert importing.delete_replaced(settings, 30) == 1
    assert recent.is_dir() and not stale.exists() and not stale.parent.exists()
    assert importing.delete_replaced(settings, -1) == 0
    assert importing.delete_replaced(settings, 0) == 1 and not recent.exists()


def test_pages(world, monkeypatch):
    settings = world
    settings.auto_import, settings.transcode_check, settings.rip_verify = True, False, False
    old = old_copy(settings)
    imp, _ = new_download(settings, upgrade_of=str(old))
    with session() as s:
        imp = s.get(Import, imp.id)
        imp.upgrade_check = {"old": {"path": str(old), "tracks": 10, "depth": 16, "rate": 44100, "size": 1, "release": REL_RM,
                                     "pref": -10, "edition": "2009 GB CD", "complete": False},
                             "new": {"tracks": 12, "depth": 16, "rate": 44100, "size": 2, "release": REL_GB, "pref": 4,
                                     "edition": "1997 GB CD", "complete": True},
                             "better": True, "reasons": ["complete (12 tracks vs 10)"], "worse": []}
        imp.state, imp.step = "review", "review"
        s.add(imp)
        s.commit()

    async def fake_editions(rg):
        return []

    from picker import albums as albums_module

    monkeypatch.setattr(albums_module, "album_editions", fake_editions)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    html = client.get(f"/imports/{imp.id}").text
    assert "This replaces your copy" in html and "complete (12 tracks vs 10)" in html and "2009 GB CD" in html
    assert "Put the old copy back" not in html  # not imported yet
    assert "auto_upgrade is off" in html
    settings_html = client.get("/settings").text
    assert "Replaced copies" in settings_html and "It is empty" in settings_html
    assert "upgrade" in client.get("/history").text
