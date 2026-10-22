"""autoscan's manual trigger: asked to scan each imported or retagged folder."""

import base64

import httpx
import pytest

from picker.clients.autoscan import AutoscanClient
from picker.clients.http import ServiceError


def client(handler, user="admin", password="pw"):
    c = AutoscanClient("http://autoscan:3030", user, password)
    c.http._transport = httpx.MockTransport(handler)
    return c


async def test_scan_posts_the_folder_with_basic_auth():
    seen = []

    def handle(req):
        seen.append(req)
        return httpx.Response(200)

    await client(handle).scan("/mediadata/media/music/Vince Watson/Moments in Time (2002)")
    req = seen[0]
    assert req.method == "POST" and req.url.path == "/triggers/manual"
    assert req.url.params["dir"] == "/mediadata/media/music/Vince Watson/Moments in Time (2002)"
    assert req.headers["authorization"] == "Basic " + base64.b64encode(b"admin:pw").decode()


async def test_check_queues_nothing_and_reads_the_answer():
    assert await client(lambda req: httpx.Response(400, text="dir missing")).check() == 400
    with pytest.raises(ServiceError, match="username and password"):
        await client(lambda req: httpx.Response(401)).check()
    with pytest.raises(ServiceError, match="autoscan's URL"):
        await client(lambda req: httpx.Response(404)).check()


def test_no_credentials_means_no_auth_header():
    assert AutoscanClient("http://a", "", "").http.auth is None
