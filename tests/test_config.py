import pytest

from picker import config


def test_defaults_match_the_unraid_layout(monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    s = config.load_settings()
    assert str(s.download_dir).replace("\\", "/") == "/mediadata/soulseek/complete"
    assert str(s.music_dir).replace("\\", "/") == "/mediadata/media/music"
    assert s.lidarr_root_folder == "/mediadata/media/music"
    assert s.album_folder_template == "{Album Title} ({Release Year})"
    assert s.plex_music_path == ""  # identity mapping
    assert s.auto_import is True  # imports by itself when every check passes
    assert (s.stall_timeout_minutes, s.queue_timeout_hours) == (30, 6)


def test_empty_environment_variables_do_not_lock_settings(monkeypatch, tmp_path):
    """Unraid passes every template variable, empty ones included."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("SLSKD_API_KEY", "")
    monkeypatch.setenv("LIDARR_URL", "http://192.168.1.10:8686")
    (tmp_path / "picker.yaml").write_text("slskd_api_key: from-the-ui\n", encoding="utf-8")
    s = config.load_settings()
    assert s.slskd_api_key == "from-the-ui"
    assert s.lidarr_url == "http://192.168.1.10:8686"
    assert config.env_locked() >= {"lidarr_url"} and "slskd_api_key" not in config.env_locked()


def test_cli_module_imports():
    import picker.__main__  # noqa: F401  (the probe CLI lives here; nothing else imports it)


def test_unknown_template_tokens_are_rejected():
    with pytest.raises(ValueError, match="Bogus"):
        config.Settings(album_folder_template="{Album Title} {Bogus}")


def test_saving_the_form_stores_only_values_that_differ_from_the_defaults(monkeypatch, tmp_path):
    """The Settings form submits every field; untouched ones must not freeze today's defaults."""
    import yaml
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config, "_settings", None)
    defaults = config.Settings()
    form = {name: getattr(defaults, name) for name in ("auto_import", "stall_timeout_minutes", "flac_only")}
    form["autoscan_url"] = "http://192.168.0.164:3030"
    config.save_settings(form)
    stored = yaml.safe_load((tmp_path / "picker.yaml").read_text())
    assert stored == {"autoscan_url": "http://192.168.0.164:3030"}
    # A real choice that differs from the default is kept, and dropped again once it matches.
    config.save_settings({"auto_import": False})
    assert yaml.safe_load((tmp_path / "picker.yaml").read_text())["auto_import"] is False
    config.save_settings({"auto_import": True})
    assert "auto_import" not in yaml.safe_load((tmp_path / "picker.yaml").read_text())
