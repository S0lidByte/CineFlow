import json
import os
from unittest.mock import patch

from auth import _bff_api_key_matches
from program.apis.trakt_api import TraktAPI
from program.db.db import db_reset_allowed
from program.settings import (
    SettingsManager,
    _resolve_settings_filename,
    is_force_env_enabled,
)
from program.settings.models import AppModel, TraktModel, TraktOauthModel
from program.utils import generate_api_key


def test_resolve_settings_filename_precedence(tmp_path, monkeypatch):
    """Test resolution order: CINEFLOW_SETTINGS_FILENAME > SETTINGS_FILENAME > cineflow.json > settings.json."""
    import program.settings

    monkeypatch.setattr(program.settings, "data_dir_path", tmp_path)

    # 1. Default when no env and no files
    monkeypatch.delenv("CINEFLOW_SETTINGS_FILENAME", raising=False)
    monkeypatch.delenv("SETTINGS_FILENAME", raising=False)
    assert _resolve_settings_filename() == "settings.json"

    # 2. settings.json exists on disk
    (tmp_path / "settings.json").write_text("{}")
    assert _resolve_settings_filename() == "settings.json"

    # 3. cineflow.json exists on disk -> takes precedence over settings.json
    (tmp_path / "cineflow.json").write_text("{}")
    assert _resolve_settings_filename() == "cineflow.json"

    # 4. SETTINGS_FILENAME env var -> overrides disk files
    monkeypatch.setenv("SETTINGS_FILENAME", "custom_legacy.json")
    assert _resolve_settings_filename() == "custom_legacy.json"

    # 5. CINEFLOW_SETTINGS_FILENAME env var -> overrides SETTINGS_FILENAME
    monkeypatch.setenv("CINEFLOW_SETTINGS_FILENAME", "custom_cineflow.json")
    assert _resolve_settings_filename() == "custom_cineflow.json"


def test_is_force_env_enabled(monkeypatch):
    """Test force env precedence: CINEFLOW_FORCE_ENV > RIVEN_FORCE_ENV."""
    monkeypatch.delenv("CINEFLOW_FORCE_ENV", raising=False)
    monkeypatch.delenv("RIVEN_FORCE_ENV", raising=False)
    assert is_force_env_enabled() is False

    # RIVEN_FORCE_ENV fallback
    monkeypatch.setenv("RIVEN_FORCE_ENV", "true")
    assert is_force_env_enabled() is True

    # CINEFLOW_FORCE_ENV overrides RIVEN_FORCE_ENV
    monkeypatch.setenv("CINEFLOW_FORCE_ENV", "false")
    assert is_force_env_enabled() is False

    monkeypatch.setenv("CINEFLOW_FORCE_ENV", "true")
    monkeypatch.setenv("RIVEN_FORCE_ENV", "false")
    assert is_force_env_enabled() is True


def test_check_environment_dual_prefix_precedence(tmp_path, monkeypatch):
    """Test environment variable overriding with CINEFLOW primary and RIVEN fallback."""
    import program.settings

    monkeypatch.setattr(program.settings, "data_dir_path", tmp_path)
    monkeypatch.delenv("CINEFLOW_SETTINGS_FILENAME", raising=False)
    monkeypatch.delenv("SETTINGS_FILENAME", raising=False)

    sm = SettingsManager.__new__(SettingsManager)

    base_settings = {
        "debug": False,
        "downloaders": {
            "proxy_url": "",
            "real_debrid": {
                "enabled": False,
                "api_key": "",
            },
            "torbox": {
                "enabled": False,
                "api_key": "",
            },
        },
    }

    # Case A: Only RIVEN_* set
    monkeypatch.setenv("RIVEN_DEBUG", "true")
    monkeypatch.setenv("RIVEN_DOWNLOADERS_REAL_DEBRID_API_KEY", "riven_rd_key")
    checked = sm.check_environment(
        base_settings, prefix="CINEFLOW", fallback_prefix="RIVEN"
    )
    assert checked["debug"] is True
    assert checked["downloaders"]["real_debrid"]["api_key"] == "riven_rd_key"

    # Case B: Both CINEFLOW_* and RIVEN_* set -> CINEFLOW_* takes precedence
    monkeypatch.setenv("CINEFLOW_DEBUG", "false")
    monkeypatch.setenv("CINEFLOW_DOWNLOADERS_REAL_DEBRID_API_KEY", "cineflow_rd_key")
    checked = sm.check_environment(
        base_settings, prefix="CINEFLOW", fallback_prefix="RIVEN"
    )
    assert checked["debug"] is False
    assert checked["downloaders"]["real_debrid"]["api_key"] == "cineflow_rd_key"


def test_settings_manager_load_with_cineflow_force_env(tmp_path, monkeypatch):
    """Test SettingsManager loading from file with CINEFLOW_FORCE_ENV overriding persisted values."""
    import program.settings

    monkeypatch.setattr(program.settings, "data_dir_path", tmp_path)

    persisted = AppModel().model_dump(mode="json")
    persisted["downloaders"]["real_debrid"]["enabled"] = False
    persisted["downloaders"]["real_debrid"]["api_key"] = "saved_key"

    (tmp_path / "cineflow.json").write_text(json.dumps(persisted))
    monkeypatch.delenv("CINEFLOW_SETTINGS_FILENAME", raising=False)
    monkeypatch.delenv("SETTINGS_FILENAME", raising=False)

    # Force env active with CINEFLOW_*
    monkeypatch.setenv("CINEFLOW_FORCE_ENV", "true")
    monkeypatch.setenv("CINEFLOW_DOWNLOADERS_REAL_DEBRID_ENABLED", "true")
    monkeypatch.setenv("CINEFLOW_DOWNLOADERS_REAL_DEBRID_API_KEY", "overridden_key")

    sm = SettingsManager()
    assert sm.filename == "cineflow.json"
    assert sm.settings.downloaders.real_debrid.enabled is True
    assert sm.settings.downloaders.real_debrid.api_key == "overridden_key"


