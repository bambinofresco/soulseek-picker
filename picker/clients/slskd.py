"""slskd API client (base /api/v0, header X-API-Key)."""

from __future__ import annotations

import asyncio
import uuid

import httpx

from .http import DEFAULT_TIMEOUT, ServiceError, request

SERVICE = "slskd"

# slskd answers 429 to a second concurrent search POST, so serialise them process-wide.
_search_post_lock = asyncio.Lock()


def state_flags(state: str | None) -> set[str]:
    """'Completed, Succeeded' -> {'Completed', 'Succeeded'}."""
    return {part.strip() for part in (state or "").split(",") if part.strip()}


FAILED_FLAGS = {"Cancelled", "TimedOut", "Errored", "Rejected", "Aborted", "Failed"}


def transfer_succeeded(state: str | None) -> bool:
    flags = state_flags(state)
    return "Completed" in flags and "Succeeded" in flags


def transfer_failed(state: str | None) -> bool:
    flags = state_flags(state)
    return "Completed" in flags and bool(flags & FAILED_FLAGS)


class SlskdClient:
    def __init__(self, base_url: str, api_key: str):
        self.http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/api/v0",
            headers={"X-API-Key": api_key},
            timeout=DEFAULT_TIMEOUT,
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _call(self, method: str, path: str, **kwargs) -> httpx.Response:
        return await request(self.http, SERVICE, method, path, **kwargs)

    async def application(self) -> dict:
        return (await self._call("GET", "/application")).json()

    async def options(self) -> dict:
        return (await self._call("GET", "/options")).json()

    # Searches

    async def start_search(self, text: str, *, timeout_s: int, response_limit: int, file_limit: int) -> str:
        search_id = str(uuid.uuid4())
        body = {
            "id": search_id,
            "searchText": text,
            "searchTimeout": timeout_s,
            "responseLimit": response_limit,
            "fileLimit": file_limit,
            "filterResponses": True,
            "minimumResponseFileCount": 1,
        }
        async with _search_post_lock:
            for attempt in range(6):
                resp = await self._call("POST", "/searches", json=body, ok_statuses=(429,))
                if resp.status_code != 429:
                    return search_id
                await asyncio.sleep(1 + attempt)
        raise ServiceError(SERVICE, "search rejected with 429 (another search is starting)", 429)

    async def get_search(self, search_id: str) -> dict:
        return (await self._call("GET", f"/searches/{search_id}")).json()

    async def search_responses(self, search_id: str) -> list[dict]:
        return (await self._call("GET", f"/searches/{search_id}/responses")).json()

    async def stop_search(self, search_id: str) -> None:
        await self._call("PUT", f"/searches/{search_id}", ok_statuses=(404,))

    # Transfers

    async def enqueue_batch(self, username: str, files: list[dict], destination: str) -> dict | None:
        """Enqueue via the batch API (slskd >= 0.26). Returns None when the server lacks it (404)."""
        body = {"username": username, "files": files, "options": {"destination": destination}}
        resp = await self._call("POST", "/transfers/downloads/batches", json=body, ok_statuses=(404,))
        if resp.status_code == 404:
            return None
        data = resp.json() if resp.content else {}
        if resp.status_code == 200:
            raise ServiceError(SERVICE, "batch accepted but no files were queued", 200)
        data["_status"] = resp.status_code  # 201 all queued, 207 partial
        return data

    async def enqueue_legacy(self, username: str, files: list[dict]) -> None:
        await self._call("POST", f"/transfers/downloads/{username}", json=files)

    async def get_batch(self, batch_id: str) -> dict:
        return (await self._call("GET", f"/transfers/downloads/batches/{batch_id}")).json()

    async def user_downloads(self, username: str) -> list[dict]:
        """Flat list of this user's download transfers."""
        resp = await self._call("GET", f"/transfers/downloads/{username}", ok_statuses=(404,))
        if resp.status_code == 404:
            return []
        data = resp.json()
        return [f for d in data.get("directories", []) for f in d.get("files", [])]

    async def cancel_download(self, username: str, transfer_id: str) -> None:
        await self._call("DELETE", f"/transfers/downloads/{username}/{transfer_id}", ok_statuses=(404,))

    async def browse_directory(self, username: str, directory: str) -> list[dict]:
        resp = await self._call("POST", f"/users/{username}/directory", json={"directory": directory})
        data = resp.json()
        dirs = data if isinstance(data, list) else [data]
        return [f for d in dirs for f in d.get("files", [])]
