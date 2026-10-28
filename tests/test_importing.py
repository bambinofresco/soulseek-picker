"""Import pipeline: a fixture album in a temp directory, beets in pretend mode, fake Lidarr and Plex."""

import struct
from pathlib import Path

import pytest
from mutagen.flac import FLAC
from sqlmodel import select

from picker import albums, audio, beets, importing
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
        return {"id": 9, "foreignAlbumId": rg, "title": "OK Computer", "releaseDate": "1997-05-21T00:00:00Z",
                "releases": self.releases}

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

    async def fake_editions(rg):
        return [{"id": REL, "title": "OK Computer", "date": "1997-07-01", "country": "US",
                 "label-info": [{"label": {"name": "Capitol Records"}, "catalog-number": "CDP 7243 8 55229 2 5"}],
                 "media": [{"position": 1, "format": "CD", "tracks": [{"length": 1}] * 3}]},
                {"id": OTHER, "title": "OK Computer", "date": "2009-03-24", "country": "GB", "disambiguation": "remastered",
                 "media": [{"position": 1, "format": "CD", "tracks": [{"length": 1}] * 3}]}]

    monkeypatch.setattr(albums, "editions", fake_editions)

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
    # The tag snapshot shown on the review page and in History reads the files on disk.
    assert imp.tags["file"] == "01 Track.flac" and imp.tags["tags"]["album"] == "OK Computer"
    assert (settings.beets_dir / "config.yaml").read_text().startswith(f"directory: {settings.music_dir.as_posix()}")

    # Lidarr: artist added without a back-catalogue search, edition selected, folder rescanned
    assert lidarr.added["monitor"] == "none" and lidarr.added["monitor_new_items"] == "new"
    assert lidarr.commands[0] == ("RefreshArtist", {"artistIds": [5]})
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
        if any(str(a).endswith("force.yaml") for a in args):
            state["in_library"] = True
            return 0, "Tagging: Radiohead - OK Computer"
        return 0, "Skipping."

    monkeypatch.setattr(beets, "_beet", fake_beet)

    async def fake_explain(s, album_dir, release):
        assert Path(album_dir).name == "OK Computer (1997)" and release == REL
        return {"similarity": 0.827, "threshold": 0.9, "penalties": [{"key": "tracks", "name": "track differences (below)", "distance": 0.17}],
                "tracks": [], "missing": [], "unmatched": [], "album": "Radiohead - OK Computer", "release": REL}

    monkeypatch.setattr(beets, "explain_skip", fake_explain)
    imp = await run(importing.start_import(landed_download(settings, job_id=11)))
    imp = await run(importing.approve(imp))
    assert imp.state == "skipped" and imp.step == "beets"
    assert "Skipping." in imp.beets_output
    assert imp.beets_match["similarity"] == 0.827  # the breakdown the Review page shows
    with session() as s:
        from picker.models import JobEvent
        events = [e.message for e in s.exec(select(JobEvent)).all()]
    assert any("(82.7%, needs 90%)" in e for e in events), events

    imp = await run(importing.force_apply(imp))
    assert imp.state == "imported"
    imports = [a for a in calls if "import" in a]
    assert imports[0][1].endswith("pinned.yaml")  # every import ignores tags the picker overwrites
    forced = [a for a in calls if any(str(x).endswith("force.yaml") for x in a)][0]
    assert forced[1].endswith("force.yaml") and "--search-id" in forced and REL in forced
    assert "strong_rec_thresh: 1.01" in (settings.beets_dir / "force.yaml").read_text()


def _spectrum(monkeypatch, kind="full", error=None):
    from picker import spectrum

    async def fake(path, rate, length, cutoff_db=30.0):
        if error:
            raise RuntimeError(error)
        detail = {"full": "energy continues to the top of the spectrum",
                  "lossy": "spectrum ends at 16.0 kHz (62 dB cliff), consistent with a ~128 kbps lossy source"}[kind]
        return spectrum.Verdict(kind, 16000 if kind == "lossy" else None, None, detail)

    monkeypatch.setattr(spectrum, "analyse_file", fake)


