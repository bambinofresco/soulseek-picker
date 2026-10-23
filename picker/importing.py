"""The import pipeline: a finished download becomes a tagged album folder in the library.

Steps, each recorded on the Import row so a restart resumes where it stopped:

    verify   file count, flac -t, mutagen; spectrograms for review
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
from pathlib import Path

from sqlmodel import select

from . import albums, audio, beets
from .clients import services
from .clients.http import ServiceError
from .config import Settings, get_settings
from .db import session
from .jobs import JobFailed, handler, log_event, worker
from .models import Album, Download, Import, Job, utcnow
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
        source_dir=str(landed_dir(dl, get_settings())),
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


def refetch(imp: Import) -> Import:
    """Delete the files that failed the integrity check and download just those again.

    The damaged copies must go first: slskd renames a download whose name already exists
    (name_<ticks>.flac). Only files inside the download directory's picker/ folder are deleted.
    """
    from . import downloading  # imported here: downloading imports this module

    settings = get_settings()
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
    names = ", ".join(path.name for path, _ in targets)
    _save(imp, state="refetching", step="verify", refetches=(imp.refetches or 0) + 1, error=None)
    log_event(imp.job_id, imp.rg_mbid, f"{names} failed the integrity check ({bad[0].get('detail')}); "
                                       f"downloading {'it' if len(targets) == 1 else 'them'} again from {dl.username}")
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
    if imp.state not in ("review", "skipped", "blocked", "failed"):
        raise Blocked(f"cannot change the edition while the import is {imp.state}")
    with session() as s:
        if imp.download_id and (dl := s.get(Download, imp.download_id)):
            dl.release_mbid = release_mbid
            s.add(dl)
            s.commit()
    return _save(imp, release_mbid=release_mbid, force=False)


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
    return None


async def _pipeline(imp: Import) -> None:
    settings = get_settings()
    if imp.step == "verify":
        await _verify(imp, settings)
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
        transcode = await audio.transcode_check(results, picks, settings.cutoff_db)
        _save(imp, transcode=transcode)
        if transcode["album"]["kind"] in ("lossy", "upsampled"):
            log_event(imp.job_id, imp.rg_mbid, f"Possible transcode: {transcode['album']['detail']}")


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
            monitor_new_items=settings.lidarr_monitor_new_items,
        )
        _note(imp, f"Added {album.artist} to Lidarr (monitor {settings.lidarr_monitor}, new items {settings.lidarr_monitor_new_items})")
        # Wait for the refresh so the album exists in Lidarr before the edition is selected.
        cmd = await lidarr.command("RefreshArtist", artistIds=[artist["id"]])
        await lidarr.wait_for_command(cmd["id"])
    path = Path(artist["path"])
    if not within(path, settings.music_dir):
        raise JobFailed(f"Lidarr's folder for {album.artist} is {path}, outside the music library at {settings.music_dir}")
    return path


def _place(imp: Import, target: Path) -> None:
    src = Path(imp.source_dir or "")
    if imp.album_dir and Path(imp.album_dir).is_dir() and not src.exists():
        _save(imp, step="beets")  # placed before a restart
        return
    if target.exists():
        raise Blocked(f"{target} already exists; the picker never overwrites. Move or rename it, then Retry")
    if not src.is_dir():
        raise JobFailed(f"the downloaded folder {src} is gone")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(src, target)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        shutil.move(str(src), str(target))  # different filesystems: copy, then remove the download
    _save(imp, album_dir=str(target), step="beets")
    log_event(imp.job_id, imp.rg_mbid, f"Placed at {target}")


async def _beets(imp: Import, settings: Settings) -> bool:
    album_dir = Path(imp.album_dir or "")
    if imp.source == "lidarr" and not settings.beets_pretend:
        # A re-imported or upgraded album may already be in the beets library: start clean.
        await beets.forget_album(settings, imp.release_mbid, album_dir)
    result = await beets.import_album(settings, album_dir, imp.release_mbid, force=imp.force,
                                      pretend=settings.beets_pretend)
    _save(imp, beets_output=result.output)
    if result.imported:
        _save(imp, step="lidarr", tags=audio.tag_snapshot(album_dir))
        log_event(imp.job_id, imp.rg_mbid, "beets tagged the album" + (" (forced)" if imp.force else ""))
        return True
    if result.skipped:
        _save(imp, state="skipped")
        log_event(imp.job_id, imp.rg_mbid, "beets skipped the album: the match was not strong enough. Review its output, then Force apply")
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


def imports_for(download_ids: list[int]) -> dict[int, Import]:
    with session() as s:
        rows = s.exec(select(Import).where(Import.download_id.in_(download_ids)).order_by(Import.id)).all()
    return {i.download_id: i for i in rows}
