"""The import pipeline: a finished download becomes a tagged album folder in the library.

Steps, each recorded on the Import row so a restart resumes where it stopped:

    verify   file count, flac -t, mutagen; spectrograms, spectrum check, rip verification
    tags     the uploader's tags against the chosen edition (a different edition holds for review)
    review   wait for Import / Change edition / Reject (skipped when auto_import is on and every check passed)
    artist   make sure Lidarr has the artist; read its folder
    place    move the download to <artist folder>/<album folder>; never overwrite
    beets    tag in place, pinned to the chosen release
    lidarr   select the same release on the Lidarr album; rescan the artist folder
    plex     partial scan of the album folder
    done

Nothing under the music library is ever deleted or overwritten. Reject deletes only
inside the download directory.
"""

from __future__ import annotations

import errno
import os
import posixpath
import shutil
from datetime import datetime
from pathlib import Path

from sqlmodel import select

from . import albums, audio, beets, library, preferences, queueing, ripverify, tagcheck
from .clients import services
from .clients.http import ServiceError
from .config import Settings, get_settings
from .db import session
from .jobs import JobFailed, handler, log_event, worker
from .models import Album, Candidate, Download, Import, Job, utcnow
from .naming import FolderValues, album_folder, clean_component, clean_value



class Blocked(Exception):
    """The pipeline needs the user (target folder exists, no edition chosen, ...)."""


class Refetching(Exception):
    """Files that failed the integrity check are being downloaded again; the import waits."""


AUTO_REFETCHES = 1  # automatic re-downloads of damaged files before asking the user


def _save(imp: Import, **fields) -> Import:
    for name, value in fields.items():
        setattr(imp, name, value)
    with session() as s:
        s.merge(imp)
        s.commit()
    return imp


def _note(imp: Import, message: str) -> None:
    _save(imp, notes=[*imp.notes, message])
    log_event(imp.job_id, imp.rg_mbid, message)


def safe_name(name: str) -> str:
    """One folder-name component, cleaned the way Lidarr cleans token values."""
    return clean_component(clean_value(name)) or "Unknown"


async def folder_values(imp: Import, album: Album, settings: Settings) -> FolderValues:
    """Values for the album folder template.

    Title and year come from Lidarr's album when Lidarr has it, so the folder matches the
    one Lidarr would build; otherwise from MusicBrainz. Edition extras come from the
    chosen release.
    """
    title, date = album.title, album.first_release_date
    if settings.lidarr_api_key:
        try:
            lidarr_album = await services().lidarr.album_by_foreign_id(imp.rg_mbid)
        except ServiceError:
            lidarr_album = None
        if lidarr_album:
            title, date = lidarr_album.get("title") or title, lidarr_album.get("releaseDate") or date
    edition = None
    try:
        edition = next((e for e in await albums.album_editions(imp.rg_mbid) if e.id == imp.release_mbid), None)
    except ServiceError:
        pass
    year = (date or "")[:4]
    return FolderValues(
        album_title=title,
        release_year=year if year.isdigit() else None,
        artist_name=album.artist,
        album_type=album.primary_type or "",
        album_mbid=album.rg_mbid,
        artist_mbid=album.artist_mbid or "",
        catalog_number=(edition.catalog_numbers[:1] or [""])[0] if edition else "",
        label=(edition.labels[:1] or [""])[0] if edition else "",
        country=edition.country if edition else "",
        media=edition.format_summary if edition else "",
        edition_date=edition.date if edition else "",
    )


def within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def source_root(landed: Path) -> Path:
    """slskd may nest the files in the remote folder's name: unwrap a lone subfolder."""
    entries = list(landed.iterdir()) if landed.is_dir() else []
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return landed


# Starting and controlling an import ------------------------------------------------------

def start_import(dl: Download) -> Import:
    from .downloading import landed_dir  # imported here: downloading starts imports

    imp = Import(
        download_id=dl.id, rg_mbid=dl.rg_mbid, release_mbid=dl.release_mbid, username=dl.username,
        source_dir=str(landed_dir(dl, get_settings())), upgrade_of=dl.upgrade_of,
    )
    with session() as s:
        s.add(imp)
        s.commit()
        s.refresh(imp)
    return enqueue(imp)


