import json
import os
from pathlib import Path

from program.settings import SettingsManager

TEST_VERSION = "1.28.3"
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


def test_load_and_migrate_settings(tmp_path):
    data_path = tmp_path / "data"
    data_path.mkdir()
    temp_settings_file = data_path / "settings.json"
    version_file = data_path / "VERSION"

    temp_settings_file.write_text(json.dumps(old_settings_data))
    version_file.write_text("9.9.9")

    import program.settings.models

    program.settings.data_dir_path = data_path
    program.settings.models.version_file_path = version_file
    settings_manager = SettingsManager()

    assert settings_manager.settings.downloaders.real_debrid.enabled is False
    assert settings_manager.settings.downloaders.all_debrid.enabled is True
    assert settings_manager.settings.downloaders.all_debrid.api_key == "12345678"
    assert (
        settings_manager.settings.downloaders.proxy_url
        == "https://no_proxy.com"
    )
    assert settings_manager.settings.version == TEST_VERSION
