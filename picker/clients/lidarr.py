"""Lidarr API client (base /api/v1, header X-Api-Key)."""

from __future__ import annotations

import asyncio
import time

import httpx

from .http import DEFAULT_TIMEOUT, ServiceError, request

SERVICE = "Lidarr"


class LidarrClient:
    def __init__(self, base_url: str, api_key: str):
        self.http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/api/v1",
            headers={"X-Api-Key": api_key},
            timeout=DEFAULT_TIMEOUT,
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _get(self, path: str, **params) -> object:
        return (await request(self.http, SERVICE, "GET", path, params=params or None)).json()

    async def system_status(self) -> dict:
        return await self._get("/system/status")

    async def root_folders(self) -> list[dict]:
        return await self._get("/rootfolder")

    async def quality_profiles(self) -> list[dict]:
        return await self._get("/qualityprofile")

    async def metadata_profiles(self) -> list[dict]:
        return await self._get("/metadataprofile")

    # Artists

    async def artists(self) -> list[dict]:
        return await self._get("/artist")

    async def artist_by_mbid(self, artist_mbid: str) -> dict | None:
        for artist in await self.artists():
            if artist.get("foreignArtistId") == artist_mbid:
                return artist
        return None

    async def lookup_artist(self, artist_mbid: str) -> dict | None:
        results = await self._get("/artist/lookup", term=f"lidarr:{artist_mbid}")
        return results[0] if results else None

    async def add_artist(self, lookup: dict, *, root_folder: str, quality_profile_id: int, metadata_profile_id: int,
                         monitor: str, monitor_new_items: str) -> dict:
        body = {
            **lookup,
            "qualityProfileId": quality_profile_id,
            "metadataProfileId": metadata_profile_id,
            "rootFolderPath": root_folder,
            "monitored": True,
            "monitorNewItems": monitor_new_items,
            # Adding one old album must never make Lidarr search the back catalogue.
            "addOptions": {"monitor": monitor, "searchForMissingAlbums": False},
        }
        return (await request(self.http, SERVICE, "POST", "/artist", json=body)).json()

    # Albums

    async def album_by_foreign_id(self, rg_mbid: str) -> dict | None:
        albums = await self._get("/album", foreignAlbumId=rg_mbid)
        return albums[0] if albums else None

    async def album(self, album_id: int) -> dict:
        return await self._get(f"/album/{album_id}")

    async def update_album(self, album: dict) -> dict:
        return (await request(self.http, SERVICE, "PUT", f"/album/{album['id']}", json=album)).json()

    async def track_files(self, album_id: int) -> list[dict]:
        return await self._get("/trackfile", albumId=album_id)

    async def track_files_for_artist(self, artist_id: int) -> list[dict]:
        return await self._get("/trackfile", artistId=artist_id)

    async def wanted_missing(self, page: int, page_size: int = 100) -> dict:
        return await self._get(
            "/wanted/missing", monitored="true", includeArtist="true", page=page, pageSize=page_size,
            sortKey="releaseDate", sortDirection="descending",
        )

    # Commands

    async def command(self, name: str, **body) -> dict:
        return (await request(self.http, SERVICE, "POST", "/command", json={"name": name, **body})).json()

    async def wait_for_command(self, command_id: int, timeout: float = 300, interval: float = 2) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            cmd = await self._get(f"/command/{command_id}")
            status = (cmd.get("status") or "").lower()
            if status == "completed":
                return cmd
            if status in ("failed", "aborted", "cancelled", "orphaned"):
                raise ServiceError(SERVICE, f"command {cmd.get('name')} {status}: {cmd.get('message') or ''}".strip())
            if time.monotonic() > deadline:
                raise ServiceError(SERVICE, f"command {cmd.get('name')} still {status or 'queued'} after {timeout:.0f} s")
            await asyncio.sleep(interval)
