"""The library index (picker/library.py): scanning, owned lookups, the Lidarr comparison, the page."""

import os
import struct
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mutagen.flac import FLAC
from sqlmodel import select

from picker import library, owned
from picker.db import session
from picker.models import Job, LibraryFile
from picker.web import router

REL, RG = "11111111-2222-3333-4444-555555555555", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
REC = "99999999-8888-7777-6666-555555555555"


def make_flac(path: Path, n: int, seconds: int = 200, **tags) -> None:
    """A valid, silent FLAC with a STREAMINFO claiming `seconds` of 44.1 kHz 16-bit stereo, plus tags."""
    samples = 44100 * seconds
    info = (44100 << 44) | (1 << 41) | (15 << 36) | samples
    block = struct.pack(">HH", 4096, 4096) + b"\0" * 6 + struct.pack(">Q", info) + b"\0" * 16
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fLaC" + bytes([0x80, 0, 0, 34]) + block)
    f = FLAC(path)
    f.update({"title": f"Track {n}", "tracknumber": f"{n}/12", "artist": "Radiohead", "album": "OK Computer", **tags})
    f.save()


def album(music: Path, name="OK Computer (1997)", tracks=3, **tags) -> Path:
    folder = music / "Radiohead" / name
    for n in range(1, tracks + 1):
        make_flac(folder / f"{n:02d} Track {n}.flac", n, **tags)
    return folder


def rows():
    with session() as s:
        return s.exec(select(LibraryFile).order_by(LibraryFile.path)).all()


def test_read_file_takes_stream_facts_and_musicbrainz_ids(settings, tmp_path):
    path = tmp_path / "music" / "x.flac"
    make_flac(path, 7, seconds=61, musicbrainz_albumid=REL, musicbrainz_releasegroupid=RG.upper(),
              musicbrainz_trackid=REC, discnumber="1/2", albumartist="Radiohead")
    read = library.read_file(path)
    assert read["error"] is None
    assert read["duration"] == 61.0 and read["bit_depth"] == 16 and read["sample_rate"] == 44100
    assert read["title"] == "Track 7" and read["tracknumber"] == 7 and read["discnumber"] == 1
    assert read["release_mbid"] == REL and read["rg_mbid"] == RG and read["recording_mbid"] == REC
    assert read["albumartist"] == "Radiohead"


def test_read_file_ignores_values_that_are_not_mbids(settings, tmp_path):
    path = tmp_path / "music" / "x.flac"
    make_flac(path, 1, musicbrainz_albumid="not-an-id", tracknumber="A1")
    read = library.read_file(path)
    assert read["release_mbid"] is None and read["tracknumber"] is None


def test_read_file_reports_garbage(settings, tmp_path):
    path = tmp_path / "music" / "bad.flac"
    path.write_bytes(b"garbage")
    read = library.read_file(path)
    assert read["error"] and read["title"] is None


def test_scan_adds_updates_and_removes(settings):
    folder = album(settings.music_dir, tracks=3, musicbrainz_albumid=REL, musicbrainz_releasegroupid=RG)
    (settings.music_dir / "Radiohead" / "cover.jpg").write_bytes(b"\xff\xd8")  # not audio: ignored
    result = library.scan(settings.music_dir, settings)
    assert (result.files, result.added, result.updated, result.removed) == (3, 3, 0, 0)
    assert len(rows()) == 3 and all(r.rg_mbid == RG and r.folder == str(folder) for r in rows())

    # Unchanged files are not re-read; a retagged one is.
    second = library.scan(settings.music_dir, settings)
    assert (second.added, second.updated, second.removed) == (0, 0, 0)
    path = folder / "02 Track 2.flac"
    f = FLAC(path)
    f["title"] = "Renamed"
    f.save()
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 10))
    third = library.scan(settings.music_dir, settings)
    assert (third.added, third.updated, third.removed) == (0, 1, 0)
    assert [r.title for r in rows()] == ["Track 1", "Renamed", "Track 3"]

    # A deleted file drops out.
    (folder / "03 Track 3.flac").unlink()
    fourth = library.scan(settings.music_dir, settings)
    assert fourth.removed == 1 and len(rows()) == 2


def test_scan_of_one_folder_leaves_the_rest_alone(settings):
    a = album(settings.music_dir, "A (1990)", tracks=2)
    b = album(settings.music_dir, "B (1991)", tracks=2)
    library.scan(settings.music_dir, settings)
    (a / "01 Track 1.flac").unlink()
    make_flac(b / "03 Track 3.flac", 3)
    result = library.scan(b, settings)
    assert (result.files, result.added, result.removed) == (3, 1, 0)
    assert len(rows()) == 5  # A's deleted file is still indexed until A or the library is scanned
    assert library.scan(a, settings).removed == 1