def enqueue(imp: Import) -> Import:
    job = worker.enqueue("import", {"import_id": imp.id}, rg_mbid=imp.rg_mbid)
    return _save(imp, job_id=job.id)


def approve(imp: Import) -> Import:
    if not imp.release_mbid:
        raise Blocked("choose the edition to tag it as first")
    return enqueue(_save(imp, step="artist", state="importing", error=None))


def force_apply(imp: Import) -> Import:
    return enqueue(_save(imp, step="beets", state="importing", force=True, error=None))


def retry(imp: Import) -> Import:
    return enqueue(_save(imp, state="importing", error=None))


def reject(imp: Import) -> Import:
    """Delete the downloaded folder (download directory only) and close the import."""
    settings = get_settings()
    src = Path(imp.source_dir or "")
    picker_root = settings.download_dir / "picker"
    if imp.album_dir:
        raise Blocked("this album is already in the library; the picker never deletes from it")
    if src.is_dir():
        if not within(src, picker_root) or src.resolve() == picker_root.resolve():
            raise Blocked(f"refusing to delete {src}: not inside {picker_root}")
        shutil.rmtree(src)
        landed = _landed_root(src, picker_root)
        if landed and landed.is_dir() and not any(landed.iterdir()):
            landed.rmdir()
    return _save(imp, state="rejected", finished_at=utcnow())


def _landed_root(path: Path, picker_root: Path) -> Path | None:
    """picker/<job-id> for any path inside it."""
    try:
        rel = path.resolve().relative_to(picker_root.resolve())
    except ValueError:
        return None
    return picker_root / rel.parts[0] if rel.parts else None


def refetch(imp: Import, names: list[str] | None = None) -> Import:
    """Delete the files that failed the integrity check (or the named files, e.g. the ones
    rip verification called out) and download just those again.

    The damaged copies must go first: slskd renames a download whose name already exists
    (name_<ticks>.flac). Only files inside the download directory's picker/ folder are deleted.
    """
    from . import downloading  # imported here: downloading imports this module

    settings = get_settings()
    if names is not None:
        bad = [r for r in imp.integrity or [] if r.get("name") in names and r.get("path")]
        if not bad:
            raise Blocked("none of those files is in the integrity list")
    else:
        bad = [r for r in imp.integrity or [] if not r.get("ok") and r.get("path")]
    if not bad:
        raise Blocked("no file failed the integrity check")
    if imp.album_dir:
        raise Blocked("the album is already in the library")
    with session() as s:
        dl = s.get(Download, imp.download_id) if imp.download_id else None
    if dl is None or dl.legacy_enqueue:
        raise Blocked("this download can't be repeated file by file (no slskd batch destination)")
    picker_root = settings.download_dir / "picker"
    by_local = {downloading.local_path(dl, f, settings).resolve(): f for f in dl.files}
    targets = []
    for r in bad:
        path = Path(r["path"])
        remote = by_local.get(path.resolve())
        if remote is None:
            raise Blocked(f"can't tell which requested file {path.name} is")
        if not within(path, picker_root):
            raise Blocked(f"refusing to delete {path}: not inside {picker_root}")
        targets.append((path, remote["filename"]))
    for path, _ in targets:
        path.unlink(missing_ok=True)
    downloading.refetch_files(dl.id, [remote for _, remote in targets], imp.id)
    listed = ", ".join(path.name for path, _ in targets)
    _save(imp, state="refetching", step="verify", refetches=(imp.refetches or 0) + 1, error=None)
    why = "differed from the verified rip" if names is not None else f"failed the integrity check ({bad[0].get('detail')})"
    log_event(imp.job_id, imp.rg_mbid, f"{listed} {why}; downloading {'it' if len(targets) == 1 else 'them'} again from {dl.username}")
    return imp


def resume_after_refetch(import_id: int) -> None:
    with session() as s:
        imp = s.get(Import, import_id)
    if imp and imp.state == "refetching":
        log_event(imp.job_id, imp.rg_mbid, "Downloaded the damaged files again; checking the folder")
        enqueue(_save(imp, state="verifying", step="verify", error=None))


