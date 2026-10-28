"""beets' distance breakdown for a pinned release (picker/beets_explain.py), without MusicBrainz."""

import os

import pytest
from mutagen.flac import FLAC

from picker.beets_explain import PENALTY_NAMES, explain
from tests.test_importing import make_flac


@pytest.fixture
def beets_config(tmp_path, monkeypatch):
    beets_dir = tmp_path / "beets"
    beets_dir.mkdir()
    (beets_dir / "config.yaml").write_text("plugins: musicbrainz\nmatch:\n  strong_rec_thresh: 0.10\n", encoding="utf-8")
    monkeypatch.setenv("BEETSDIR", str(beets_dir))
    from beets import config
    config.clear()
    config.read()
    return config


def test_explain_pairs_files_with_tracks_and_names_what_differs(tmp_path, beets_config):
    from beets.autotag.hooks import AlbumInfo, TrackInfo
    from beets.library import Item

    album = tmp_path / "album"
    album.mkdir()
    for n in (1, 2, 3):
        make_flac(album / f"{n:02d} Track.flac", n)
    f = FLAC(album / "02 Track.flac")
    f["title"] = "Something Else"
    f.save()
    items = [Item.from_path(str(p)) for p in sorted(album.glob("*.flac"))]
    tracks = [TrackInfo(title=f"Track {n}", track_id=f"t{n}", index=n, medium=1, medium_index=n, length=200.0) for n in (1, 2, 3)]
    tracks.append(TrackInfo(title="Track 4", track_id="t4", index=4, medium=1, medium_index=4, length=180.0))
    info = AlbumInfo(album="OK Computer", album_id="rel-chosen", artist="Radiohead", artist_id="a", tracks=tracks, mediums=1, year=1997)

    out = explain(items, info, beets_config["match"]["strong_rec_thresh"].as_number())
    assert out["release"] == "rel-chosen" and out["album"] == "Radiohead - OK Computer"
    assert 0 < out["similarity"] < 0.9 and out["threshold"] == 0.9
    assert [p["key"] for p in out["penalties"]] == ["tracks", "missing_tracks"]
    assert all(p["name"] == PENALTY_NAMES[p["key"]] for p in out["penalties"])
    by_file = {t["file"]: t for t in out["tracks"]}
    assert by_file["02 Track.flac"]["title"] == "Track 2" and by_file["02 Track.flac"]["file_title"] == "Something Else"
    assert [p["key"] for p in by_file["02 Track.flac"]["penalties"]] == ["track_title", "track_length"]
    assert by_file["01 Track.flac"]["file_length"] is None  # the fixture FLACs hold no samples
    assert out["missing"] == [{"index": 4, "medium": 1, "title": "Track 4", "length": 180.0}]
    assert out["unmatched"] == []


def test_module_runs_as_a_script_and_reports_an_empty_folder(tmp_path, beets_config):
    import subprocess
    import sys

    empty = tmp_path / "empty"
    empty.mkdir()
    overlay = tmp_path / "beets" / "pinned.yaml"
    overlay.write_text("match:\n  distance_weights:\n    album_id: 0.0\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "picker.beets_explain", str(tmp_path / "beets"), str(overlay), "x", str(empty)],
        capture_output=True, text=True, env={**os.environ, "BEETSDIR": str(tmp_path / "beets")}, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert '"error": "no audio files in' in proc.stdout
