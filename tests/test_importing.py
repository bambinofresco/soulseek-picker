"""Import pipeline: a fixture album in a temp directory, beets in pretend mode, fake Lidarr and Plex."""

import struct
from pathlib import Path

import pytest
from mutagen.flac import FLAC

from picker import audio, beets, importing
from picker.db import session
from picker.models import Album, Download, Import, Job

RG, REL, OTHER = "rg-okc", "rel-chosen", "rel-other"


def make_flac(path: Path, n: int) -> None:
    """A valid, silent FLAC: STREAMINFO only (44.1 kHz, 16-bit stereo, 0 samples) plus tags."""
    info = (44100 << 44) | (1 << 41) | (15 << 36)
    block = struct.pack(">HH", 4096, 4096) + b"\0" * 6 + struct.pack(">Q", info) + b"\0" * 16
    path.write_bytes(b"fLaC" + bytes([0x80, 0, 0, 34]) + block)
    f = FLAC(path)
    f.update({"title": f"Track {n}", "tracknumber": str(n), "artist": "Radiohead", "album": "OK Computer"})
    f.save()


class FakeLidarr:
    def __init__(self, music: Path, releases=(REL, OTHER)):
        self.music = music
        self.artist = None
        self.added = None
        self.commands = []
        self.updated = None
        self.releases = [{"foreignReleaseId": r, "monitored": r == OTHER} for r in releases]

    async def artist_by_mbid(self, mbid):
        return self.artist

    async def lookup_artist(self, mbid):
        return {"foreignArtistId": mbid, "artistName": "Radiohead"}

    async def add_artist(self, lookup, **kw):
        self.added = kw
        self.artist = {"id": 5, "path": str(self.music / "Radiohead")}
        return self.artist

    async def command(self, name, **body):
        self.commands.append((name, body))
        return {"id": len(self.commands)}

    async def wait_for_command(self, cid):
        return {"status": "completed"}

    async def album_by_foreign_id(self, rg):
        return {"id": 9, "foreignAlbumId": rg, "releases": self.releases}

    async def update_album(self, album):
        self.updated = album
        return album

    async def album(self, album_id):
        return {"id": album_id, "statistics": {"trackFileCount": 3, "trackCount": 3}}


class FakePlex:
    def __init__(self):
        self.scanned = []

    async def scan_path(self, path):
        self.scanned.append(path)


class FakeServices:
    def __init__(self, lidarr, plex):
        self.lidarr, self.plex = lidarr, plex


@pytest.fixture
def env(settings, monkeypatch):
    settings.lidarr_api_key = "k"
    settings.plex_token = "t"
    settings.plex_section_id = "1"
    settings.plex_music_path = "/data/music"
    settings.beets_pretend = True
    lidarr, plex = FakeLidarr(settings.music_dir), FakePlex()
    monkeypatch.setattr(importing, "services", lambda: FakeServices(lidarr, plex))

    def enqueue(kind, payload, rg_mbid=None):
        with session() as s:
            job = Job(kind=kind, payload=payload, rg_mbid=rg_mbid)
            s.add(job)
            s.commit()
            s.refresh(job)
        return job

    monkeypatch.setattr(importing.worker, "enqueue", enqueue)

    async def fake_tool(args, timeout=0):
        if args[0] == "sox":
            Path(args[args.index("-o") + 1]).write_bytes(b"\x89PNG fake")
        return 0, ""

    monkeypatch.setattr(audio, "run_tool", fake_tool)

    with session() as s:
        s.add(Album(rg_mbid=RG, artist="Radiohead", artist_mbid="a74b1b7f", title="OK Computer",
                    first_release_date="1997-05-21"))
        s.commit()
    return settings, lidarr, plex


def landed_download(settings, job_id=7, nested=True) -> Download:
    root = settings.download_dir / "picker" / str(job_id)
    album = root / "OK Computer [CDP 7243 8 55229 2 5]" if nested else root
    album.mkdir(parents=True)
    for n in (1, 2, 3):
        make_flac(album / f"{n:02d} Track.flac", n)
    (album / "folder.jpg").write_bytes(b"jpg")
    files = [{"filename": rf"m\OKC\{p.name}", "size": p.stat().st_size} for p in album.iterdir()]
    with session() as s:
        dl = Download(candidate_id=1, search_id=1, rg_mbid=RG, release_mbid=REL, username="alice",
                      folder=r"m\OKC", destination=f"picker/{job_id}", files=files, state="completed")
        s.add(dl)
        s.commit()
        s.refresh(dl)
    return dl


async def run(imp: Import) -> Import:
    with session() as s:
        imp = s.get(Import, imp.id)
        job = s.get(Job, imp.job_id)
    await importing.run_import(job)
    with session() as s:
        return s.get(Import, imp.id)