def refetch_failed(import_id: int, message: str) -> None:
    with session() as s:
        imp = s.get(Import, import_id)
    if imp and imp.state == "refetching":
        _save(imp, state="failed", error=f"downloading the damaged files again failed: {message}", finished_at=utcnow())
        log_event(imp.job_id, imp.rg_mbid, imp.error)


def change_edition(imp: Import, release_mbid: str) -> Import:
    """Pick another edition. Under review, the checks run again against it, so an import
    that now passes every check (auto_import on) goes straight in."""
    if imp.state not in ("review", "skipped", "blocked", "failed"):
        raise Blocked(f"cannot change the edition while the import is {imp.state}")
    with session() as s:
        if imp.download_id and (dl := s.get(Download, imp.download_id)):
            dl.release_mbid = release_mbid
            s.add(dl)
            s.commit()
    imp = _save(imp, release_mbid=release_mbid, force=False)
    if imp.state == "review":
        return enqueue(_save(imp, step="tags", state="verifying", error=None))
    return imp


# The job ---------------------------------------------------------------------------------

@handler("import")
async def run_import(job: Job) -> None:
    with session() as s:
        imp = s.get(Import, job.payload["import_id"])
    imp.job_id = job.id
    try:
        await _pipeline(imp)
    except Blocked as exc:
        _save(imp, state="blocked", error=str(exc))
        log_event(job.id, imp.rg_mbid, f"Import waiting: {exc}")
    except Refetching:
        pass  # the download job resumes this import when the files arrive
    except Exception as exc:
        _save(imp, state="failed", error=str(exc), finished_at=utcnow())
        raise


def match_tier(imp: Import) -> str | None:
    """The match tier of the folder this import came from (None for Lidarr's own imports)."""
    if not imp.download_id:
        return None
    with session() as s:
        dl = s.get(Download, imp.download_id)
        candidate = s.get(Candidate, dl.candidate_id) if dl else None
    return ((candidate.match or {}).get("tier") if candidate else None)


def new_items_policy(imp: Import, settings: Settings) -> str:
    """Artists a run adds to Lidarr get monitor-new-items none: a playlist can touch dozens of
    artists the user wanted one song from, and the normal default would have Lidarr grab every
    future release of theirs. Hand-picked imports keep the setting."""
    item = queueing.item_for_download(imp.download_id) if imp.download_id else None
    return "none" if item is not None and item.source == "playlist" else settings.lidarr_monitor_new_items


def review_reason(imp: Import, settings: Settings) -> str | None:
    """Why this import waits on the Review page instead of importing by itself (None: import now).

    With auto_import on, only an import that passed every check goes straight in: the
    integrity check (already required to get here), a spectrum check that found no lossy
    low-pass or upsampling and actually ran, and a chosen edition.
    """
    if not settings.auto_import:
        return "auto_import is off"
    if not imp.release_mbid:
        return "no edition chosen"
    if settings.transcode_check:
        verdict = ((imp.transcode or {}).get("album") or {})
        kind = verdict.get("kind")
        if kind in ("lossy", "upsampled"):
            return "the spectrum check suggests a transcode" if kind == "lossy" else "the spectrum check suggests upsampling"
        failed = [f for f in (imp.transcode or {}).get("files", []) if str(f.get("detail", "")).startswith("could not analyse")]
        if kind is None or failed:
            return "the spectrum check could not run"
    kind = (imp.tag_check or {}).get("kind")
    if kind == "different" and match_tier(imp) == "Exact":
        kind = None  # a disc ID, catalog number or barcode agreed with the chosen edition: physical evidence beats tags
    if kind == "different":
        return "the files are tagged as a different edition"
    if kind == "mixed":
        return "the files are tagged as several different releases"
    if settings.rip_verify and (imp.rip_check or {}).get("mismatched"):
        return "a track differs from the verified rip"
    if imp.upgrade_of:
        if not settings.auto_upgrade:
            return "it replaces the copy you have (auto_upgrade is off)"
        if not (imp.upgrade_check or {}).get("better"):
            return "the new copy is not clearly better than the one you have"
    return None


