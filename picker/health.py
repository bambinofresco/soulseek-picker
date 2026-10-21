"""Startup path checks and the settings page's connection tests."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import PurePosixPath

from .clients import services
from .clients.http import ServiceError
from .config import Settings

# Set by the last slskd test; shown on the settings page.
slskd_dir_warning: str | None = None


def path_warnings(settings: Settings) -> list[str]:
    warnings = []
    for label, path in (("Download directory", settings.download_dir), ("Music library", settings.music_dir)):
        if not path.is_dir():
            warnings.append(f"{label} {path} does not exist inside the container")
        elif not os.access(path, os.W_OK):
            warnings.append(f"{label} {path} is not writable by this user (check PUID/PGID)")
    return warnings


@dataclass
class TestResult:
    ok: bool
    message: str


async def test_service(name: str, settings: Settings) -> TestResult:
    try:
        return await _TESTS[name](settings)
    except ServiceError as exc:
        return TestResult(False, exc.message)


async def _test_slskd(settings: Settings) -> TestResult:
    global slskd_dir_warning
    if not settings.slskd_api_key:
        return TestResult(False, "No API key set")
    slskd = services().slskd
    app = await slskd.application()
    version = app.get("version", {})
    version = version.get("full") or version.get("current") if isinstance(version, dict) else version
    message = f"Connected to slskd {version or ''}".strip()
    try:
        options = await slskd.options()
    except ServiceError:
        return TestResult(True, message + " (could not read its options to check the download directory)")
    remote_dir = (options.get("directories") or {}).get("downloads")
    slskd_dir_warning = None
    if remote_dir and PurePosixPath(remote_dir) != PurePosixPath(settings.download_dir.as_posix()):
        slskd_dir_warning = (
            f"slskd downloads to {remote_dir} but the picker reads {settings.download_dir}. "
            "Mount the same host folder at the same path in both containers."
        )
    return TestResult(True, message + f"; downloads to {remote_dir}")


async def _test_lidarr(settings: Settings) -> TestResult:
    if not settings.lidarr_api_key:
        return TestResult(False, "No API key set")
    lidarr = services().lidarr
    status = await lidarr.system_status()
    roots = ", ".join(r.get("path", "?") for r in await lidarr.root_folders()) or "none"
    quality = ", ".join(f"{p['id']}={p.get('name')}" for p in await lidarr.quality_profiles())
    metadata = ", ".join(f"{p['id']}={p.get('name')}" for p in await lidarr.metadata_profiles())
    message = (f"Connected to Lidarr {status.get('version', '')}. Root folders: {roots}. "
               f"Quality profiles: {quality}. Metadata profiles: {metadata}")
    if settings.lidarr_root_folder not in roots.split(", "):
        return TestResult(False, message + f". The root folder setting ({settings.lidarr_root_folder}) is not one of them")
    return TestResult(True, message)


async def _test_plex(settings: Settings) -> TestResult:
    if not settings.plex_token or not settings.plex_section_id:
        return TestResult(False, "Token and music section ID are both required")
    section = await services().plex.section()
    if section.get("type") != "artist":
        return TestResult(False, f"Section {settings.plex_section_id} is “{section.get('title')}”, not a music library")
    return TestResult(True, f"Music section “{section.get('title')}” found")


async def _test_musicbrainz(settings: Settings) -> TestResult:
    if not settings.mb_contact:
        return TestResult(False, "Set a contact (email or URL) for the User-Agent; generic agents are throttled harder")
    groups = await services().mb.search_release_groups("Radiohead", "OK Computer", limit=1)
    return TestResult(bool(groups), "Search works" if groups else "Search returned nothing")


_TESTS = {
    "slskd": _test_slskd,
    "lidarr": _test_lidarr,
    "plex": _test_plex,
    "musicbrainz": _test_musicbrainz,
}
SERVICES = list(_TESTS)
