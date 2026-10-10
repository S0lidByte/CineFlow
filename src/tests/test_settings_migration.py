import json
import os
from pathlib import Path

from program.settings import SettingsManager
from program.utils import get_version

DATA_PATH = Path(os.curdir) / "data"

# Sample old settings data
old_settings_data = {
    "version": "0.7.5",
    "debug": True,
    "log": True,
    "force_refresh": False,
    "map_metadata": True,
    "tracemalloc": False,
    "downloaders": {
        "proxy_url": "https://no_proxy.com",
        "real_debrid": {
            "enabled": False,
            "api_key": "",
        },
        "all_debrid": {
            "enabled": True,
            "api_key": "12345678",
        },
    },
}


def test_load_and_migrate_settings(tmp_path, monkeypatch):
    data_path = tmp_path / "data"
    data_path.mkdir()
    temp_settings_file = data_path / "settings.json"
    version_file = data_path / "VERSION"

    temp_settings_file.write_text(json.dumps(old_settings_data))
    version_file.write_text("9.9.9")

    import program.settings.models

    monkeypatch.delenv("CINEFLOW_SETTINGS_FILENAME", raising=False)
    monkeypatch.delenv("SETTINGS_FILENAME", raising=False)
    monkeypatch.setattr(program.settings, "data_dir_path", data_path)
    settings_manager = SettingsManager()

    assert settings_manager.settings.downloaders.real_debrid.enabled is False
    assert settings_manager.settings.downloaders.all_debrid.enabled is True
    assert settings_manager.settings.downloaders.all_debrid.api_key == "12345678"
    assert settings_manager.settings.downloaders.proxy_url == "https://no_proxy.com"
    assert settings_manager.settings.version == get_version()


def test_force_env_preserves_saved_downloader_key_when_no_env_set(
    tmp_path, monkeypatch
):
    data_path = tmp_path / "data"
    data_path.mkdir()
    temp_settings_file = data_path / "settings.json"
    version_file = data_path / "VERSION"

    saved_data = {
        "downloaders": {
            "all_debrid": {
                "enabled": True,
                "api_key": "persisted_secret_key",
            },
        },
    }
    temp_settings_file.write_text(json.dumps(saved_data))
    version_file.write_text("9.9.9")

    import program.settings.models

    monkeypatch.setenv("CINEFLOW_FORCE_ENV", "true")
    monkeypatch.delenv("CINEFLOW_DOWNLOADERS_ALL_DEBRID_API_KEY", raising=False)
    monkeypatch.delenv("RIVEN_DOWNLOADERS_ALL_DEBRID_API_KEY", raising=False)
    monkeypatch.delenv("CINEFLOW_SETTINGS_FILENAME", raising=False)
    monkeypatch.delenv("SETTINGS_FILENAME", raising=False)

    monkeypatch.setattr(program.settings, "data_dir_path", data_path)

    settings_manager = SettingsManager()
    assert (
        settings_manager.settings.downloaders.all_debrid.api_key
        == "persisted_secret_key"
    )

    # Reload also preserves
    settings_manager.load()
    assert (
        settings_manager.settings.downloaders.all_debrid.api_key
        == "persisted_secret_key"
    )


def test_force_env_applies_explicit_downloader_env_override(tmp_path, monkeypatch):
    data_path = tmp_path / "data"
    data_path.mkdir()
    temp_settings_file = data_path / "settings.json"
    version_file = data_path / "VERSION"

    saved_data = {
        "downloaders": {
            "all_debrid": {
                "enabled": True,
                "api_key": "persisted_secret_key",
            },
        },
    }
    temp_settings_file.write_text(json.dumps(saved_data))
    version_file.write_text("9.9.9")

    import program.settings.models

    monkeypatch.setenv("CINEFLOW_FORCE_ENV", "true")
    monkeypatch.setenv("CINEFLOW_DOWNLOADERS_ALL_DEBRID_API_KEY", "env_override_key")
    monkeypatch.delenv("CINEFLOW_SETTINGS_FILENAME", raising=False)
    monkeypatch.delenv("SETTINGS_FILENAME", raising=False)

    monkeypatch.setattr(program.settings, "data_dir_path", data_path)

    settings_manager = SettingsManager()
    assert (
        settings_manager.settings.downloaders.all_debrid.api_key == "env_override_key"
    )