async def _pipeline(imp: Import) -> None:
    settings = get_settings()
    if imp.step == "verify":
        await _verify(imp, settings)
        _save(imp, step="tags")
    if imp.step == "tags":
        await _check_tags(imp)
        if imp.upgrade_of:
            _save(imp, upgrade_check=await compare_upgrade(imp, settings))
        hold = review_reason(imp, settings)
        if hold:
            _save(imp, step="review", state="review")
            log_event(imp.job_id, imp.rg_mbid, f"Downloaded and verified; waiting for review ({hold})")
            return
        log_event(imp.job_id, imp.rg_mbid, "Passed every check; importing")
        _save(imp, step="artist", state="importing")
    if imp.step == "review":
        return

    with session() as s:
        album = s.get(Album, imp.rg_mbid)

    if imp.step in ("artist", "place"):
        artist_dir = await _artist_dir(imp, album, settings)
        name = album_folder(settings.album_folder_template, await folder_values(imp, album, settings))
        _place(imp, artist_dir / name)
    if imp.step == "beets":
        if not await _beets(imp, settings):
            return
    if imp.step == "lidarr":
        await _sync_lidarr(imp, settings)
        _save(imp, step="plex")
    if imp.step == "plex":
        await _scan_plex(imp, settings)
        _save(imp, step="done", state="imported", finished_at=utcnow())
        log_event(imp.job_id, imp.rg_mbid, f"Imported into {imp.album_dir}")
        if imp.album_dir:
            library.queue_index(imp.album_dir)  # the index sees the new folder before the nightly pass


async def _verify(imp: Import, settings: Settings) -> None:
    root = Path(imp.source_dir or "")
    if not root.is_dir():
        raise JobFailed(f"cannot see the download at {root}; check the path mapping")
    root = source_root(root)
    with session() as s:
        dl = s.get(Download, imp.download_id) if imp.download_id else None
    expected = len(dl.files) if dl else 0
    ok, results, reason = await audio.verify(root, expected)
    _save(imp, source_dir=str(root), integrity=results)
    bad = [r for r in results if not r["ok"]]
    if not ok and bad and (imp.refetches or 0) < AUTO_REFETCHES:
        # Soulseek has no file hashes, so a transfer damaged on the way (seen in practice: a
        # burst of garbled bytes mid-file, "Completed, Succeeded" in slskd) is only caught
        # here. A second download is almost always clean: try once before asking.
        try:
            refetch(imp)
        except Blocked as exc:
            _note(imp, f"Could not download the damaged files again automatically: {exc}")
        else:
            raise Refetching()
    if not ok:
        raise JobFailed(f"verification failed: {reason}")
    picks = audio.spectrogram_picks(results, audio.audio_files(root))
    names, error = await audio.spectrograms(picks, settings.spectrogram_dir / str(imp.id))
    _save(imp, spectrograms=names)
    if error:
        _note(imp, f"No spectrogram: {error}")
    if settings.transcode_check:
        # A warning for the Review page, never a reject: some masterings are band-limited.
        transcode = await audio.transcode_check(results, audio.audio_files(root), settings.cutoff_db)
        _save(imp, transcode=transcode)
        if transcode["album"]["kind"] in ("lossy", "upsampled"):
            log_event(imp.job_id, imp.rg_mbid, f"Possible transcode: {transcode['album']['detail']}")
    if settings.rip_verify:
        # A signal, not a gate, except for a track that fails while its siblings verify.
        rip = await ripverify.verify_folder(root, audio.audio_files(root), settings)
        _save(imp, rip_check=rip)
        if rip.get("applicable"):
            log_event(imp.job_id, imp.rg_mbid, f"Rip verification: {rip['summary']}")


async def _check_tags(imp: Import) -> None:
    """What the uploader's tags say the files are, against the edition chosen at download.

    beets is told to ignore those tags (see beets.py), so this is where a folder tagged
    as another edition gets noticed. Only a different edition holds the import.
    """
    try:
        editions = await albums.album_editions(imp.rg_mbid)
    except ServiceError as exc:
        check = tagcheck.TagCheck("unreadable", f"could not load the album's editions: {exc.message}", tags={})
    else:
        chosen = next((e for e in editions if e.id == imp.release_mbid), None)
        check = tagcheck.check_folder(audio.audio_files(Path(imp.source_dir or "")), chosen, editions)
    _save(imp, tag_check=check.to_dict())
    if check.kind in ("different", "mixed"):
        log_event(imp.job_id, imp.rg_mbid, f"Tag check: {check.detail}")


