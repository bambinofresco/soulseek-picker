"""Plex Media Server client (X-Plex-Token)."""

from __future__ import annotations

import httpx

from .http import DEFAULT_TIMEOUT, ServiceError, request

SERVICE = "Plex"


class PlexClient:
    def __init__(self, base_url: str, token: str, section_id: str):
        self.section_id = section_id
        self.http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"X-Plex-Token": token, "Accept": "application/json"},
            timeout=DEFAULT_TIMEOUT,
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    async def section(self) -> dict:
        """The configured music section's directory entry."""
        data = (await request(self.http, SERVICE, "GET", "/library/sections")).json()
        for directory in data.get("MediaContainer", {}).get("Directory", []):
            if str(directory.get("key")) == str(self.section_id):
                return directory
        raise ServiceError(SERVICE, f"no library section with ID {self.section_id!r}")

    async def scan_path(self, path: str) -> None:
        """Partial scan of one folder; `path` must be the path as Plex sees it."""
        await request(self.http, SERVICE, "GET", f"/library/sections/{self.section_id}/refresh", params={"path": path})
