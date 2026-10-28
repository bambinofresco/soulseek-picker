"""Is this album already in the library? (picker/owned.py)"""

import pytest

from picker import beets, owned
from picker.db import session
from picker.models import Import


class FakeLidarr:
    def __init__(self, album=None, files=()):
        self.album_data, self.files = album, list(files)

    async def album_by_foreign_id(self, rg):
        return self.album_data

    async def track_files(self, album_id):
        return self.files


class Services:
    def __init__(self, lidarr):
        self.lidarr = lidarr


@pytest.fixture
def library(monkeypatch):
    data = {}

    async def fake_album_list(settings):
        return data

    monkeypatch.setattr(beets, "album_list", fake_album_list)
    return data


async def test_nothing_known(settings, library, monkeypatch):
    monkeypatch.setattr(owned, "services", lambda: Services(FakeLidarr()))
    assert await owned.check_album("rg-1", settings) is None
    assert await owned.owned_release_groups(["rg-1"], settings) == {}


async def test_picker_history_wins_when_its_folder_still_exists(settings, library, monkeypatch, tmp_path):
    folder = tmp_path / "music" / "Radiohead" / "OK Computer (1997)"
    folder.mkdir(parents=True)
    gone = tmp_path / "music" / "Radiohead" / "Gone (2001)"
    with session() as s:
        s.add(Import(rg_mbid="rg-1", release_mbid="jp-cd", state="imported", step="done", album_dir=str(folder)))
        s.add(Import(rg_mbid="rg-2", release_mbid="x", state="imported", step="done", album_dir=str(gone)))
        s.add(Import(rg_mbid="rg-3", release_mbid="y", state="rejected", step="verify"))
        s.commit()
    settings.lidarr_api_key = ""
    have = await owned.check_album("rg-1", settings)
    assert have.source == "picker" and have.release_mbid == "jp-cd" and have.path == str(folder) and have.import_id
    assert await owned.check_album("rg-2", settings) is None  # folder deleted since
    assert await owned.check_album("rg-3", settings) is None
    found = await owned.owned_release_groups(["rg-1", "rg-2", "rg-3"], settings)
    assert list(found) == ["rg-1"]


async def test_lidarr_with_track_files_counts(settings, library, monkeypatch):
    settings.lidarr_api_key = "k"
    album = {"id": 9, "statistics": {"trackFileCount": 11, "trackCount": 12},
             "releases": [{"foreignReleaseId": "uk-cd", "monitored": False}, {"foreignReleaseId": "jp-cd", "monitored": True}]}
    files = [{"path": "/mediadata/media/music/Radiohead/OK Computer (1997)/01 Airbag.flac"}]
    monkeypatch.setattr(owned, "services", lambda: Services(FakeLidarr(album, files)))
    have = await owned.check_album("rg-1", settings)
    assert have.source == "lidarr" and have.release_mbid == "jp-cd" and (have.have, have.want) == (11, 12)
    assert have.path == "/mediadata/media/music/Radiohead/OK Computer (1997)"
    # Lidarr knows the album but has no files: not owned.
    monkeypatch.setattr(owned, "services", lambda: Services(FakeLidarr({"id": 9, "statistics": {"trackFileCount": 0}})))
    assert await owned.check_album("rg-1", settings) is None


async def test_beets_library_is_the_last_word_and_is_cached(settings, library, monkeypatch, tmp_path):
    settings.lidarr_api_key = ""
    folder = tmp_path / "music" / "Radiohead" / "Kid A (2000)"
    folder.mkdir(parents=True)
    library["rg-kid-a"] = {"release": "kid-a-cd", "path": str(folder)}
    library["rg-gone"] = {"release": "z", "path": str(tmp_path / "music" / "nope")}
    have = await owned.check_album("rg-kid-a", settings)
    assert have.source == "beets" and have.release_mbid == "kid-a-cd" and have.path == str(folder)
    assert await owned.check_album("rg-gone", settings) is None
    library.clear()  # the listing is cached for a while
    assert (await owned.owned_release_groups(["rg-kid-a"], settings))["rg-kid-a"].source == "beets"


async def test_beets_album_list_parses_the_listing(settings, monkeypatch):
    async def fake_beet(s, args, timeout):
        assert args[:3] == ["ls", "-a", "-f"]
        return 0, "rg-1\trel-1\t/music/A/B (1997)\n\trel-x\t/music/untagged\nrg-2\t\t/music/C\n"

    monkeypatch.setattr(beets, "_beet", fake_beet)
    assert await beets.album_list(settings) == {"rg-1": {"release": "rel-1", "path": "/music/A/B (1997)"},
                                                "rg-2": {"release": None, "path": "/music/C"}}
