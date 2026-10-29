"""MusicBrainz web service client with one rate limiter shared by the whole process."""

from __future__ import annotations

import asyncio
import re
import time

import httpx

from .. import __version__
from .http import DEFAULT_TIMEOUT, ServiceError

SERVICE = "MusicBrainz"
BASE_URL = "https://musicbrainz.org/ws/2"

MBID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
URL_RE = re.compile(rf"musicbrainz\.org/(release-group|release)/({MBID})", re.I)


def parse_mb_url(text: str) -> tuple[str, str] | None:
    """'https://musicbrainz.org/release/<id>' -> ('release', '<id>')."""
    m = URL_RE.search(text or "")
    return (m.group(1).lower(), m.group(2).lower()) if m else None


class RateLimiter:
    def __init__(self, interval: float):
        self.interval = interval
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        async with self._lock:
            delay = self._last + self.interval - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


# About 1 request per second per IP; going over returns 503 for everything.
limiter = RateLimiter(1.05)


def _lucene_escape(text: str) -> str:
    return re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r"\\\1", text)


class MusicBrainzClient:
    def __init__(self, contact: str):
        agent = f"SoulseekPicker/{__version__} ( {contact or 'unconfigured'} )"
        self.http = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"User-Agent": agent, "Accept": "application/json"},
            timeout=DEFAULT_TIMEOUT,
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _get(self, path: str, params: dict | None = None, retries: int = 4) -> dict:
        params = {**(params or {}), "fmt": "json"}
        for attempt in range(retries + 1):
            await limiter.wait()
            try:
                resp = await self.http.get(path, params=params)
            except httpx.TransportError as exc:
                if attempt == retries:
                    raise ServiceError(SERVICE, f"cannot reach musicbrainz.org ({type(exc).__name__})") from exc
            else:
                if resp.status_code < 400:
                    return resp.json()
                if resp.status_code == 404:
                    raise ServiceError(SERVICE, f"not found: {path}", 404)
                if resp.status_code < 500 or attempt == retries:
                    raise ServiceError(SERVICE, f"HTTP {resp.status_code} on {path}", resp.status_code)
            await asyncio.sleep(2**attempt)
        raise AssertionError("unreachable")

    async def search_release_groups(self, artist: str, album: str, limit: int = 25) -> list[dict]:
        parts = []
        if album.strip():
            parts.append(f'releasegroup:"{_lucene_escape(album.strip())}"')
        if artist.strip():
            parts.append(f'artist:"{_lucene_escape(artist.strip())}"')
        if not parts:
            return []
        data = await self._get("/release-group/", {"query": " AND ".join(parts), "limit": limit})
        return data.get("release-groups", [])

    async def release_group_track_counts(self, rg_mbid: str) -> list[int]:
        """Track totals of the group's releases, from one lookup with media (no recordings)."""
        data = await self._get(f"/release-group/{rg_mbid}", {"inc": "releases media"})
        return [sum(m.get("track-count") or 0 for m in r.get("media") or []) for r in data.get("releases") or []]

    async def recordings_by_isrcs(self, isrcs: list[str]) -> list[dict]:
        """Recordings carrying any of these ISRCs (at most 25 per call), each with its `isrcs`
        and every release it is on, including the release group's type and the release's date,
        country and track count. One request instead of one per ISRC."""
        if not isrcs:
            return []
        query = "isrc:(" + " OR ".join(_lucene_escape(i.strip().upper()) for i in isrcs) + ")"
        data = await self._get("/recording/", {"query": query, "limit": 100})
        return data.get("recordings", []) or []

    async def search_recordings(self, artist: str, title: str, duration_ms: int | None = None,
                                limit: int = 10) -> list[dict]:
        """Recordings by title and artist, within 3 s of the duration when known; scored by MusicBrainz."""
        if not title.strip():
            return []
        parts = [f'recording:"{_lucene_escape(title.strip())}"']
        if artist.strip():
            parts.append(f'artist:"{_lucene_escape(artist.strip())}"')
        if duration_ms:
            parts.append(f"dur:[{max(0, duration_ms - 3000)} TO {duration_ms + 3000}]")
        data = await self._get("/recording/", {"query": " AND ".join(parts), "limit": limit})
        return data.get("recordings", []) or []

    async def release_group(self, rg_mbid: str) -> dict:
        return await self._get(f"/release-group/{rg_mbid}", {"inc": "artist-credits"})

    async def release(self, release_mbid: str) -> dict:
        return await self._get(f"/release/{release_mbid}", {"inc": "release-groups artist-credits"})

    async def discid_releases(self, disc_id: str, toc: str) -> list[dict]:
        """Releases carrying this disc ID; with `toc`, an unknown ID falls back to a fuzzy TOC match."""
        try:
            data = await self._get(
                f"/discid/{disc_id}",
                {"toc": toc, "cdstubs": "no", "inc": "labels recordings release-groups"},
            )
        except ServiceError as exc:
            if exc.status == 404:
                return []
            raise
        return data.get("releases", []) or []

    async def releases_in_group(self, rg_mbid: str) -> list[dict]:
        """Every release in the group with recordings, labels and disc IDs.

        Pages are capped by track count as well as by limit, so offset advances
        by the number of releases actually returned.
        """
        releases: list[dict] = []
        offset = 0
        while True:
            data = await self._get(
                "/release",
                {"release-group": rg_mbid, "inc": "recordings labels discids", "limit": 100, "offset": offset},
            )
            page = data.get("releases", [])
            releases.extend(page)
            offset += len(page)
            if not page or offset >= data.get("release-count", 0):
                return releases
