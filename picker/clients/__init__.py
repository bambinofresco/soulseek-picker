"""One client per external service, rebuilt when settings change."""

from __future__ import annotations

import asyncio

from ..config import Settings, get_settings
from .autoscan import AutoscanClient
from .lidarr import LidarrClient
from .musicbrainz import MusicBrainzClient
from .plex import PlexClient
from .slskd import SlskdClient


class Services:
    def __init__(self, settings: Settings):
        self.slskd = SlskdClient(settings.slskd_url, settings.slskd_api_key)
        self.mb = MusicBrainzClient(settings.mb_contact)
        self.lidarr = LidarrClient(settings.lidarr_url, settings.lidarr_api_key)
        self.plex = PlexClient(settings.plex_url, settings.plex_token, settings.plex_section_id)
        self.autoscan = AutoscanClient(settings.autoscan_url, settings.autoscan_username, settings.autoscan_password)

    async def aclose(self) -> None:
        for client in (self.slskd, self.mb, self.lidarr, self.plex, self.autoscan):
            await client.aclose()


_services: Services | None = None


def services() -> Services:
    global _services
    if _services is None:
        _services = Services(get_settings())
    return _services


async def reset_services() -> None:
    """Rebuild clients after a settings change.

    In-flight calls keep their old client, which is closed once they have had time to finish.
    """
    global _services
    old, _services = _services, None
    if old is not None:
        loop = asyncio.get_running_loop()
        loop.call_later(120, lambda: loop.create_task(old.aclose()))


async def close_services() -> None:
    global _services
    if _services is not None:
        await _services.aclose()
        _services = None
