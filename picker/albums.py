"""Resolve albums (release groups) and their editions through MusicBrainz, with caching."""

from __future__ import annotations

from datetime import timedelta

from .clients import services
from .db import cache_get, cache_put, session
from .editions import Edition, editions_from_releases
from .models import Album

EDITIONS_TTL = timedelta(days=7)
RG_TTL = timedelta(days=7)


def artist_credit_name(entity: dict) -> str:
    return "".join(c.get("name", "") + c.get("joinphrase", "") for c in entity.get("artist-credit", []))


def first_artist_id(entity: dict) -> str | None:
    credits = entity.get("artist-credit", [])
    return credits[0].get("artist", {}).get("id") if credits else None


async def release_group_data(rg_mbid: str) -> dict:
    key = f"rg:{rg_mbid}"
    data = cache_get(key, RG_TTL)
    if data is None:
        data = await services().mb.release_group(rg_mbid)
        cache_put(key, data)
    return data


async def editions(rg_mbid: str) -> list[dict]:
    """Every release in the group, as MusicBrainz returns it (cached for 7 days)."""
    key = f"editions:{rg_mbid}"
    data = cache_get(key, EDITIONS_TTL)
    if data is None:
        data = await services().mb.releases_in_group(rg_mbid)
        cache_put(key, data)
    return data


async def album_editions(rg_mbid: str) -> list[Edition]:
    return editions_from_releases(await editions(rg_mbid))


def label_names(releases: list[dict]) -> list[str]:
    names: list[str] = []
    for release in releases:
        for info in release.get("label-info", []) or []:
            name = (info.get("label") or {}).get("name")
            if name and name not in names:
                names.append(name)
    return names


def track_count(release: dict) -> int:
    return sum(len(m.get("tracks", []) or []) or m.get("track-count", 0) for m in release.get("media", []))


async def ensure_album(rg_mbid: str) -> Album:
    """Return the Album row, creating it from MusicBrainz if needed."""
    with session() as s:
        album = s.get(Album, rg_mbid)
    if album:
        return album
    rg = await release_group_data(rg_mbid)
    album = Album(
        rg_mbid=rg_mbid,
        artist_mbid=first_artist_id(rg),
        artist=artist_credit_name(rg),
        title=rg.get("title", ""),
        first_release_date=rg.get("first-release-date") or None,
        primary_type=rg.get("primary-type"),
    )
    with session() as s:
        album = s.merge(album)
        s.commit()
    return album


async def load_min_track_count(album: Album) -> int | None:
    """Fetch the editions and record the smallest track count on the album."""
    counts = [n for n in (track_count(r) for r in await editions(album.rg_mbid)) if n]
    album.min_track_count = min(counts) if counts else None
    with session() as s:
        s.merge(album)
        s.commit()
    return album.min_track_count


async def resolve_mb_url(kind: str, mbid: str) -> str:
    """Release-group ID for a pasted release or release-group URL."""
    if kind == "release-group":
        return mbid
    release = await services().mb.release(mbid)
    return release["release-group"]["id"]
