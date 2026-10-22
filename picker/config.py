"""Settings: /config/picker.yaml, overridden field-by-field by environment variables.

Each field's environment variable is its name in upper case (SLSKD_API_KEY, MUSIC_DIR, ...).
CONFIG_DIR itself can only come from the environment, since it locates the YAML file.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, field_validator

SECRET_FIELDS = {"slskd_api_key", "lidarr_api_key", "plex_token", "webhook_password"}


class Settings(BaseModel):
    port: int = 8765
    config_dir: Path = Path("/config")
    # One /mediadata volume, mapped the same way as in slskd and Lidarr.
    download_dir: Path = Path("/mediadata/soulseek/complete")  # slskd's download directory
    music_dir: Path = Path("/mediadata/media/music")

    # On Docker's bridge network container names don't resolve: use http://<server LAN IP>:<port>.
    slskd_url: str = ""  # e.g. http://192.168.1.10:5030 (published on Gluetun)
    slskd_api_key: str = ""
    lidarr_url: str = ""  # e.g. http://192.168.1.10:8686
    lidarr_api_key: str = ""
    plex_url: str = ""  # e.g. http://192.168.1.10:32400
    plex_token: str = ""
    plex_section_id: str = ""
    # The library root as Plex sees it. Empty means the same path as music_dir.
    plex_music_path: str = ""
    mb_contact: str = ""

    # Lidarr defaults for adding an artist
    lidarr_root_folder: str = "/mediadata/media/music"
    lidarr_quality_profile_id: int = 1
    lidarr_metadata_profile_id: int = 1
    lidarr_monitor: str = "none"  # addOptions.monitor for the artist's existing albums
    lidarr_monitor_new_items: str = "new"
    # Basic auth Lidarr's Webhook connection must send to /hooks/lidarr
    webhook_username: str = "lidarr"
    webhook_password: str = ""

    # Import
    # The folder part of Lidarr's track format; see picker/naming.py for the tokens.
    album_folder_template: str = "{Album Title} ({Release Year})"
    beets_timeout_minutes: int = 20
    beets_pretend: bool = False  # run beets with --pretend (tests and dry runs); tags nothing

    # Search
    search_timeout: int = 15
    search_response_limit: int = 500
    search_file_limit: int = 20000
    min_flac_folders: int = 5

    # Preferences
    flac_only: bool = True
    prefer_original: bool = True
    auto_import: bool = False
    stuck_days: int = 3
    stall_timeout_minutes: int = 30  # bytes started flowing, then stopped
    queue_timeout_hours: int = 6  # waiting in the uploader's queue
    download_scans: bool = False
    max_sidecar_mb: int = 15

    @field_validator("album_folder_template")
    @classmethod
    def _known_tokens(cls, value: str) -> str:
        from .naming import unknown_tokens

        bad = unknown_tokens(value)
        if bad:
            raise ValueError(f"album_folder_template has unknown token(s): {', '.join(bad)}")
        return value

    @property
    def db_path(self) -> Path:
        return self.config_dir / "picker.db"

    @property
    def yaml_path(self) -> Path:
        return self.config_dir / "picker.yaml"

    @property
    def beets_dir(self) -> Path:
        return self.config_dir / "beets"

    @property
    def spectrogram_dir(self) -> Path:
        return self.config_dir / "spectrograms"


def env_locked() -> set[str]:
    """Fields set by environment variables; the settings page shows them read-only."""
    return {name for name in Settings.model_fields if os.environ.get(name.upper(), "").strip()}


def load_settings() -> Settings:
    config_dir = Path(os.environ.get("CONFIG_DIR") or "/config")
    values: dict = {}
    yaml_path = config_dir / "picker.yaml"
    if yaml_path.exists():
        values.update(yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {})
    for name in Settings.model_fields:
        if os.environ.get(name.upper(), "").strip():  # Unraid passes empty template variables too
            values[name] = os.environ[name.upper()]
    values["config_dir"] = config_dir
    return Settings.model_validate(values)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def save_settings(updates: dict) -> Settings:
    """Write updates to picker.yaml (skipping env-locked fields) and reload."""
    global _settings
    current = get_settings()
    yaml_path = current.yaml_path
    stored: dict = {}
    if yaml_path.exists():
        stored = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    locked = env_locked() | {"config_dir"}
    for name, value in updates.items():
        if name in Settings.model_fields and name not in locked:
            stored[name] = value
    # Validate before writing so a bad value never lands on disk.
    Settings.model_validate({**current.model_dump(), **stored})
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    yaml_path.write_text(yaml.safe_dump(stored, sort_keys=True), encoding="utf-8")
    _settings = load_settings()
    return _settings
