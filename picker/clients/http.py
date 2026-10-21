"""Shared request helper: timeouts, retry with backoff on 5xx and connection errors."""

from __future__ import annotations

import asyncio
import logging

import httpx

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


class ServiceError(Exception):
    """A failed call to an external service, with a message safe to show in the UI."""

    def __init__(self, service: str, message: str, status: int | None = None):
        super().__init__(f"{service}: {message}")
        self.service = service
        self.message = message
        self.status = status


async def request(
    client: httpx.AsyncClient,
    service: str,
    method: str,
    url: str,
    *,
    retries: int = 3,
    backoff: float = 1.0,
    ok_statuses: tuple[int, ...] = (),
    **kwargs,
) -> httpx.Response:
    """Send a request, retrying 5xx and transport errors with exponential backoff.

    Raises ServiceError for any final status >= 400 not listed in ok_statuses.
    """
    for attempt in range(retries + 1):
        try:
            resp = await client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            if attempt == retries:
                raise ServiceError(service, f"cannot reach {client.base_url or url} ({type(exc).__name__})") from exc
            log.info("%s %s %s failed (%s), retrying", service, method, url, type(exc).__name__)
        else:
            if resp.status_code >= 500 and resp.status_code not in ok_statuses and attempt < retries:
                log.info("%s %s %s returned %s, retrying", service, method, url, resp.status_code)
            elif resp.status_code >= 400 and resp.status_code not in ok_statuses:
                raise ServiceError(service, _describe(resp), resp.status_code)
            else:
                return resp
        await asyncio.sleep(backoff * 2**attempt)
    raise AssertionError("unreachable")


def _describe(resp: httpx.Response) -> str:
    if resp.status_code in (401, 403):
        return f"HTTP {resp.status_code}: check the API key or token"
    body = resp.text.strip().replace("\n", " ")
    if len(body) > 200:
        body = body[:200] + "…"
    return f"HTTP {resp.status_code} on {resp.request.method} {resp.request.url.path}" + (f": {body}" if body else "")
