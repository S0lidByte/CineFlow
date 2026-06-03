import json

import pytest

from program.settings import SettingsManager
from program.settings.models import AppModel, OpenSubtitlesProviderConfig


def test_opensubtitles_provider_defaults_to_allow_anonymous():
    config = dict(
        post_processing=dict(
            subtitle=dict(
                providers=dict(
                    opensubtitles=dict(
                        enabled=True,
                    )
                )
            )
        )
    )
    validated = AppModel.model_validate(config)

    assert validated.post_processing.subtitle.providers.opensubtitles.allow_anonymous is True


def test_opensubtitles_provider_requires_credentials_without_anonymous():
    config = dict(
        post_processing=dict(
            subtitle=dict(
                providers=dict(
                    opensubtitles=dict(
                        enabled=True,
                        allow_anonymous=False,
                    )
                )
            )
        )
    )

    with pytest.raises(ValueError):
        AppModel.model_validate(config)


def test_opensubtitles_provider_model_defaults_to_allow_anonymous():
    validated = OpenSubtitlesProviderConfig(enabled=True)
    assert validated.allow_anonymous is True


def test_settings_manager_recovers_missing_open_subtitles_credentials(monkeypatch, tmp_path):
    settings_data = {
        "post_processing": {
            "subtitle": {
                "providers": {
                    "opensubtitles": {
                        "enabled": True,
                        "allow_anonymous": False,
                        "username": "",
                        "password": "",
                    }
                }
            }
        }
    }

    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps(settings_data), encoding="utf-8")

    monkeypatch.setenv("SETTINGS_FILENAME", "settings.json")
    monkeypatch.setattr("program.settings.data_dir_path", tmp_path)
    settings_manager = SettingsManager()

    provider = settings_manager.settings.post_processing.subtitle.providers.opensubtitles

    assert provider.enabled is True
    assert provider.allow_anonymous is True
