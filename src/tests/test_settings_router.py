from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from program.settings import settings_manager
from program.settings.models import AppModel
from program.settings.mutation import API_KEY_SENTINEL
from program.settings.secrets import SETTINGS_SECRET_SENTINEL
from routers.secure.settings import router as settings_router

app = FastAPI()
app.include_router(settings_router, prefix="/api/v1")

client = TestClient(app)


@pytest.fixture
def mock_settings():
    original_settings = settings_manager.settings

    # Create a fresh AppModel for testing
    test_settings = AppModel(api_key="REAL_SECRET")
    test_settings.content.trakt.api_key = "TRAKT_PUBLIC_CLIENT_ID"
    test_settings.downloaders.real_debrid.api_key = "RD_PRIVATE_TOKEN"
    test_settings.updaters.plex.token = "PLEX_PRIVATE_TOKEN"
    test_settings.content.plex_webhook.webhook_secret = "PLEX_WEBHOOK_SECRET"
    test_settings.content.trakt.oauth.oauth_client_secret = "TRAKT_PRIVATE_SECRET"
    test_settings.content.trakt.oauth.access_token = "TRAKT_ACCESS_TOKEN"
    test_settings.content.trakt.oauth.refresh_token = "TRAKT_REFRESH_TOKEN"
    test_settings.updaters.plex.url = (
        "http://plex-user:plex-password@plex:32400/?api_key=plex-query-token"
    )

    with patch.object(settings_manager, "settings", test_settings):
        yield test_settings


def test_get_all_settings_masks_root_api_key(mock_settings):
    response = client.get(
        "/api/v1/settings/get/all", headers={"x-api-key": "REAL_SECRET"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["api_key"] == API_KEY_SENTINEL
    assert data["content"]["trakt"]["api_key"] == "TRAKT_PUBLIC_CLIENT_ID"
    assert data["downloaders"]["real_debrid"]["api_key"] == SETTINGS_SECRET_SENTINEL
    assert data["updaters"]["plex"]["token"] == SETTINGS_SECRET_SENTINEL
    assert data["content"]["plex_webhook"]["webhook_secret"] == SETTINGS_SECRET_SENTINEL
    assert (
        data["content"]["trakt"]["oauth"]["oauth_client_secret"]
        == SETTINGS_SECRET_SENTINEL
    )
    assert data["content"]["trakt"]["oauth"]["access_token"] == SETTINGS_SECRET_SENTINEL
    assert (
        data["content"]["trakt"]["oauth"]["refresh_token"] == SETTINGS_SECRET_SENTINEL
    )
    redacted_url = data["updaters"]["plex"]["url"]
    assert "plex-password" not in redacted_url
    assert "plex-query-token" not in redacted_url
    assert "redacted" in redacted_url


def test_get_settings_masks_root_api_key(mock_settings):
    response = client.get(
        "/api/v1/settings/get/api_key,content.trakt.api_key",
        headers={"x-api-key": "REAL_SECRET"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["api_key"] == API_KEY_SENTINEL
    assert data["content.trakt.api_key"] == "TRAKT_PUBLIC_CLIENT_ID"

    nested = client.get(
        "/api/v1/settings/get/updaters.plex.token,content.plex_webhook.webhook_secret",
        headers={"x-api-key": "REAL_SECRET"},
    )
    assert nested.status_code == 200
    assert nested.json() == {
        "updaters.plex.token": SETTINGS_SECRET_SENTINEL,
        "content.plex_webhook.webhook_secret": SETTINGS_SECRET_SENTINEL,
    }


def test_set_all_settings_preserves_sentinel(mock_settings):
    # Get current settings (masked)
    response = client.get(
        "/api/v1/settings/get/all", headers={"x-api-key": "REAL_SECRET"}
    )
    data = response.json()

    # Post them back
    response = client.post(
        "/api/v1/settings/set/all", json=data, headers={"x-api-key": "REAL_SECRET"}
    )
    assert response.status_code == 200

    # Verify root and nested integration secrets survive the masked round-trip.
    assert settings_manager.settings.api_key == "REAL_SECRET"
    assert (
        settings_manager.settings.downloaders.real_debrid.api_key == "RD_PRIVATE_TOKEN"
    )
    assert settings_manager.settings.updaters.plex.token == "PLEX_PRIVATE_TOKEN"
    assert (
        settings_manager.settings.content.plex_webhook.webhook_secret
        == "PLEX_WEBHOOK_SECRET"
    )
    assert (
        settings_manager.settings.content.trakt.oauth.oauth_client_secret
        == "TRAKT_PRIVATE_SECRET"
    )
    assert (
        settings_manager.settings.content.trakt.oauth.access_token
        == "TRAKT_ACCESS_TOKEN"
    )
    assert (
        settings_manager.settings.content.trakt.oauth.refresh_token
        == "TRAKT_REFRESH_TOKEN"
    )
    assert (
        settings_manager.settings.updaters.plex.url
        == "http://plex-user:plex-password@plex:32400/?api_key=plex-query-token"
    )


def test_set_all_settings_updates_real_key(mock_settings):
    # Get current settings (masked)
    response = client.get(
        "/api/v1/settings/get/all", headers={"x-api-key": "REAL_SECRET"}
    )
    data = response.json()

    # Change the api key
    data["api_key"] = "NEW_SECRET_32_CHARS_LONG_FOR_TESTING"

    # Post them back
    response = client.post(
        "/api/v1/settings/set/all", json=data, headers={"x-api-key": "REAL_SECRET"}
    )
    assert response.status_code == 200

    # Verify the real secret is updated
    assert settings_manager.settings.api_key == "NEW_SECRET_32_CHARS_LONG_FOR_TESTING"


def test_set_settings_preserves_sentinel(mock_settings):
    response = client.post(
        "/api/v1/settings/set/api_key",
        json={"api_key": API_KEY_SENTINEL},
        headers={"x-api-key": "REAL_SECRET"},
    )
    assert response.status_code == 200

    # Verify the real secret is preserved
    assert settings_manager.settings.api_key == "REAL_SECRET"


def test_set_settings_preserves_masked_plex_secrets(mock_settings):
    response = client.post(
        "/api/v1/settings/set/updaters.plex.token,content.plex_webhook.webhook_secret",
        json={
            "updaters.plex.token": SETTINGS_SECRET_SENTINEL,
            "content.plex_webhook.webhook_secret": SETTINGS_SECRET_SENTINEL,
        },
        headers={"x-api-key": "REAL_SECRET"},
    )
    assert response.status_code == 200
    assert settings_manager.settings.updaters.plex.token == "PLEX_PRIVATE_TOKEN"
    assert (
        settings_manager.settings.content.plex_webhook.webhook_secret
        == "PLEX_WEBHOOK_SECRET"
    )


def test_set_settings_updates_real_key(mock_settings):
    response = client.post(
        "/api/v1/settings/set/api_key",
        json={"api_key": "NEW_SECRET_32_CHARS_LONG_FOR_TESTING"},
        headers={"x-api-key": "REAL_SECRET"},
    )
    assert response.status_code == 200

    # Verify the real secret is updated
    assert settings_manager.settings.api_key == "NEW_SECRET_32_CHARS_LONG_FOR_TESTING"


def test_set_settings_invalid_key_fails(mock_settings):
    response = client.post(
        "/api/v1/settings/set/api_key",
        json={"api_key": "   "},
        headers={"x-api-key": "REAL_SECRET"},
    )
    assert response.status_code == 400

    # Verify the real secret is preserved
    assert settings_manager.settings.api_key == "REAL_SECRET"
