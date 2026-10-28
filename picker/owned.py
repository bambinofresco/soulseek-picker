"""Is this album already in the library?

Checked before any bandwidth is spent: on the album page (fresh, every source) and in the
Find-album results (local sources only, cached). Sources, in order of trust:

    picker   an import of this release group that finished, whose folder still exists
    Lidarr   the album has track files; its monitored release says which edition
    beets    the beets library holds an album with this release-group ID
    index    files on disk carry this release-group ID (picker/library.py), whoever tagged them

Nothing here changes anything: a download of an owned album still works, and stops at
placing because the folder exists (the picker never overwrites).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path

from sqlmodel import select

from . import beets, library
from .clients import services
from .clients.http import ServiceError
from .config import Settings
from .db import cache_get, cache_put, session
from .models import Import

LIBRARY_TTL = timedelta(minutes=2)


@dataclass
class Owned:
    source: str  # picker | lidarr | beets
    release_mbid: str | None = None
    path: str | None = None
    have: int | None = None  # track files Lidarr sees
    want: int | None = None
    import_id: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _from_history(rg_mbid: str) -> Owned | None:
    with session() as s:
        rows = s.exec(select(Import).where(Import.rg_mbid == rg_mbid, Import.state == "imported")
                      .order_by(Import.id.desc())).all()
    for imp in rows:
        if imp.album_dir and Path(imp.album_dir).is_dir():
            return Owned("picker", imp.release_mbid, imp.album_dir, import_id=imp.id)
    return None


async def _from_lidarr(rg_mbid: str, settings: Settings) -> Owned | None:
    if not settings.lidarr_api_key:
        return None
    lidarr = services().lidarr
    album = await lidarr.album_by_foreign_id(rg_mbid)
    stats = (album or {}).get("statistics") or {}
    have = stats.get("trackFileCount") or 0
    if not album or not have:
        return None
    release = next((r.get("foreignReleaseId") for r in album.get("releases") or [] if r.get("monitored")), None)
    path = None
    try:
        files = await lidarr.track_files(album["id"])
    except ServiceError:
        files = []
    if files and files[0].get("path"):
        path = str(Path(files[0]["path"]).parent.as_posix())
    return Owned("lidarr", release, path, have, stats.get("trackCount"))


async def _beets_albums(settings: Settings) -> dict[str, dict]:
    """Release group -> {release, path} for every album in the beets library (cached)."""
    cached = cache_get("beets:albums", LIBRARY_TTL)
    if cached is not None:
        return cached
    albums = await beets.album_list(settings)
    cache_put("beets:albums", albums)
    return albums


async def check_album(rg_mbid: str, settings: Settings) -> Owned | None:
    """Every source, freshest first; None when nothing says the album is in the library."""
    owned = _from_history(rg_mbid)
    if owned:
        return owned
    try:
        owned = await _from_lidarr(rg_mbid, settings)
    except ServiceError:
        owned = None
    if owned:
        return owned
    try:
        entry = (await _beets_albums(settings)).get(rg_mbid)
    except OSError:
        entry = None
    if entry and (not entry.get("path") or Path(entry["path"]).is_dir()):
        return Owned("beets", entry.get("release"), entry.get("path"))
    return _from_index(rg_mbid)


def _from_index(rg_mbid: str) -> Owned | None:
    """Files on disk tagged with this release group, whoever tagged them (Lidarr's own grabs,
    albums beets imported elsewhere, folders the index found that no other source knows)."""
    files = library.album_files(rg_mbid)
    if not files:
        return None
    releases = Counter(f.release_mbid for f in files if f.release_mbid)
    release = releases.most_common(1)[0][0] if releases else None
    folders = Counter(f.folder for f in files)
    return Owned("index", release, folders.most_common(1)[0][0], have=len(files))


async def owned_release_groups(rg_mbids: list[str], settings: Settings) -> dict[str, Owned]:
    """The local sources only (history, beets, the index): cheap enough for a results list."""
    found: dict[str, Owned] = {}
    for rg in rg_mbids:
        owned = _from_history(rg)
        if owned:
            found[rg] = owned
    try:
        beets_library = await _beets_albums(settings)
    except OSError:
        beets_library = {}
    for rg in rg_mbids:
        entry = beets_library.get(rg)
        if rg not in found and entry and (not entry.get("path") or Path(entry["path"]).is_dir()):
            found[rg] = Owned("beets", entry.get("release"), entry.get("path"))
    for rg in rg_mbids:
        if rg not in found:
            owned = _from_index(rg)
            if owned:
                found[rg] = owned
    return found