async def _artist_dir(imp: Import, album: Album, settings: Settings) -> Path:
    """The artist's folder from Lidarr, adding the artist if needed."""
    if not settings.lidarr_api_key:
        _note(imp, "Lidarr is not configured; placing under the music directory by artist name")
        return settings.music_dir / safe_name(album.artist)
    if not album.artist_mbid:
        raise JobFailed("the album has no MusicBrainz artist ID, so it cannot be added to Lidarr")
    lidarr = services().lidarr
    artist = await lidarr.artist_by_mbid(album.artist_mbid)
    if artist is None:
        lookup = await lidarr.lookup_artist(album.artist_mbid)
        if lookup is None:
            raise JobFailed(f"Lidarr cannot find artist {album.artist_mbid}")
        artist = await lidarr.add_artist(
            lookup,
            root_folder=settings.lidarr_root_folder,
            quality_profile_id=settings.lidarr_quality_profile_id,
            metadata_profile_id=settings.lidarr_metadata_profile_id,
            monitor=settings.lidarr_monitor,
            monitor_new_items=new_items_policy(imp, settings),
        )
        _note(imp, f"Added {album.artist} to Lidarr (monitor {settings.lidarr_monitor}, new items {new_items_policy(imp, settings)})")
        # Wait for the refresh so the album exists in Lidarr before the edition is selected.
        cmd = await lidarr.command("RefreshArtist", artistIds=[artist["id"]])
        await lidarr.wait_for_command(cmd["id"])
    path = Path(artist["path"])
    if not within(path, settings.music_dir):
        raise JobFailed(f"Lidarr's folder for {album.artist} is {path}, outside the music library at {settings.music_dir}")
    return path


def _move(src: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(src, target)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        shutil.move(str(src), str(target))  # different filesystems: copy, then remove the source


def folder_bytes(folder: Path) -> int:
    return sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())


def free_space_check(src: Path, dest_root: Path) -> str | None:
    """Why the move might not fit (None when it fits): a rename needs nothing, a copy across
    filesystems needs the folder's size, and Unraid user shares can turn a rename into a copy."""
    probe = dest_root
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        return None
    need = folder_bytes(src) + 100_000_000
    if free < need:
        return f"not enough free space under {dest_root}: {free / 1e9:.1f} GB free, {need / 1e9:.1f} GB needed"
    return None


def replaced_target(old: Path, settings: Settings, marker: str = "") -> Path:
    stamp = datetime.now().strftime("%Y-%m-%d %H%M")
    return settings.replaced_dir / old.parent.name / f"{old.name} ({marker + ' ' if marker else ''}{stamp})"


def _retire_old_copy(imp: Import, settings: Settings, src: Path) -> None:
    """An upgrade: move the copy being replaced out of the library to the replaced folder.

    Never deleted by the picker. The Settings page shows the folder's size and offers the only
    deletion the picker performs, by an explicit click.
    """
    old = Path(imp.upgrade_of)
    if imp.replaced_dir:
        return  # already moved (a retry after a block)
    if not old.is_dir():
        _note(imp, f"The copy to replace, {old}, was already gone")
        return
    if not within(old, settings.music_dir) or old.resolve() == settings.music_dir.resolve():
        raise Blocked(f"refusing to replace {old}: it is not an album folder inside {settings.music_dir}")
    short = free_space_check(src, settings.music_dir)
    if short:
        raise Blocked(short)
    parked = replaced_target(old, settings)
    _move(old, parked)
    library.forget_folder(old)
    _save(imp, replaced_dir=str(parked))
    log_event(imp.job_id, imp.rg_mbid, f"Moved the old copy {old} to {parked}")


def _place(imp: Import, target: Path) -> None:
    src = Path(imp.source_dir or "")
    if imp.album_dir and Path(imp.album_dir).is_dir() and not src.exists():
        _save(imp, step="beets")  # placed before a restart
        return
    if not src.is_dir():
        raise JobFailed(f"the downloaded folder {src} is gone")
    if imp.upgrade_of:
        _retire_old_copy(imp, get_settings(), src)
    if target.exists():
        raise Blocked(f"{target} already exists; the picker never overwrites. Move or rename it, then Retry")
    _move(src, target)
    _save(imp, album_dir=str(target), step="beets")
    log_event(imp.job_id, imp.rg_mbid, f"Placed at {target}")