async def test_auto_import_goes_straight_in_when_every_check_passes(env, monkeypatch):
    settings, *_ = env
    settings.auto_import = True
    _spectrum(monkeypatch, "full")
    imp = await run(importing.start_import(landed_download(settings, job_id=12)))
    assert imp.state == "imported"


@pytest.mark.parametrize("setup, reason", [
    (lambda mp, imp_kw: _spectrum(mp, "lossy"), "suggests a transcode"),
    (lambda mp, imp_kw: _spectrum(mp, error="ffmpeg is not installed"), "could not run"),
    (lambda mp, imp_kw: (_spectrum(mp, "full"), imp_kw.update(release=None)), "no edition chosen"),
])
async def test_auto_import_waits_for_review_when_a_check_does_not_pass(env, monkeypatch, setup, reason):
    settings, *_ = env
    settings.auto_import = True
    kw = {"release": REL}
    setup(monkeypatch, kw)
    dl = landed_download(settings, job_id=13)
    if kw["release"] is None:
        with session() as s:
            row = s.get(Download, dl.id)
            row.release_mbid = None
            s.add(row)
            s.commit()
            dl = row
    imp = await run(importing.start_import(dl))
    assert imp.state == "review"
    with session() as s:
        from picker.models import JobEvent
        events = [e.message for e in s.exec(select(JobEvent)).all()]
    assert any(reason in e for e in events), events


def _rip(monkeypatch, result):
    from picker import ripverify

    async def fake(root, files, settings):
        return result

    monkeypatch.setattr(ripverify, "verify_folder", fake)


VERIFIED = {"applicable": True, "mismatched": [], "summary": "CTDB verified (confidence 40), AccurateRip 3/3 (confidence 20)",
            "discs": [{"applicable": True, "toc_source": "lengths", "files": 3,
                       "accuraterip": {"status": "verified", "offset": 0, "confidence": 20, "matched": 3, "known": 3, "tracks": []},
                       "ctdb": {"status": "verified", "offset": 0, "confidence": 40, "entries": 1, "tracks": []}}]}


async def test_rip_verification_runs_before_review_and_a_differing_track_holds(env, monkeypatch):
    settings, *_ = env
    settings.auto_import = True
    _spectrum(monkeypatch, "full")
    _rip(monkeypatch, VERIFIED)
    imp = await run(importing.start_import(landed_download(settings, job_id=17)))
    assert imp.state == "imported" and imp.rip_check["summary"].startswith("CTDB verified")
    placed = Path(imp.album_dir)

    mismatch = {**VERIFIED, "mismatched": ["02 Track.flac"], "summary": "CTDB 2/3 tracks match"}
    _rip(monkeypatch, mismatch)
    imp = await run(importing.start_import(landed_download(settings, job_id=18, nested=False)))  # flat, as slskd batches land
    assert imp.state == "review" and importing.review_reason(imp, settings) == "a track differs from the verified rip"
    with session() as s:
        from picker.models import JobEvent
        events = [e.message for e in s.exec(select(JobEvent)).all()]
    assert any("Rip verification: CTDB 2/3" in e for e in events), events

    # Re-download just that track: the file goes, a refetch is requested, and the import waits for it.
    from picker import downloading
    asked = []
    monkeypatch.setattr(downloading, "refetch_files", lambda dl_id, names, imp_id: asked.append(list(names)))
    imp = importing.refetch(imp, names=["02 Track.flac"])
    assert imp.state == "refetching" and asked == [[r"m\OKC\02 Track.flac"]]
    assert not (Path(imp.source_dir) / "02 Track.flac").exists() and (Path(imp.source_dir) / "01 Track.flac").exists()

    settings.rip_verify = False
    import shutil
    shutil.rmtree(placed)  # the picker never overwrites a placed folder
    imp = await run(importing.start_import(landed_download(settings, job_id=19)))
    assert imp.state == "imported" and imp.rip_check is None


def _tag_files(dl: Download, settings, **tags) -> None:
    from picker.downloading import landed_dir

    for path in importing.source_root(landed_dir(dl, settings)).glob("*.flac"):
        f = FLAC(path)
        f.update({k.upper(): v for k, v in tags.items()})
        f.save()


