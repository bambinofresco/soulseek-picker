"""Tracks-only mode (spec 7.1): finding the wanted tracks in a folder, downloading only those, importing a partial album."""

import pytest
from sqlmodel import select

from picker import beets, downloading, importing, playlists, queueing, tracks
from picker.config import Settings
from picker.db import session
from picker.editions import Edition, edition_from_release
from picker.models import Album, Candidate, Download, Import, Playlist, PlaylistAlbum, PlaylistTrack, QueueItem
from picker.owned import Owned
from tests.test_queueing import cand, finish_search, item, world  # noqa: F401  (fixture)

REC = [f"rec-{n}" for n in range(1, 13)]
LENGTHS = [280, 245, 300, 190, 260, 215, 330, 205, 270, 240, 255, 310]


def edition(**kw) -> Edition:
    base = dict(id="gb", title="OK Computer", date="1997-06-16", country="GB", formats=["CD"], track_counts=[12],
                lengths=[[float(x) for x in LENGTHS]], recordings=[REC])
    base.update(kw)
    return Edition(**base)


def files(names, lengths=LENGTHS, disc=None):
    return [{"filename": rf"m\\OKC\\{name}", "size": 30_000_000, "kind": "audio", "disc": disc, "length": length}
            for name, length in zip(names, lengths)]


def folder(names, lengths=LENGTHS, disc=None, extra=()):
    c = Candidate(search_id=1, username="alice", folder=r"m\OKC", query="q", audio_count=len(names),
                  files=files(names, lengths, disc) + list(extra))
    return c


def test_track_numbers_from_names():
    assert tracks.file_track_number(r"m\A\07 Karma Police.flac") == 7
    assert tracks.file_track_number(r"m\A\07. Karma Police.flac") == 7
    assert tracks.file_track_number(r"m\A\1-07 Karma Police.flac") == 7
    assert tracks.file_track_number(r"m\A\07-Karma Police.flac") == 7
    assert tracks.file_track_number(r"m\A\Karma Police.flac") is None


def test_wanted_files_by_duration_and_number():
    c = folder([f"{n:02d} Track {n}.flac" for n in range(1, 13)])
    assert tracks.wanted_files(c, edition(), ["rec-7", "rec-2"]) == [r"m\\OKC\\07 Track 7.flac", r"m\\OKC\\02 Track 2.flac"]
    # Numbers missing: duration alone decides.
    c = folder([f"Track {n}.flac" for n in range(1, 13)])
    assert tracks.wanted_files(c, edition(), ["rec-7"]) == [r"m\\OKC\\Track 7.flac"]
    # Two tracks of the same length: the number breaks the tie.
    lengths = list(LENGTHS)
    lengths[3] = lengths[6]
    c = folder([f"{n:02d}.flac" for n in range(1, 13)], lengths)
    assert tracks.wanted_files(c, edition(), ["rec-7"]) == [r"m\\OKC\\07.flac"]
    # Lengths unknown on the uploader's side: the number alone.
    c = folder([f"{n:02d}.flac" for n in range(1, 13)], [None] * 12)
    assert tracks.wanted_files(c, edition(), ["rec-4"]) == [r"m\\OKC\\04.flac"]
    # A recording the edition does not carry, or a track that cannot be told apart: not eligible.
    assert tracks.wanted_files(c, edition(), ["rec-99"]) is None
    c = folder([f"Track {n}.flac" for n in range(1, 13)], [None] * 12)
    assert tracks.wanted_files(c, edition(), ["rec-4"]) is None
    assert tracks.wanted_files(c, edition(), []) is None


def test_wanted_files_across_discs():
    two = edition(formats=["CD", "CD"], track_counts=[6, 6], lengths=[[float(x) for x in LENGTHS[:6]], [float(x) for x in LENGTHS[6:]]],
                  recordings=[REC[:6], REC[6:]])
    # Disc subfolders.
    c = Candidate(search_id=1, username="a", folder=r"m\OKC", query="q", audio_count=12,
                  files=files([f"CD1\\{n:02d}.flac" for n in range(1, 7)], LENGTHS[:6], disc="CD1")
                  + files([f"CD2\\{n:02d}.flac" for n in range(1, 7)], LENGTHS[6:], disc="CD2"))
    assert tracks.wanted_files(c, two, ["rec-8", "rec-1"]) == ["m\\\\OKC\\\\CD2\\02.flac", "m\\\\OKC\\\\CD1\\01.flac"]
    # One folder holding both discs, numbered straight through.
    c = folder([f"{n:02d}.flac" for n in range(1, 13)])
    assert tracks.wanted_files(c, two, ["rec-8"]) == [r"m\\OKC\\08.flac"]