async def _beets(imp: Import, settings: Settings) -> bool:
    album_dir = Path(imp.album_dir or "")
    if imp.source == "lidarr" and not settings.beets_pretend:
        # A re-imported or upgraded album may already be in the beets library: start clean.
        await beets.forget_album(settings, imp.release_mbid, album_dir)
    if imp.upgrade_of and imp.replaced_dir and not settings.beets_pretend:
        await beets.forget_path(settings, Path(imp.upgrade_of))  # the replaced copy leaves the beets library
    result = await beets.import_album(settings, album_dir, imp.release_mbid, force=imp.force,
                                      pretend=settings.beets_pretend)
    _save(imp, beets_output=result.output)
    if result.imported:
        _save(imp, step="lidarr", tags=audio.tag_snapshot(album_dir))
        log_event(imp.job_id, imp.rg_mbid, "beets tagged the album" + (" (forced)" if imp.force else ""))
        return True
    if result.skipped:
        # beets says only "Skipping."; ask its matcher what cost the points so the Review page can show it.
        explanation = await beets.explain_skip(settings, album_dir, imp.release_mbid)
        _save(imp, state="skipped", beets_match=explanation)
        rated = f" ({explanation['similarity'] * 100:.1f}%, needs {explanation['threshold'] * 100:.0f}%)" if "similarity" in explanation else ""
        log_event(imp.job_id, imp.rg_mbid, f"beets skipped the album: the match was not strong enough{rated}. "
                                           "The Review page shows what differed; Force apply tags it anyway")
        return False
    raise JobFailed(f"beets exited {result.exit_code}: {(result.output.splitlines() or ['no output'])[-1]}")


async def _sync_lidarr(imp: Import, settings: Settings) -> None:
    if not settings.lidarr_api_key or imp.source == "lidarr":
        return  # a retag of Lidarr's own import: its edition choice stands
    lidarr = services().lidarr
    album = await lidarr.album_by_foreign_id(imp.rg_mbid)
    if album is None:
        _note(imp, "Lidarr has no album for this release group; its metadata profile may exclude this album type")
        return
    releases = album.get("releases") or []
    if not any(r.get("foreignReleaseId") == imp.release_mbid for r in releases):
        _note(imp, "Lidarr does not list the chosen edition for this album: the artist's metadata profile filters it "
                   "out (check its allowed release statuses and formats). The files are tagged; Lidarr will not show this edition.")
    else:
        changed = False
        for r in releases:
            # Exactly one may be monitored; Lidarr misbehaves with two.
            want = r.get("foreignReleaseId") == imp.release_mbid
            changed |= r.get("monitored") != want
            r["monitored"] = want
        if changed:
            await lidarr.update_album(album)
    artist_dir = str(Path(imp.album_dir).parent) if imp.album_dir else None
    cmd = await lidarr.command("RescanFolders", folders=[artist_dir], filter="matched")
    await lidarr.wait_for_command(cmd["id"])
    stats = (await lidarr.album(album["id"])).get("statistics") or {}
    have, want = stats.get("trackFileCount"), stats.get("trackCount")
    if have != want:
        _note(imp, f"Lidarr sees {have} of {want} track files for this album after the rescan")


def plex_path(album_dir: Path, settings: Settings) -> str:
    rel = album_dir.resolve().relative_to(settings.music_dir.resolve()).as_posix()
    root = settings.plex_music_path or settings.music_dir.as_posix()
    return posixpath.join(root, rel)


async def _scan_plex(imp: Import, settings: Settings) -> None:
    """Tell the library scanners about the new or retagged folder.

    autoscan (when configured) gets the exact folder through its manual trigger: an
    inotify watcher can miss a folder moved into place in one rename. Plex (when
    configured) gets a partial scan directly. Neither failing undoes the import.
    """
    album_dir = Path(imp.album_dir)
    asked = False
    if settings.autoscan_url:
        asked = True
        try:
            await services().autoscan.scan(album_dir.as_posix())
            log_event(imp.job_id, imp.rg_mbid, f"Asked autoscan to scan {album_dir}")
        except ServiceError as exc:
            _note(imp, f"autoscan scan request failed: {exc.message}")
    if settings.plex_token and settings.plex_section_id:
        asked = True
        try:
            await services().plex.scan_path(plex_path(album_dir, settings))
        except ServiceError as exc:
            _note(imp, f"Plex scan failed: {exc.message}")
    if not asked:
        _note(imp, "Neither autoscan nor Plex is configured; the library scan is left to Plex's own watcher")