async def test_files_tagged_as_another_edition_wait_for_review_and_can_switch_to_it(env, monkeypatch):
    settings, lidarr, _ = env
    settings.auto_import = True
    _spectrum(monkeypatch, "full")
    dl = landed_download(settings, job_id=14)
    _tag_files(dl, settings, musicbrainz_albumid=OTHER, date="2009")
    imp = await run(importing.start_import(dl))
    assert imp.state == "review" and imp.tag_check["kind"] == "different" and imp.tag_check["tagged_mbid"] == OTHER
    assert "dated 2009, not 1997" in imp.tag_check["detail"] and "a remaster" in imp.tag_check["detail"]
    assert importing.review_reason(imp, settings) == "the files are tagged as a different edition"

    # "Use that edition instead": the checks run again against it and the import goes straight in.
    imp = await run(importing.change_edition(imp, OTHER))
    assert imp.state == "imported" and imp.release_mbid == OTHER and imp.tag_check["kind"] == "same"
    assert lidarr.updated is None  # Lidarr already had OTHER monitored: nothing to change
    with session() as s:
        assert s.get(Download, dl.id).release_mbid == OTHER


async def test_plain_tags_that_fit_do_not_hold(env, monkeypatch):
    settings, *_ = env
    settings.auto_import = True
    _spectrum(monkeypatch, "full")
    dl = landed_download(settings, job_id=15)
    _tag_files(dl, settings, album="OK Computer", date="1997")
    imp = await run(importing.start_import(dl))
    assert imp.state == "imported" and imp.tag_check["kind"] == "agree"


async def test_remaster_in_the_album_tag_holds_and_import_overrides(env, monkeypatch):
    settings, *_ = env
    settings.auto_import = True
    _spectrum(monkeypatch, "full")
    dl = landed_download(settings, job_id=16)
    _tag_files(dl, settings, album="OK Computer (Remastered)")
    imp = await run(importing.start_import(dl))
    assert imp.state == "review" and imp.tag_check["kind"] == "different" and imp.tag_check["tagged_mbid"] is None
    imp = await run(importing.approve(imp))
    assert imp.state == "imported" and imp.release_mbid == REL


def test_names_and_paths(settings):
    assert importing.safe_name("AC/DC") == "AC+DC"
    assert importing.safe_name("...") == "Unknown"
    # Plex sees the library at the same path: no prefix configured means an identity mapping.
    settings.plex_music_path = ""
    album_dir = settings.music_dir / "Radiohead" / "OK Computer (1997)"
    assert importing.plex_path(album_dir, settings) == f"{settings.music_dir.resolve().as_posix()}/Radiohead/OK Computer (1997)"
    settings.plex_music_path = "/data/music"
    assert importing.plex_path(album_dir, settings) == "/data/music/Radiohead/OK Computer (1997)"
    assert importing.within(settings.music_dir / "A", settings.music_dir)
    assert not importing.within(settings.download_dir, settings.music_dir)


async def test_folder_template_extras_come_from_the_chosen_edition(env):
    settings, *_ = env
    settings.album_folder_template = "{Album Title} ({Release Year}){ [Catalog Number]}"
    imp = await run(importing.start_import(landed_download(settings, job_id=13)))
    imp = await run(importing.approve(imp))
    assert Path(imp.album_dir).name == "OK Computer (1997) [CDP 7243 8 55229 2 5]"


# Damaged files: downloaded again automatically once ------------------------------------------

@pytest.fixture
def damaged(env, monkeypatch):
    """flac -t fails for '02 Track.flac' while broken['on'] is set."""
    broken = {"on": True}

    async def tool(args, timeout=0):
        if args[0] == "flac" and broken["on"] and args[-1].endswith("02 Track.flac"):
            return 1, "02 Track.flac: ERROR while decoding data\n                      state = FLAC__STREAM_DECODER_ABORTED"
        if args[0] == "sox":
            Path(args[args.index("-o") + 1]).write_bytes(b"\x89PNG fake")
        return 0, ""

    monkeypatch.setattr(audio, "run_tool", tool)
    return broken