def test_edition_carries_recordings():
    release = {"id": "r", "title": "T", "media": [{"position": 1, "format": "CD", "tracks": [
        {"length": 280000, "recording": {"id": "REC-A"}}, {"recording": {"id": "rec-b", "length": 245000}}]}]}
    ed = edition_from_release(release)
    assert ed.recordings == [["REC-A", "rec-b"]] and ed.position_of("rec-a") == (0, 0) and ed.position_of("rec-b") == (0, 1)
    assert ed.position_of("nope") is None


def test_files_to_fetch_only_keeps_wanted_audio_and_sidecars(settings):
    c = folder([f"{n:02d}.flac" for n in range(1, 4)], LENGTHS[:3],
               extra=[{"filename": r"m\\OKC\\rip.log", "size": 100, "kind": "log"}, {"filename": r"m\\OKC\\cover.jpg", "size": 100, "kind": "image"}])
    names = [f["filename"] for f in downloading.files_to_fetch(c, settings, only={r"m\\OKC\\02.flac"})]
    assert names == [r"m\\OKC\\02.flac", r"m\\OKC\\rip.log", r"m\\OKC\\cover.jpg"]
    assert len(downloading.files_to_fetch(c, settings)) == 5
    with session() as s:
        s.add(c)
        s.commit()
        s.refresh(c)
    dl = downloading.start_download(c, "rg", "gb", only={r"m\\OKC\\02.flac"})
    assert dl.partial and len(dl.files) == 3
    imp = importing.start_import(dl)
    assert imp.partial


def test_beets_overlay_for_partial_imports(settings):
    beets.ensure_config(settings)
    text = (settings.beets_dir / "partial.yaml").read_text(encoding="utf-8")
    assert "missing_tracks: 0.0" in text and "album_id: 0.0" in text
    assert "missing_tracks" not in (settings.beets_dir / "pinned.yaml").read_text(encoding="utf-8")


async def test_partial_import_skips_rip_check_and_unmonitors_in_lidarr(settings, monkeypatch, tmp_path):
    settings.rip_verify, settings.transcode_check, settings.lidarr_api_key = True, False, "k"
    from tests.test_library import make_flac

    src = settings.download_dir / "picker" / "3" / "OKC"
    make_flac(src / "02.flac", 2)
    with session() as s:
        s.add(Album(rg_mbid="rg", artist="Radiohead", title="OK Computer", min_track_count=12))
        dl = Download(candidate_id=1, search_id=1, rg_mbid="rg", username="a", folder="f", destination="picker/3",
                      release_mbid="gb", partial=True, files=[{"filename": "f\\02.flac", "size": 1}])
        s.add(dl)
        s.commit()
        s.refresh(dl)
        imp = Import(download_id=dl.id, rg_mbid="rg", release_mbid="gb", source_dir=str(src), partial=True)
        s.add(imp)
        s.commit()
        s.refresh(imp)

    async def no_spectrograms(paths, out):
        return [], None

    async def all_good(root, expected):
        return True, [{"name": "02.flac", "ok": True, "detail": "", "length": 200}], None

    monkeypatch.setattr(importing.audio, "spectrograms", no_spectrograms)
    monkeypatch.setattr(importing.audio, "verify", all_good)  # no flac binary on the test machine
    await importing._verify(imp, settings)
    with session() as s:
        imp = s.get(Import, imp.id)
    assert imp.rip_check == {"applicable": False, "discs": [], "mismatched": [], "summary": "not checked (partial album)"}

    class Lidarr:
        def __init__(self):
            self.updated = None

        async def album_by_foreign_id(self, rg):
            return {"id": 5, "monitored": True, "releases": [{"foreignReleaseId": "gb", "monitored": False},
                                                             {"foreignReleaseId": "jp", "monitored": True}]}

        async def update_album(self, album):
            self.updated = album
            return album

        async def command(self, name, **body):
            return {"id": 1}

        async def wait_for_command(self, cid):
            return {}

        async def album(self, album_id):
            return {"statistics": {"trackFileCount": 1, "trackCount": 12}}

    lidarr = Lidarr()

    class Services:
        pass

    Services.lidarr = lidarr
    monkeypatch.setattr(importing, "services", lambda: Services())
    imp.album_dir = str(settings.music_dir / "Radiohead" / "OK Computer (1997)")
    await importing._sync_lidarr(imp, settings)
    assert lidarr.updated["monitored"] is False
    assert [r["monitored"] for r in lidarr.updated["releases"]] == [True, False]
    with session() as s:
        imp = s.get(Import, imp.id)
    assert any("unmonitored" in n for n in imp.notes)


