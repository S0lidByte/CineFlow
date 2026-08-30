import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import patch

from program.settings import settings_manager
from program.settings.models import AppModel
from routers.secure.settings import router as settings_router, API_KEY_SENTINEL

app = FastAPI()
app.include_router(settings_router, prefix="/api/v1")

client = TestClient(app)

@pytest.fixture
def mock_settings():
    original_settings = settings_manager.settings
    
    # Create a fresh AppModel for testing
    test_settings = AppModel(api_key="REAL_SECRET")
    test_settings.content.trakt.api_key = "TRAKT_SECRET"
    
    with patch.object(settings_manager, "settings", test_settings):
        yield test_settings

def test_get_all_settings_masks_root_api_key(mock_settings):
    response = client.get("/api/v1/settings/get/all", headers={"x-api-key": "REAL_SECRET"})
    assert response.status_code == 200
    data = response.json()
    assert data["api_key"] == API_KEY_SENTINEL
    assert data["content"]["trakt"]["api_key"] == "TRAKT_SECRET"

def test_get_settings_masks_root_api_key(mock_settings):
    response = client.get("/api/v1/settings/get/api_key,content.trakt.api_key", headers={"x-api-key": "REAL_SECRET"})
    assert response.status_code == 200
    data = response.json()
    assert data["api_key"] == API_KEY_SENTINEL
    assert data["content.trakt.api_key"] == "TRAKT_SECRET"

def test_set_all_settings_preserves_sentinel(mock_settings):
    # Get current settings (masked)
    response = client.get("/api/v1/settings/get/all", headers={"x-api-key": "REAL_SECRET"})
    data = response.json()
    
    # Post them back
    response = client.post("/api/v1/settings/set/all", json=data, headers={"x-api-key": "REAL_SECRET"})
    assert response.status_code == 200
    
    # Verify the real secret is preserved
    assert settings_manager.settings.api_key == "REAL_SECRET"

def test_set_all_settings_updates_real_key(mock_settings):
    # Get current settings (masked)
    response = client.get("/api/v1/settings/get/all", headers={"x-api-key": "REAL_SECRET"})
    data = response.json()
    
    # Change the api key
    data["api_key"] = "NEW_SECRET_32_CHARS_LONG_FOR_TESTING"
    
    # Post them back
    response = client.post("/api/v1/settings/set/all", json=data, headers={"x-api-key": "REAL_SECRET"})
    assert response.status_code == 200
    
    # Verify the real secret is updated
    assert settings_manager.settings.api_key == "NEW_SECRET_32_CHARS_LONG_FOR_TESTING"

def test_set_settings_preserves_sentinel(mock_settings):
    response = client.post(
        "/api/v1/settings/set/api_key", 
        json={"api_key": API_KEY_SENTINEL}, 
        headers={"x-api-key": "REAL_SECRET"}
    )
    assert response.status_code == 200
    
    # Verify the real secret is preserved
    assert settings_manager.settings.api_key == "REAL_SECRET"

def test_set_settings_updates_real_key(mock_settings):
    response = client.post(
        "/api/v1/settings/set/api_key", 
        json={"api_key": "NEW_SECRET_32_CHARS_LONG_FOR_TESTING"}, 
        headers={"x-api-key": "REAL_SECRET"}
    )
    assert response.status_code == 200
    
    # Verify the real secret is updated
    assert settings_manager.settings.api_key == "NEW_SECRET_32_CHARS_LONG_FOR_TESTING"

def test_set_settings_invalid_key_fails(mock_settings):
    response = client.post(
        "/api/v1/settings/set/api_key", 
        json={"api_key": "   "}, 
        headers={"x-api-key": "REAL_SECRET"}
    )
    assert response.status_code == 400
    
    # Verify the real secret is preserved
    assert settings_manager.settings.api_key == "REAL_SECRET"