# Upgrades (spec 7.2) --------------------------------------------------------------------------

def _most_common(values: list) -> object | None:
    values = [v for v in values if v]
    return max(set(values), key=values.count) if values else None


async def compare_upgrade(imp: Import, settings: Settings) -> dict:
    """Old copy (from the library index) against the new one (from the download), for the Review
    page and for auto_upgrade. `better` means strictly better on at least one count and worse on none."""
    old_files = library.folder_files(imp.upgrade_of) if imp.upgrade_of else []
    old = {
        "path": imp.upgrade_of, "tracks": len(old_files),
        "depth": max((f.bit_depth or 0 for f in old_files), default=0) or None,
        "rate": max((f.sample_rate or 0 for f in old_files), default=0) or None,
        "size": sum(f.size for f in old_files), "release": _most_common([f.release_mbid for f in old_files]),
    }
    with session() as s:
        dl = s.get(Download, imp.download_id) if imp.download_id else None
        cand = s.get(Candidate, dl.candidate_id) if dl else None
        album = s.get(Album, imp.rg_mbid)
    parsed = (cand.parsed or {}) if cand else {}
    new = {
        "tracks": cand.audio_count if cand else len(audio.audio_files(Path(imp.source_dir or ""))),
        "depth": (cand.bit_depth or parsed.get("bit_depth")) if cand else None,
        "rate": (cand.sample_rate or parsed.get("sample_rate")) if cand else None,
        "size": dl.bytes_total if dl else None, "release": imp.release_mbid,
    }
    try:
        editions = {e.id: e for e in await albums.album_editions(imp.rg_mbid)}
    except ServiceError:
        editions = {}
    for side in (old, new):
        edition = editions.get(side["release"] or "")
        side["pref"] = preferences.score_edition(edition, settings).score if edition else None
        side["edition"] = f"{edition.date or ''} {edition.country or ''} {edition.format_summary or ''}".strip() if edition else None
    expected = album.min_track_count if album and album.min_track_count else None
    old["complete"] = bool(expected and old["tracks"] >= expected)
    new["complete"] = bool(expected and new["tracks"] >= expected)
    reasons, worse = [], []
    if old["pref"] is not None and new["pref"] is not None:
        if new["pref"] > old["pref"]:
            reasons.append(f"preferred edition ({new['pref']:+d} vs {old['pref']:+d})")
        elif new["pref"] < old["pref"]:
            worse.append(f"less preferred edition ({new['pref']:+d} vs {old['pref']:+d})")
    if new["complete"] and not old["complete"]:
        reasons.append(f"complete ({new['tracks']} tracks vs {old['tracks']})")
    elif new["tracks"] < old["tracks"]:
        worse.append(f"fewer tracks ({new['tracks']} vs {old['tracks']})")
    if old["depth"] and new["depth"]:
        if new["depth"] > old["depth"]:
            reasons.append(f"{new['depth']}-bit vs {old['depth']}-bit")
        elif new["depth"] < old["depth"]:
            worse.append(f"{new['depth']}-bit vs {old['depth']}-bit")
    if old["pref"] is None and old["release"] is None:
        worse.append("the old copy's edition is unknown (no MusicBrainz tags)")
    return {"old": old, "new": new, "better": bool(reasons) and not worse, "reasons": reasons, "worse": worse}


def request_undo(imp: Import) -> None:
    """Put the replaced copy back; the new one goes to the replaced folder instead."""
    if not imp.upgrade_of or not imp.replaced_dir or imp.upgrade_undone:
        raise Blocked("there is no replaced copy to put back")
    if not Path(imp.replaced_dir).is_dir():
        raise Blocked(f"the replaced copy is no longer at {imp.replaced_dir}")
    if not imp.album_dir or not Path(imp.album_dir).is_dir():
        raise Blocked("the new copy is not in the library (yet)")
    if Path(imp.upgrade_of).exists() and Path(imp.upgrade_of).resolve() != Path(imp.album_dir).resolve():
        raise Blocked(f"{imp.upgrade_of} exists again; move it away first")
    worker.enqueue("undo_upgrade", {"import_id": imp.id}, rg_mbid=imp.rg_mbid)


