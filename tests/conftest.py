import pytest

from picker import config
from picker.config import Settings
from picker.db import init_db


@pytest.fixture
def settings(tmp_path, monkeypatch):
    """Isolated settings and database under a temp directory."""
    (tmp_path / "downloads").mkdir()
    (tmp_path / "music").mkdir()
    s = Settings(
        config_dir=tmp_path / "config",
        download_dir=tmp_path / "downloads",
        music_dir=tmp_path / "music",
        slskd_api_key="test",
        stall_timeout_minutes=30,
        auto_import=False,  # tests opt in to automatic import explicitly
    )
    s.config_dir.mkdir()
    monkeypatch.setattr(config, "_settings", s)
    init_db(s.db_path)
    return s