def test_owned_lookups(settings):
    album(settings.music_dir, tracks=2, musicbrainz_albumid=REL, musicbrainz_releasegroupid=RG)
    folder = settings.music_dir / "Radiohead" / "OK Computer (1997)"
    f = FLAC(folder / "01 Track 1.flac")
    f["musicbrainz_trackid"] = REC
    f["title"] = "Airbag (Remastered)"
    f.save()
    library.scan(settings.music_dir, settings)

    by_id = library.owned_recordings([REC, "00000000-0000-0000-0000-000000000000"])
    assert list(by_id) == [REC] and by_id[REC].path.endswith("01 Track 1.flac")
    assert library.owned_by_name("Radiohead", "Airbag", 200).path.endswith("01 Track 1.flac")
    assert library.owned_by_name("The Radiohead", "airbag!", None) is not None
    assert library.owned_by_name("Radiohead", "Airbag", 250) is None  # 50 s off
    assert library.owned_by_name("Radiohead", "Paranoid Android", None) is None
    assert library.owned_by_name("Blur", "Airbag", None) is None
    assert len(library.album_files(RG.upper())) == 2
    assert len(library.folder_files(folder)) == 2
    library.forget_folder(folder)
    assert library.album_files(RG) == []


async def test_index_is_a_fourth_owned_source(settings, monkeypatch):
    class Services:
        lidarr = None

    monkeypatch.setattr(owned, "services", lambda: Services())
    settings.lidarr_api_key = ""

    async def no_beets(_settings):
        return {}

    monkeypatch.setattr(owned, "_beets_albums", no_beets)
    assert await owned.check_album(RG, settings) is None
    album(settings.music_dir, tracks=2, musicbrainz_albumid=REL, musicbrainz_releasegroupid=RG)
    library.scan(settings.music_dir, settings)
    have = await owned.check_album(RG, settings)
    assert have.source == "index" and have.release_mbid == REL and have.have == 2
    assert have.path == str(settings.music_dir / "Radiohead" / "OK Computer (1997)")
    found = await owned.owned_release_groups([RG, "other"], settings)
    assert list(found) == [RG]


class FakeLidarr:
    def __init__(self, files_by_artist):
        self.files_by_artist = files_by_artist

    async def artists(self):
        return [{"id": i} for i in self.files_by_artist]

    async def track_files_for_artist(self, artist_id):
        return self.files_by_artist[artist_id]


async def test_compare_with_lidarr(settings, monkeypatch):
    folder = album(settings.music_dir, tracks=2)
    library.scan(settings.music_dir, settings)
    settings.lidarr_api_key = "k"
    lidarr = FakeLidarr({1: [{"id": 10, "path": str(folder / "01 Track 1.flac")},
                             {"id": 11, "path": str(folder / "09 Gone.flac")}]})

    class Services:
        pass

    Services.lidarr = lidarr
    monkeypatch.setattr(library, "services", lambda: Services())
    diff = await library.compare_with_lidarr(settings)
    assert diff.checked and diff.lidarr_files == 2 and diff.matched == 1
    assert diff.unknown_to_lidarr == [str(folder / "02 Track 2.flac")]
    assert diff.missing_on_disk == [os.path.normpath(str(folder / "09 Gone.flac"))]
    ids = {Path(r.path).name: r.lidarr_file_id for r in rows()}
    assert ids == {"01 Track 1.flac": 10, "02 Track 2.flac": None}


async def test_compare_without_lidarr_is_not_checked(settings):
    settings.lidarr_api_key = ""
    diff = await library.compare_with_lidarr(settings)
    assert not diff.checked


def test_queue_index_dedupes(settings):
    assert library.queue_index(settings.music_dir / "Radiohead" / "A").kind == "index"
    assert library.queue_index(settings.music_dir / "Radiohead" / "A") is None  # same folder already queued
    assert library.queue_index(settings.music_dir / "Radiohead" / "B") is not None
    full = library.queue_index()
    assert full is not None and full.payload == {}
    assert library.queue_index() is None
    assert library.queue_index(settings.music_dir / "Radiohead" / "C") is None  # the full pass covers it
    with session() as s:
        assert len(s.exec(select(Job).where(Job.kind == "index")).all()) == 3


async def test_run_index_job_records_status(settings, monkeypatch):
    album(settings.music_dir, tracks=2, musicbrainz_albumid=REL, musicbrainz_releasegroupid=RG)
    settings.lidarr_api_key = ""
    job = library.queue_index()
    await library.run_index(job)
    st = library.status()
    assert st["scope"] == "library" and st["files"] == 2 and st["scan"]["added"] == 2
    assert st["lidarr"]["checked"] is False
    summary = library.summary()
    assert (summary.files, summary.folders, summary.with_ids) == (2, 1, 2)
    assert library.untagged_folders() == []


def test_library_page(settings):
    album(settings.music_dir, tracks=2)  # no IDs: shows under folders without IDs
    library.scan(settings.music_dir, settings)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    html = client.get("/library").text
    assert "Library" in html and ">2<" in html
    assert "OK Computer (1997)" in html
    assert "Connect Lidarr" in html
    response = client.post("/library/reindex", follow_redirects=False)
    assert response.status_code == 303
    with session() as s:
        assert s.exec(select(Job).where(Job.kind == "index")).first() is not None
