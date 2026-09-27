"""Tests for Trakt aliases caching, validation, and placeholder guards."""

from __future__ import annotations

from unittest.mock import MagicMock

from program.apis.trakt_api import TraktAPI
from program.settings.models import TraktModel, TraktOauthModel


def test_is_configured_rejects_placeholders_and_revoked_default():
    # Empty string
    settings = TraktModel(api_key="")
    api = TraktAPI(settings)
    assert not api.is_configured

    # Placeholder string from settings/templates
    settings = TraktModel(api_key="TRAKT_SECRET")
    api = TraktAPI(settings)
    assert not api.is_configured

    # Upstream revoked default client id
    settings = TraktModel(api_key=TraktAPI._DEFAULT_CLIENT_ID)
    api = TraktAPI(settings)
    assert not api.is_configured

    # Legitimate configured client id
    settings = TraktModel(api_key="valid_trakt_client_id_12345")
    api = TraktAPI(settings)
    assert api.is_configured


def test_is_enabled_requires_enabled_and_configured():
    # Enabled=False, configured=True -> not enabled
    settings = TraktModel(enabled=False, api_key="valid_trakt_client_id_12345")
    api = TraktAPI(settings)
    assert not api.is_enabled

    # Enabled=True, configured=False -> not enabled
    settings = TraktModel(enabled=True, api_key="TRAKT_SECRET")
    api = TraktAPI(settings)
    assert not api.is_enabled

    # Enabled=True, configured=True -> enabled
    settings = TraktModel(enabled=True, api_key="valid_trakt_client_id_12345")
    api = TraktAPI(settings)
    assert api.is_enabled


def test_get_aliases_short_circuits_when_disabled():
    settings = TraktModel(enabled=False, api_key="valid_trakt_client_id_12345")
    api = TraktAPI(settings)
    api.session.get = MagicMock()

    result = api.get_aliases("tt1286039", "shows")
    assert result == {}
    api.session.get.assert_not_called()


def test_get_aliases_short_circuits_on_empty_imdb_id():
    settings = TraktModel(enabled=True, api_key="valid_trakt_client_id_12345")
    api = TraktAPI(settings)
    api.session.get = MagicMock()

    assert api.get_aliases(None, "shows") == {}
    assert api.get_aliases("", "shows") == {}
    api.session.get.assert_not_called()


def test_get_aliases_caches_successful_result():
    settings = TraktModel(enabled=True, api_key="valid_trakt_client_id_12345")
    api = TraktAPI(settings)

    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.status_code = 200
    mock_resp.data = True
    mock_resp.json.return_value = [
        {"title": "Drive to Survive", "country": "us"},
        {"title": "Anime-Formula 1", "country": "jp"},
    ]
    api.session.get = MagicMock(return_value=mock_resp)

    # First call - fetches from network
    aliases1 = api.get_aliases("tt1286039", "shows")
    assert aliases1 == {"us": ["Drive to Survive"], "jp": ["Formula 1"]}
    assert api.session.get.call_count == 1

    # Second call for the same imdb_id - served from in-memory cache
    aliases2 = api.get_aliases("tt1286039", "shows")
    assert aliases2 == {"us": ["Drive to Survive"], "jp": ["Formula 1"]}
    assert api.session.get.call_count == 1


def test_get_aliases_handles_403_and_circuit_breaks():
    settings = TraktModel(enabled=True, api_key="bad_client_id")
    api = TraktAPI(settings)

    mock_resp = MagicMock()
    mock_resp.ok = False
    mock_resp.status_code = 403
    mock_resp.text = "Forbidden"
    api.session.get = MagicMock(return_value=mock_resp)

    # First call - hits 403, sets _auth_failed flag
    aliases1 = api.get_aliases("tt1286039", "shows")
    assert aliases1 == {}
    assert api._auth_failed is True
    assert not api.is_enabled
    assert api.session.get.call_count == 1

    # Second call for another item - short-circuits due to _auth_failed without making HTTP request
    aliases2 = api.get_aliases("tt9999999", "shows")
    assert aliases2 == {}
    assert api.session.get.call_count == 1


def test_sync_client_id_resets_auth_state_on_key_update():
    settings = TraktModel(enabled=True, api_key="bad_id")
    api = TraktAPI(settings)
    api._auth_failed = True
    api._aliases_cache[("tt1286039", "shows")] = {"us": ["Cached"]}

    # Update settings to new valid key
    settings.api_key = "new_good_id"
    api._sync_client_id()

    assert api.client_id == "new_good_id"
    assert api._auth_failed is False
    assert len(api._aliases_cache) == 0
    assert api.is_enabled is True