async def test_damaged_file_is_downloaded_again_then_import_continues(env, damaged):
    settings, *_ = env
    dl = landed_download(settings, job_id=20, nested=False)  # batch downloads land flat
    imp = await run(importing.start_import(dl))
    assert imp.state == "refetching" and imp.refetches == 1
    folder = settings.download_dir / "picker" / "20"
    assert sorted(p.name for p in folder.iterdir()) == ["01 Track.flac", "03 Track.flac", "folder.jpg"]  # only the bad one went
    with session() as s:
        dl = s.get(Download, dl.id)
        jobs = s.exec(select(Job).where(Job.kind == "download")).all()
    assert dl.refetch_import_id == imp.id and dl.state == "transferring"
    assert dl.file_states[r"m\OKC\02 Track.flac"]["status"] == "pending"
    assert dl.file_states[r"m\OKC\01 Track.flac"]["status"] == "done"
    assert jobs  # the download job will fetch just that file

    # The file arrives again, intact; the download job hands back to the import.
    make_flac(folder / "02 Track.flac", 2)
    damaged["on"] = False
    importing.resume_after_refetch(imp.id)
    imp = await run(_reload(imp))
    assert imp.state == "review"
    assert all(r["ok"] for r in imp.integrity)


async def test_second_failure_stops_for_the_user(env, damaged):
    settings, *_ = env
    imp = await run(importing.start_import(landed_download(settings, job_id=21, nested=False)))
    make_flac(settings.download_dir / "picker" / "21" / "02 Track.flac", 2)  # arrives, still broken
    importing.resume_after_refetch(imp.id)
    with pytest.raises(Exception, match="integrity"):
        await run(_reload(imp))
    imp = _reload(imp)
    assert imp.state == "failed" and imp.step == "verify" and imp.refetches == 1
    # The manual button still works, as often as you like.
    importing.refetch(imp)
    assert _reload(imp).state == "refetching" and _reload(imp).refetches == 2


async def test_refetch_never_deletes_outside_the_download_folder(env):
    settings, *_ = env
    imp = await run(importing.start_import(landed_download(settings, job_id=22, nested=False)))
    outside = settings.music_dir / "02 Track.flac"
    outside.write_bytes(b"keep me")
    imp.integrity = [{"name": "02 Track.flac", "path": str(outside), "ok": False, "detail": "x"}]
    with pytest.raises(importing.Blocked):
        importing.refetch(imp)
    assert outside.exists()


async def test_refetch_download_failure_fails_the_import(env, damaged, monkeypatch):
    from picker import downloading
    settings, *_ = env
    imp = await run(importing.start_import(landed_download(settings, job_id=23, nested=False)))
    with session() as s:
        dl = s.exec(select(Download).where(Download.refetch_import_id == imp.id)).one()
        job = s.exec(select(Job).where(Job.kind == "download")).first()

    async def gave_up(job, dl):
        raise downloading.JobFailed("you gave up on this folder")

    monkeypatch.setattr(downloading, "_track", gave_up)
    with pytest.raises(downloading.JobFailed):
        await downloading.run_download(job)
    imp = _reload(imp)
    assert imp.state == "failed" and "you gave up" in imp.error
    with session() as s:
        assert s.get(Download, dl.id).refetch_import_id is None


async def test_completed_refetch_resumes_the_import_instead_of_starting_a_new_one(env, damaged, monkeypatch):
    from picker import downloading
    settings, *_ = env
    imp = await run(importing.start_import(landed_download(settings, job_id=24, nested=False)))
    with session() as s:
        job = s.exec(select(Job).where(Job.kind == "download")).first()

    async def completed(job, dl):
        return "completed"

    monkeypatch.setattr(downloading, "_track", completed)
    await downloading.run_download(job)
    with session() as s:
        assert len(s.exec(select(Import)).all()) == 1  # no second import
    assert _reload(imp).state == "verifying"