async def test_full_pipeline_with_beets_pretend(env):
    settings, lidarr, plex = env
    dl = landed_download(settings)
    imp = importing.start_import(dl)

    imp = await run(imp)
    assert imp.state == "review" and imp.step == "review"
    assert [r["ok"] for r in imp.integrity] == [True, True, True]
    assert imp.source_dir.endswith("OK Computer [CDP 7243 8 55229 2 5]")  # lone subfolder unwrapped
    assert len(imp.spectrograms) == 2
    assert (settings.spectrogram_dir / str(imp.id) / imp.spectrograms[0]).exists()

    imp = await run(importing.approve(imp))
    assert imp.state == "imported", imp.error
    target = settings.music_dir / "Radiohead" / "OK Computer (1997)"
    assert Path(imp.album_dir) == target
    assert sorted(p.name for p in target.iterdir()) == ["01 Track.flac", "02 Track.flac", "03 Track.flac", "folder.jpg"]
    assert not Path(dl.destination).exists() and not (settings.download_dir / "picker" / "7" / "OK Computer").exists()

    # beets ran for real, in pretend mode, on the placed folder
    assert "01 Track.flac" in imp.beets_output
    assert (settings.beets_dir / "config.yaml").read_text().startswith(f"directory: {settings.music_dir.as_posix()}")

    # Lidarr: artist added without a back-catalogue search, edition selected, folder rescanned
    assert lidarr.added["monitor"] == "none" and lidarr.added["monitor_new_items"] == "new"
    assert lidarr.commands[0] == ("RefreshArtist", {"artistId": 5})
    assert [r["foreignReleaseId"] for r in lidarr.updated["releases"] if r["monitored"]] == [REL]
    assert lidarr.commands[-1] == ("RescanFolders", {"folders": [str(target.parent)], "filter": "matched"})

    # Plex sees the library under its own path
    assert plex.scanned == ["/data/music/Radiohead/OK Computer (1997)"]
    assert imp.notes == ["Added Radiohead to Lidarr (monitor none, new items new)"]


async def test_existing_target_blocks_and_is_never_overwritten(env):
    settings, lidarr, plex = env
    target = settings.music_dir / "Radiohead" / "OK Computer (1997)"
    target.mkdir(parents=True)
    (target / "keep.flac").write_bytes(b"precious")
    imp = await run(importing.start_import(landed_download(settings)))
    imp = await run(importing.approve(imp))
    assert imp.state == "blocked" and "never overwrites" in imp.error
    assert [p.name for p in target.iterdir()] == ["keep.flac"]
    assert Path(imp.source_dir).is_dir()  # download untouched

    target.rename(target.with_name("OK Computer (1997) old"))
    imp = await run(importing.retry(imp))
    assert imp.state == "imported"


def _reload(imp):
    with session() as s:
        return s.get(Import, imp.id)


async def test_reject_deletes_only_the_download(env):
    settings, *_ = env
    dl = landed_download(settings, job_id=8)
    imp = await run(importing.start_import(dl))
    importing.reject(imp)
    assert not (settings.download_dir / "picker" / "8").exists()
    assert _reload(imp).state == "rejected"

    outside = settings.music_dir / "Somebody"
    outside.mkdir()
    imp2 = _reload(imp)
    imp2.source_dir, imp2.state = str(outside), "review"
    with pytest.raises(importing.Blocked, match="refusing"):
        importing.reject(imp2)
    assert outside.exists()


async def test_verification_failure_stops_before_review(env, monkeypatch):
    settings, *_ = env

    async def broken_flac(args, timeout=0):
        return (1, "01 Track.flac: ERROR while decoding data") if args[0] == "flac" else (0, "")

    monkeypatch.setattr(audio, "run_tool", broken_flac)
    imp = importing.start_import(landed_download(settings, job_id=9))
    with pytest.raises(Exception, match="integrity"):
        await run(imp)
    assert _reload(imp).state == "failed"


async def test_edition_not_in_lidarr_is_noted(env):
    settings, lidarr, _ = env
    lidarr.releases = [{"foreignReleaseId": OTHER, "monitored": True}]
    imp = await run(importing.start_import(landed_download(settings, job_id=10)))
    imp = await run(importing.approve(imp))
    assert imp.state == "imported"
    assert lidarr.updated is None
    assert any("metadata profile" in n for n in imp.notes)


async def test_beets_skip_then_force(env, monkeypatch):
    settings, *_ = env
    settings.beets_pretend = False
    calls = []
    state = {"in_library": False}

    async def fake_beet(s, args, timeout):
        calls.append(args)
        if args[0] == "ls" or args[2:3] == ["ls"]:
            return 0, str(settings.music_dir / "Radiohead" / "OK Computer (1997)") if state["in_library"] else ""
        if "-c" in args:
            state["in_library"] = True
            return 0, "Tagging: Radiohead - OK Computer"
        return 0, "Skipping."

    monkeypatch.setattr(beets, "_beet", fake_beet)
    imp = await run(importing.start_import(landed_download(settings, job_id=11)))
    imp = await run(importing.approve(imp))
    assert imp.state == "skipped" and imp.step == "beets"
    assert "Skipping." in imp.beets_output

    imp = await run(importing.force_apply(imp))
    assert imp.state == "imported"
    forced = [a for a in calls if "-c" in a][0]
    assert forced[1].endswith("force.yaml") and "--search-id" in forced and REL in forced
    assert "strong_rec_thresh: 1.01" in (settings.beets_dir / "force.yaml").read_text()


async def test_auto_import_runs_straight_through(env):
    settings, *_ = env
    settings.auto_import = True
    imp = await run(importing.start_import(landed_download(settings, job_id=12)))
    assert imp.state == "imported"


def test_names_and_paths(settings):
    album = Album(rg_mbid="x", artist="AC/DC", title="Who Made Who: Live?", first_release_date="1986")
    assert importing.album_folder_name("{album} ({year})", album) == "Who Made Who - Live (1986)"
    undated = Album(rg_mbid="y", artist="A", title="B", first_release_date=None)
    assert importing.album_folder_name("{album} ({year})", undated) == "B"
    assert importing.safe_name("AC/DC") == "AC+DC"
    settings.plex_music_path = ""
    assert importing.plex_path(settings.music_dir / "A" / "B", settings).endswith("/music/A/B")
    assert importing.within(settings.music_dir / "A", settings.music_dir)
    assert not importing.within(settings.download_dir, settings.music_dir)