async def test_queue_tracks_mode_downloads_only_the_wanted_files(world, settings, monkeypatch):
    calls = []

    def fake_start_download(candidate, rg, release=None, upgrade_of=None, only=None):
        calls.append((candidate.username, release, only, upgrade_of))
        with session() as s:
            dl = Download(candidate_id=candidate.id, search_id=candidate.search_id, rg_mbid=rg, username=candidate.username,
                          folder=candidate.folder, release_mbid=release, destination="p", state="queued", partial=only is not None)
            s.add(dl)
            s.commit()
            s.refresh(dl)
        return dl

    monkeypatch.setattr(queueing, "start_download", fake_start_download)
    ed = edition()

    async def editions(rg):
        return [ed]

    monkeypatch.setattr(queueing.albums, "album_editions", editions)
    queueing.add("rg-1", "tracks", wanted=["rec-7", "rec-2"])
    await queueing.tick(settings)
    good = cand("alice", editions=("gb",), files=files([f"{n:02d}.flac" for n in range(1, 13)]))
    unusable = cand("bob", editions=("gb",), files=files([f"Track {n}.flac" for n in range(1, 13)], [None] * 12))  # no lengths, no numbers
    finish_search("rg-1", [unusable, good])
    await queueing.tick(settings)
    it = item("rg-1")
    assert it.state == "downloading"
    assert calls == [("alice", "gb", {r"m\\OKC\\07.flac", r"m\\OKC\\02.flac"}, None)]

    # The download fails: the item goes back to searched so the next eligible folder is worked out again.
    with session() as s:
        dl = s.get(Download, it.download_id)
        dl.state = "failed"
        s.add(dl)
        s.commit()
    await queueing.tick(settings)  # back to searched, then decided again in the same tick: bob's folder cannot serve
    it = item("rg-1")
    assert it.state == "none_found" and "every wanted track" in it.note and len(calls) == 1


async def test_queue_tracks_mode_with_nothing_wanted_is_done(world, settings):
    queueing.add("rg-1", "tracks", wanted=[])
    await queueing.tick(settings)
    finish_search("rg-1", [cand("alice")])
    await queueing.tick(settings)
    assert item("rg-1").state == "done"


async def test_review_offers_tracks_plus_for_a_partial_copy(settings, monkeypatch):
    with session() as s:
        s.add(Album(rg_mbid="rg-1", artist="Radiohead", title="OK Computer", min_track_count=12))
        playlist = Playlist(name="P", state="review", full_albums=False)
        s.add(playlist)
        s.commit()
        s.refresh(playlist)
        for n, rec in enumerate(["rec-7", "rec-2"], 1):
            s.add(PlaylistTrack(playlist_id=playlist.id, position=n, title=f"T{n}", artist="Radiohead", album="OK Computer",
                                rg_mbid="rg-1", recording_mbid=rec, resolution="isrc"))
        s.commit()
    from picker import owned as owned_module

    async def partial_owned(rgs, _settings):
        return {"rg-1": Owned("index", "gb", str(settings.music_dir / "Radiohead" / "OK Computer (1997)"), have=5)}

    monkeypatch.setattr(owned_module, "owned_release_groups", partial_owned)
    r = await playlists.review(playlist.id, settings)
    g = r.groups[0]
    assert g.partial_copy and g.mode == "upgrade" and r.counts["upgrade"] == 1
    playlists.set_mode(playlist.id, "rg-1", "tracks_plus")
    r = await playlists.review(playlist.id, settings)
    assert r.groups[0].mode == "tracks_plus" and r.groups[0].estimate_mb == (2 + 5) * 30

    # Start run: tracks_plus becomes a tracks item that replaces the partial copy, wanting the union.
    from picker import library
    from tests.test_library import make_flac

    folder = settings.music_dir / "Radiohead" / "OK Computer (1997)"
    make_flac(folder / "01.flac", 1, musicbrainz_trackid="99999999-0000-0000-0000-000000000001")
    library.scan(settings.music_dir, settings)
    monkeypatch.setattr(queueing, "ensure_running", lambda: None)
    monkeypatch.setattr(queueing.plexsync, "queue_sync", lambda *a, **k: None)
    created = await queueing.start_run(playlist.id, settings)
    assert created == 1
    it = item("rg-1")
    assert it.mode == "tracks" and it.owned["path"] == str(folder)
    assert set(it.wanted) == {"rec-7", "rec-2", "99999999-0000-0000-0000-000000000001"}
