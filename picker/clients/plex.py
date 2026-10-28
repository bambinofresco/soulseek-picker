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

    # Finding tracks. Plex cannot be asked for a MusicBrainz ID; callers match Media.Part.file.

    @staticmethod
    def _metadata(data: dict) -> list[dict]:
        return (data.get("MediaContainer") or {}).get("Metadata") or []

    async def tracks_titled(self, title: str) -> list[dict]:
        """Tracks in the music section whose title contains `title` (Plex's title filter)."""
        data = (await request(self.http, SERVICE, "GET", f"/library/sections/{self.section_id}/all",
                              params={"type": 10, "title": title})).json()
        return self._metadata(data)

    async def albums_titled(self, title: str) -> list[dict]:
        data = (await request(self.http, SERVICE, "GET", f"/library/sections/{self.section_id}/all",
                              params={"type": 9, "title": title})).json()
        return self._metadata(data)

    async def children(self, rating_key: str) -> list[dict]:
        """An album's tracks (or an artist's albums)."""
        data = (await request(self.http, SERVICE, "GET", f"/library/metadata/{rating_key}/children")).json()
        return self._metadata(data)

    # Playlists. Undocumented but stable; what python-plexapi wraps.

    async def machine_identifier(self) -> str:
        data = (await request(self.http, SERVICE, "GET", "/identity")).json()
        return (data.get("MediaContainer") or {}).get("machineIdentifier") or ""

    async def _uri(self, rating_keys: list[str]) -> str:
        machine = await self.machine_identifier()
        return f"server://{machine}/com.plexapp.plugins.library/library/metadata/{','.join(rating_keys)}"

    async def playlists(self) -> list[dict]:
        data = (await request(self.http, SERVICE, "GET", "/playlists", params={"playlistType": "audio"})).json()
        return self._metadata(data)

    async def create_playlist(self, title: str, rating_keys: list[str]) -> dict:
        """A new audio playlist holding these tracks, in this order. Plex needs at least one item."""
        data = (await request(self.http, SERVICE, "POST", "/playlists",
                              params={"type": "audio", "title": title, "smart": "0", "uri": await self._uri(rating_keys)})).json()
        items = self._metadata(data)
        if not items:
            raise ServiceError(SERVICE, "playlist creation returned no metadata")
        return items[0]

    async def playlist_items(self, rating_key: str) -> list[dict]:
        """Each item carries ratingKey (the track), playlistItemID (its slot) and Media/Part/file."""
        data = (await request(self.http, SERVICE, "GET", f"/playlists/{rating_key}/items")).json()
        return self._metadata(data)

    async def add_items(self, rating_key: str, track_keys: list[str]) -> None:
        if track_keys:
            await request(self.http, SERVICE, "PUT", f"/playlists/{rating_key}/items", params={"uri": await self._uri(track_keys)})

    async def remove_item(self, rating_key: str, item_id: str) -> None:
        await request(self.http, SERVICE, "DELETE", f"/playlists/{rating_key}/items/{item_id}")

    async def move_item(self, rating_key: str, item_id: str, after: str | None) -> None:
        """Put the item after another one (None: to the top)."""
        params = {"after": after} if after else None
        await request(self.http, SERVICE, "PUT", f"/playlists/{rating_key}/items/{item_id}/move", params=params)