@handler("undo_upgrade")
async def run_undo(job: Job) -> None:
    settings = get_settings()
    with session() as s:
        imp = s.get(Import, job.payload["import_id"])
    new_dir, old_dir, parked = Path(imp.album_dir), Path(imp.upgrade_of), Path(imp.replaced_dir)
    if not parked.is_dir():
        raise JobFailed(f"the replaced copy is no longer at {parked}")
    new_parked = replaced_target(new_dir, settings, "undone")
    _move(new_dir, new_parked)
    _move(parked, old_dir)
    log_event(job.id, imp.rg_mbid, f"Put {old_dir} back; the new copy is at {new_parked}")
    library.forget_folder(new_dir)
    library.scan(old_dir, settings)
    if not settings.beets_pretend:
        await beets.forget_path(settings, new_dir)
        old_release = _most_common([f.release_mbid for f in library.folder_files(old_dir)])
        if old_release:
            result = await beets.import_album(settings, old_dir, old_release)
            if not result.imported:
                _note(imp, "beets did not take the old copy back into its library; its files are untouched")
        else:
            _note(imp, "the old copy carries no MusicBrainz release ID, so beets was not told about it")
    _save(imp, upgrade_undone=True, state="undone", album_dir=str(old_dir), replaced_dir=str(new_parked),
          finished_at=utcnow())
    _note(imp, f"Upgrade undone: {old_dir} is back, the new copy is parked at {new_parked}")
    if settings.lidarr_api_key:
        try:
            cmd = await services().lidarr.command("RescanFolders", folders=[str(old_dir.parent)], filter="matched")
            await services().lidarr.wait_for_command(cmd["id"])
        except ServiceError as exc:
            _note(imp, f"Lidarr rescan after the undo failed: {exc.message}")
    await _scan_plex(imp, settings)
    item = queueing.item_for_download(imp.download_id) if imp.download_id else None
    if item is not None and item.playlist_id:
        from . import plexsync  # imported here: plexsync imports playlists, which this module must not

        plexsync.queue_sync(item.playlist_id)


def replaced_summary(settings: Settings) -> dict:
    root = settings.replaced_dir
    folders = [p for p in root.glob("*/*") if p.is_dir()] if root.is_dir() else []
    size = sum(folder_bytes(f) for f in folders)
    oldest = min((f.stat().st_mtime for f in folders), default=None)
    return {"path": str(root), "folders": len(folders), "bytes": size,
            "oldest": datetime.fromtimestamp(oldest).date().isoformat() if oldest else None}


def delete_replaced(settings: Settings, older_than_days: int) -> int:
    """The one deletion the picker performs, and only by an explicit click: replaced copies older than N days."""
    root = settings.replaced_dir
    if not root.is_dir() or older_than_days < 0:
        return 0
    cutoff = datetime.now().timestamp() - older_than_days * 86400
    removed = 0
    for folder in [p for p in root.glob("*/*") if p.is_dir()]:
        if not within(folder, root) or folder.stat().st_mtime > cutoff:
            continue
        shutil.rmtree(folder)
        removed += 1
        parent = folder.parent
        if parent != root and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
    return removed


WAITING_STATES = ("review", "skipped", "blocked")


def waiting_imports() -> list[Import]:
    """Every import that needs the user: review, beets skipped, blocked, or a failed check."""
    with session() as s:
        rows = s.exec(select(Import).where(Import.state.in_([*WAITING_STATES, "failed"])).order_by(Import.id.desc())).all()
    return [i for i in rows if i.state != "failed" or i.step == "verify"]


def imports_for(download_ids: list[int]) -> dict[int, Import]:
    with session() as s:
        rows = s.exec(select(Import).where(Import.download_id.in_(download_ids)).order_by(Import.id)).all()
    return {i.download_id: i for i in rows}