def test_db_reset_allowed_precedence(monkeypatch):
    """Test CINEFLOW_ALLOW_DB_RESET > RIVEN_ALLOW_DB_RESET."""
    monkeypatch.delenv("CINEFLOW_ALLOW_DB_RESET", raising=False)
    monkeypatch.delenv("RIVEN_ALLOW_DB_RESET", raising=False)
    assert db_reset_allowed() is False

    # Fallback to RIVEN_ALLOW_DB_RESET
    monkeypatch.setenv("RIVEN_ALLOW_DB_RESET", "1")
    assert db_reset_allowed() is True

    # CINEFLOW_ALLOW_DB_RESET takes precedence
    monkeypatch.setenv("CINEFLOW_ALLOW_DB_RESET", "0")
    assert db_reset_allowed() is False

    monkeypatch.setenv("CINEFLOW_ALLOW_DB_RESET", "true")
    assert db_reset_allowed() is True


def test_bff_api_key_matches_precedence(monkeypatch):
    """Test CINEFLOW_BFF_API_KEY > BFF_API_KEY > RIVEN_BFF_API_KEY."""
    monkeypatch.delenv("CINEFLOW_BFF_API_KEY", raising=False)
    monkeypatch.delenv("BFF_API_KEY", raising=False)
    monkeypatch.delenv("RIVEN_BFF_API_KEY", raising=False)

    assert _bff_api_key_matches("some_key") is False

    # RIVEN_BFF_API_KEY fallback
    monkeypatch.setenv("RIVEN_BFF_API_KEY", "legacy_secret")
    assert _bff_api_key_matches("legacy_secret") is True

    # BFF_API_KEY overrides RIVEN_BFF_API_KEY
    monkeypatch.setenv("BFF_API_KEY", "bff_secret")
    assert _bff_api_key_matches("bff_secret") is True
    assert _bff_api_key_matches("legacy_secret") is False

    # CINEFLOW_BFF_API_KEY overrides BFF_API_KEY
    monkeypatch.setenv("CINEFLOW_BFF_API_KEY", "cineflow_secret")
    assert _bff_api_key_matches("cineflow_secret") is True
    assert _bff_api_key_matches("bff_secret") is False


def test_generate_api_key_precedence(monkeypatch):
    """Test CINEFLOW_API_KEY > API_KEY > RIVEN_API_KEY."""
    monkeypatch.delenv("CINEFLOW_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.delenv("RIVEN_API_KEY", raising=False)

    # 1. RIVEN fallback (exact 32 chars)
    monkeypatch.setenv("RIVEN_API_KEY", "mock_riven_api_key_32_chars_1234")
    assert generate_api_key() == "mock_riven_api_key_32_chars_1234"

    # 2. Standard API_KEY overrides RIVEN (exact 32 chars)
    monkeypatch.setenv("API_KEY", "mock_standard_api_key_32_chars12")
    assert generate_api_key() == "mock_standard_api_key_32_chars12"

    # 3. CINEFLOW_API_KEY overrides API_KEY (exact 32 chars)
    monkeypatch.setenv("CINEFLOW_API_KEY", "mock_cineflow_api_key_32_chars12")
    assert generate_api_key() == "mock_cineflow_api_key_32_chars12"


def test_trakt_api_client_id_precedence(monkeypatch):
    """Test Trakt Client ID resolution with CINEFLOW_TRAKT_API_CLIENT_ID > TRAKT_API_CLIENT_ID > RIVEN."""
    settings = TraktModel(
        api_key="",
        oauth=TraktOauthModel(oauth_client_id=""),
    )

    monkeypatch.delenv("CINEFLOW_TRAKT_API_CLIENT_ID", raising=False)
    monkeypatch.delenv("TRAKT_API_CLIENT_ID", raising=False)
    monkeypatch.delenv("RIVEN_TRAKT_API_CLIENT_ID", raising=False)

    # Fallback to baked-in default
    assert TraktAPI.resolve_client_id(settings) == TraktAPI._DEFAULT_CLIENT_ID

    # RIVEN_TRAKT_API_CLIENT_ID fallback
    monkeypatch.setenv("RIVEN_TRAKT_API_CLIENT_ID", "riven_client_id")
    assert TraktAPI.resolve_client_id(settings) == "riven_client_id"

    # TRAKT_API_CLIENT_ID overrides RIVEN
    monkeypatch.setenv("TRAKT_API_CLIENT_ID", "trakt_client_id")
    assert TraktAPI.resolve_client_id(settings) == "trakt_client_id"

    # CINEFLOW_TRAKT_API_CLIENT_ID overrides TRAKT_API_CLIENT_ID
    monkeypatch.setenv("CINEFLOW_TRAKT_API_CLIENT_ID", "cineflow_client_id")
    assert TraktAPI.resolve_client_id(settings) == "cineflow_client_id"