class FakeAutoscan:
    def __init__(self, fail=False):
        self.scanned, self.fail = [], fail

    async def scan(self, folder):
        if self.fail:
            from picker.clients.http import ServiceError
            raise ServiceError("autoscan", "HTTP 401: check the autoscan username and password", 401)
        self.scanned.append(folder)


@pytest.mark.parametrize("fail", [False, True])
async def test_imports_ask_autoscan_for_the_album_folder(env, monkeypatch, fail):
    settings, lidarr, plex = env
    settings.autoscan_url = "http://autoscan:3030"
    autoscan = FakeAutoscan(fail)
    services = FakeServices(lidarr, plex)
    services.autoscan = autoscan
    monkeypatch.setattr(importing, "services", lambda: services)
    imp = await run(importing.start_import(landed_download(settings, job_id=30)))
    imp = await run(importing.approve(imp))
    assert imp.state == "imported"  # a scanner failing never undoes the import
    target = settings.music_dir / "Radiohead" / "OK Computer (1997)"
    if fail:
        assert autoscan.scanned == [] and any("autoscan scan request failed" in n for n in imp.notes)
    else:
        assert autoscan.scanned == [target.as_posix()]
    assert len(plex.scanned) == 1  # Plex, configured too, still gets its direct scan


async def test_without_scanners_the_import_says_so(env):
    settings, *_ = env
    settings.plex_token = ""
    imp = await run(importing.start_import(landed_download(settings, job_id=31)))
    imp = await run(importing.approve(imp))
    assert imp.state == "imported"
    assert any("Neither autoscan nor Plex is configured" in n for n in imp.notes)


# Transcode detection in the pipeline -----------------------------------------------------------

async def test_possible_transcode_is_reported_but_never_blocks(env, monkeypatch):
    from picker import spectrum
    settings, *_ = env

    async def fake(path, rate, length, cutoff_db=30.0):
        if path.endswith("01 Track.flac"):
            return spectrum.Verdict("lossy", 16000, 62.0, "spectrum ends at 16.0 kHz (62 dB cliff), consistent with a ~128 kbps lossy source")
        return spectrum.Verdict("full", None, None, "energy continues to the top of the spectrum")

    monkeypatch.setattr(spectrum, "analyse_file", fake)
    imp = await run(importing.start_import(landed_download(settings, job_id=40)))
    assert imp.state == "review"  # a warning, not a reject
    assert imp.transcode["album"]["kind"] == "lossy" and imp.transcode["album"]["cutoff_hz"] == 16000
    assert len(imp.transcode["files"]) == 3  # every track, not just the spectrogram ones
    assert imp.transcode["album"]["detail"].endswith("(1 of 3 tracks)")


async def test_failed_analysis_is_unknown_and_the_import_continues(env, monkeypatch):
    from picker import spectrum
    settings, *_ = env

    async def broken(path, rate, length, cutoff_db=30.0):
        raise RuntimeError("ffmpeg is not installed")

    monkeypatch.setattr(spectrum, "analyse_file", broken)
    imp = await run(importing.start_import(landed_download(settings, job_id=41)))
    assert imp.state == "review"
    assert imp.transcode["album"]["kind"] == "unknown"
    assert "ffmpeg is not installed" in imp.transcode["files"][0]["detail"]


async def test_transcode_check_can_be_turned_off(env):
    settings, *_ = env
    settings.transcode_check = False
    imp = await run(importing.start_import(landed_download(settings, job_id=42)))
    assert imp.state == "review" and imp.transcode is None


def test_overlays_ignore_tags_the_picker_overwrites(settings):
    import yaml
    beets.ensure_config(settings)
    pinned = yaml.safe_load((settings.beets_dir / "pinned.yaml").read_text())
    force = yaml.safe_load((settings.beets_dir / "force.yaml").read_text())
    for overlay in (pinned, force):
        weights = overlay["match"]["distance_weights"]
        assert weights["album_id"] == weights["track_id"] == weights["country"] == weights["label"] == 0
        assert "track_length" not in weights and "track_title" not in weights  # still judged
    assert force["match"]["strong_rec_thresh"] == 1.01
    assert "strong_rec_thresh" not in pinned["match"]  # the base config's threshold applies
