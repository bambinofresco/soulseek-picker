"""autoscan's manual trigger (https://github.com/Cloudbox/autoscan), with HTTP Basic auth.

The picker places an album with one directory rename, which inotify watchers can miss
(one event for the folder, none per file, and none at all if the artist folder was created a
moment earlier). Asking autoscan for the exact folder is reliable, and autoscan still does
its own queueing, anchor checks and Plex scan.
"""

from __future__ import annotations

import httpx

from .http import DEFAULT_TIMEOUT, ServiceError, request

SERVICE = "autoscan"


class AutoscanClient:
    def __init__(self, base_url: str, username: str, password: str):
        auth = httpx.BasicAuth(username, password) if username or password else None
        self.http = httpx.AsyncClient(base_url=base_url.rstrip("/"), auth=auth, timeout=DEFAULT_TIMEOUT)

    async def aclose(self) -> None:
        await self.http.aclose()

    async def scan(self, folder: str) -> None:
        """Queue a scan of one folder (autoscan waits minimum-age, checks anchors, then scans)."""
        await request(self.http, SERVICE, "POST", "/triggers/manual", params={"dir": folder})

    async def check(self) -> int:
        """Reach the manual trigger without a folder: nothing is queued.

        401 means bad credentials; any other answer (autoscan replies 400 for the missing
        `dir`) means the URL and login work.
        """
        resp = await request(self.http, SERVICE, "POST", "/triggers/manual", retries=1,
                             ok_statuses=tuple(range(400, 500)))
        if resp.status_code == 401:
            raise ServiceError(SERVICE, "HTTP 401: check the autoscan username and password", 401)
        if resp.status_code == 404:
            raise ServiceError(SERVICE, "HTTP 404: no /triggers/manual here; is this autoscan's URL and port?", 404)
        return resp.status_code
