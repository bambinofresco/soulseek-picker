"""The library index: every audio file under the music directory and the tags that identify it.

The playlist feature needs per-track answers to "do I already have this?", and no single
source has them: Lidarr knows only the albums its metadata profile lists, beets only what it
imported, the picker's history only its own imports. The files on disk know. This module
walks the music directory, reads each file's tags with mutagen (read-only), and keeps one
`libraryfile` row per file, so owned checks are a query.

Scans run as the `index` job: the folder an import or retag just touched, nightly for the
whole library, and on demand from the Library page. Not hourly: a walk wakes every drive in
an Unraid array. Unchanged files (same size and mtime) are not re-read, so a full pass after
the first takes seconds of stat calls. Rows whose files are gone are removed.

With Lidarr configured, a full pass also compares the index with Lidarr's track files, a
loose check that the two agree: files Lidarr doesn't know, and files Lidarr lists that are
not on disk. Nothing is changed on either side.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import mutagen
from sqlmodel import col, delete, select

from .clients import services
from .clients.http import ServiceError
from .config import Settings, get_settings
from .db import cache_get, cache_put, session
from .grouping import AUDIO_EXTS
from .jobs import handler, log_event, worker
from .models import Job, LibraryFile, utcnow

log = logging.getLogger(__name__)

STATUS_KEY = "library:index"
STATUS_TTL = timedelta(days=365)
NIGHTLY_HOUR = 3  # local time
NAME_DURATION_SLACK = 3.0  # seconds, for owned-by-name matches
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

# Easy-tag keys (mutagen normalises Vorbis comments, ID3 and MP4 atoms to these).
TAG_KEYS = {
    "artist": "artist", "albumartist": "albumartist", "album": "album", "title": "title",
    "tracknumber": "tracknumber", "discnumber": "discnumber",
    "musicbrainz_albumid": "release_mbid", "musicbrainz_releasegroupid": "rg_mbid",
    "musicbrainz_trackid": "recording_mbid",  # Picard and beets: the recording ID
}


# Reading one file -----------------------------------------------------------------------

def _first(values) -> str | None:
    if not values:
        return None
    value = values[0] if isinstance(values, list) else values
    text = str(value).strip()
    return text or None


def _int_prefix(text: str | None) -> int | None:
    """'7', '7/12' and '07' all mean 7."""
    if not text:
        return None
    m = re.match(r"\s*(\d+)", text)
    return int(m.group(1)) if m else None


def _mbid(text: str | None) -> str | None:
    return text.lower() if text and UUID_RE.match(text) else None


def read_file(path: Path) -> dict:
    """Identifying tags and stream facts of one file; tags missing or unreadable give None."""
    row: dict = {"duration": None, "bit_depth": None, "sample_rate": None, "error": None}
    for key in TAG_KEYS.values():
        row[key] = None
    try:
        info = mutagen.File(path, easy=True)
    except Exception as exc:  # noqa: BLE001 - mutagen raises many types on odd files
        row["error"] = f"{type(exc).__name__}: {exc}"
        return row
    if info is None:
        row["error"] = "not an audio file mutagen understands"
        return row
    stream = getattr(info, "info", None)
    if stream is not None:
        length = getattr(stream, "length", None)
        row["duration"] = round(float(length), 3) if length else None
        row["bit_depth"] = getattr(stream, "bits_per_sample", None) or None
        row["sample_rate"] = getattr(stream, "sample_rate", None) or None
    raw = getattr(info, "tags", None) or {}
    for tag, column in TAG_KEYS.items():
        try:
            value = _first(raw.get(tag))
        except (KeyError, ValueError):
            value = None
        row[column] = value
    row["tracknumber"] = _int_prefix(row.pop("tracknumber"))
    row["discnumber"] = _int_prefix(row.pop("discnumber"))
    for column in ("release_mbid", "rg_mbid", "recording_mbid"):
        row[column] = _mbid(row[column])
    return row


# Walking ----------------------------------------------------------------------------------

def _walk(root: Path) -> dict[str, os.stat_result]:
    """Every audio file under root: path -> stat. scandir keeps it to one syscall per entry."""
    found: dict[str, os.stat_result] = {}
    stack = [str(root)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            ext = entry.name.rsplit(".", 1)[-1].lower() if "." in entry.name else ""
                            if ext in AUDIO_EXTS:
                                found[entry.path] = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
        except OSError as exc:
            log.warning("cannot list %s: %s", current, exc)
    return found


@dataclass
class ScanResult:
    scope: str  # the folder scanned
    files: int = 0  # files under the scope after the scan
    added: int = 0
    updated: int = 0
    removed: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def scan(folder: Path, settings: Settings) -> ScanResult:
    """Bring the index for `folder` (the whole library when it is music_dir) in line with disk.

    Blocking (stat and tag reads); callers run it in a thread.
    """
    folder = Path(folder)
    result = ScanResult(scope=str(folder))
    on_disk = _walk(folder) if folder.is_dir() else {}
    prefix = str(folder).rstrip("/\\") + os.sep
    with session() as s:
        if folder.resolve() == settings.music_dir.resolve():
            rows = s.exec(select(LibraryFile)).all()
        else:
            rows = s.exec(select(LibraryFile).where(col(LibraryFile.path).startswith(prefix))).all()
        known = {r.path: r for r in rows}
        now = utcnow()
        for path, st in on_disk.items():
            row = known.get(path)
            if row and row.size == st.st_size and row.mtime == st.st_mtime:
                continue
            read = read_file(Path(path))
            if read["error"]:
                result.errors.append(f"{path}: {read['error']}")
            values = {k: v for k, v in read.items() if k != "error"}
            if row is None:
                row = LibraryFile(path=path, folder=str(Path(path).parent), size=st.st_size, mtime=st.st_mtime,
                                  indexed_at=now, **values)
                result.added += 1
            else:
                for k, v in values.items():
                    setattr(row, k, v)
                row.size, row.mtime, row.indexed_at = st.st_size, st.st_mtime, now
                result.updated += 1
            s.add(row)
        gone = [r for p, r in known.items() if p not in on_disk]
        for row in gone:
            s.delete(row)
        result.removed = len(gone)
        s.commit()
    result.files = len(on_disk)
    return result


# Lidarr comparison ------------------------------------------------------------------------

@dataclass
class LidarrDiff:
    checked: bool = False  # False when Lidarr is not configured or did not answer
    error: str | None = None
    lidarr_files: int = 0
    matched: int = 0  # index rows Lidarr also lists
    unknown_to_lidarr: list[str] = field(default_factory=list)  # on disk, not in Lidarr
    missing_on_disk: list[str] = field(default_factory=list)  # in Lidarr, not on disk

    def to_dict(self) -> dict:
        return asdict(self)


async def compare_with_lidarr(settings: Settings) -> LidarrDiff:
    diff = LidarrDiff()
    if not settings.lidarr_api_key:
        return diff
    lidarr = services().lidarr
    try:
        artists = await lidarr.artists()
        lidarr_paths: dict[str, int] = {}
        for artist in artists:
            for f in await lidarr.track_files_for_artist(artist["id"]):
                if f.get("path"):
                    lidarr_paths[os.path.normpath(f["path"])] = f.get("id")
    except ServiceError as exc:
        diff.error = exc.message
        return diff
    diff.checked = True
    diff.lidarr_files = len(lidarr_paths)
    with session() as s:
        rows = s.exec(select(LibraryFile)).all()
        on_disk = {os.path.normpath(r.path): r for r in rows}
        for path, row in on_disk.items():
            file_id = lidarr_paths.get(path)
            if row.lidarr_file_id != file_id:
                row.lidarr_file_id = file_id
                s.add(row)
            if file_id is not None:
                diff.matched += 1
            else:
                diff.unknown_to_lidarr.append(row.path)
        s.commit()
    diff.missing_on_disk = sorted(p for p in lidarr_paths if p not in on_disk)
    diff.unknown_to_lidarr.sort()
    return diff


# The job ----------------------------------------------------------------------------------

def status() -> dict:
    """What the last index pass did; shown on the Library page."""
    return cache_get(STATUS_KEY, STATUS_TTL) or {}


def queue_index(folder: Path | str | None = None) -> Job | None:
    """Queue a scan of one folder, or of the whole library when folder is None.

    A full scan already queued or running covers everything, so nothing is added then.
    """
    payload = {"folder": str(folder)} if folder else {}
    with session() as s:
        pending = s.exec(select(Job).where(Job.kind == "index", Job.status.in_(["queued", "running"]))).all()
    for job in pending:
        if not job.payload.get("folder") or job.payload == payload:
            return None
    return worker.enqueue("index", payload)


@handler("index")
async def run_index(job: Job) -> None:
    settings = get_settings()
    folder = Path(job.payload.get("folder") or settings.music_dir)
    full = folder.resolve() == settings.music_dir.resolve()
    started = utcnow()
    result = await asyncio.to_thread(scan, folder, settings)
    status_row = {**(status() if not full else {}), "started": started.isoformat(), "finished": utcnow().isoformat(),
                  "scope": "library" if full else str(folder), "scan": result.to_dict()}
    if full:
        diff = await compare_with_lidarr(settings)
        status_row["lidarr"] = diff.to_dict()
        with session() as s:
            status_row["files"] = len(s.exec(select(LibraryFile.id)).all())
    cache_put(STATUS_KEY, status_row)
    what = "the library" if full else folder.name
    log_event(job.id, None, f"Indexed {what}: {result.files} files, {result.added} new, {result.updated} changed, "
                            f"{result.removed} gone" + (f", {len(result.errors)} unreadable" if result.errors else ""))


def _seconds_until(hour: int) -> float:
    now = datetime.now()
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def scheduler() -> None:
    """A full index at start (when none exists yet) and nightly at NIGHTLY_HOUR local time."""
    try:
        if not status():
            queue_index()
    except Exception:  # noqa: BLE001 - keep the scheduler alive
        log.exception("could not queue the first library index")
    while True:
        await asyncio.sleep(_seconds_until(NIGHTLY_HOUR))
        try:
            queue_index()
        except Exception:  # noqa: BLE001
            log.exception("could not queue the nightly library index")


# Queries ----------------------------------------------------------------------------------

def _fold(text: str | None) -> str:
    """Lower-case, ASCII-ish, punctuation and bracketed suffixes dropped: 'Sing (Live) ' -> 'sing'."""
    import unicodedata

    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"\s*[\(\[].*?[\)\]]", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    return re.sub(r"^the ", "", text)


@dataclass
class OwnedTrack:
    file: LibraryFile
    how: str  # "recording" | "name"


def owned_recordings(recording_mbids: list[str]) -> dict[str, LibraryFile]:
    """Recording MBID -> a file carrying it (the first by path when several do)."""
    wanted = [m.lower() for m in recording_mbids if m]
    if not wanted:
        return {}
    found: dict[str, LibraryFile] = {}
    with session() as s:
        rows = s.exec(select(LibraryFile).where(col(LibraryFile.recording_mbid).in_(wanted))
                      .order_by(LibraryFile.path)).all()
    for row in rows:
        found.setdefault(row.recording_mbid, row)
    return found


def owned_by_name(artist: str, title: str, duration: float | None) -> LibraryFile | None:
    """A file with the same artist and title (folded) and, when both are known, a duration within 3 s.

    Looser than an ID match: it can pick the album version when the single edit was meant, so
    callers show it as "owned (by name)".
    """
    want_title, want_artist = _fold(title), _fold(artist)
    if not want_title:
        return None
    with session() as s:
        rows = s.exec(select(LibraryFile).where(LibraryFile.title.is_not(None)).order_by(LibraryFile.path)).all()
    for row in rows:
        if _fold(row.title) != want_title:
            continue
        if want_artist and _fold(row.artist) != want_artist and _fold(row.albumartist) != want_artist:
            continue
        if duration and row.duration and abs(row.duration - duration) > NAME_DURATION_SLACK:
            continue
        return row
    return None


def album_files(rg_mbid: str) -> list[LibraryFile]:
    with session() as s:
        return s.exec(select(LibraryFile).where(LibraryFile.rg_mbid == rg_mbid.lower()).order_by(LibraryFile.path)).all()


def folder_files(folder: Path | str) -> list[LibraryFile]:
    prefix = str(folder).rstrip("/\\") + os.sep
    with session() as s:
        return s.exec(select(LibraryFile).where(col(LibraryFile.path).startswith(prefix)).order_by(LibraryFile.path)).all()


@dataclass
class Summary:
    files: int
    folders: int
    with_ids: int  # files carrying a MusicBrainz release ID
    unknown_to_lidarr: int
    last: dict


def summary() -> Summary:
    with session() as s:
        rows = s.exec(select(LibraryFile.folder, LibraryFile.release_mbid, LibraryFile.lidarr_file_id)).all()
    folders = {r[0] for r in rows}
    return Summary(files=len(rows), folders=len(folders), with_ids=sum(1 for r in rows if r[1]),
                   unknown_to_lidarr=sum(1 for r in rows if r[2] is None), last=status())


def untagged_folders(limit: int = 200) -> list[tuple[str, int]]:
    """Folders whose files carry no MusicBrainz release ID: beets never tagged them."""
    with session() as s:
        rows = s.exec(select(LibraryFile.folder, LibraryFile.release_mbid)).all()
    counts: dict[str, int] = {}
    for folder, mbid in rows:
        if not mbid:
            counts[folder] = counts.get(folder, 0) + 1
    return sorted(counts.items())[:limit]


def forget_folder(folder: Path | str) -> None:
    """Drop the rows under a folder that was moved away (an upgrade's replaced copy)."""
    prefix = str(folder).rstrip("/\\") + os.sep
    with session() as s:
        s.exec(delete(LibraryFile).where(col(LibraryFile.path).startswith(prefix)))
        s.commit()
