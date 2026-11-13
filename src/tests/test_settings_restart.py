"""Tests for server restart endpoints and scheduling."""

import asyncio
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from program.utils.restart import schedule_backend_restart
from routers.secure.default import router as default_router
from routers.secure.settings import router as settings_router

app = FastAPI()
app.include_router(default_router, prefix="/api/v1")
app.include_router(settings_router, prefix="/api/v1")


@pytest.fixture
def client():
    return TestClient(app)


def test_system_restart_endpoint(client):
    """Verify POST /api/v1/system/restart initiates restart and returns 200 OK."""
    with patch("program.utils.restart.schedule_backend_restart") as mock_schedule:
        response = client.post(
            "/api/v1/system/restart",
            headers={"x-api-key": "NEW_SECRET_32_CHARS_LONG_FOR_TESTING"},
        )
        assert response.status_code == 200
        assert response.json() == {"message": "Backend restart initiated."}
        mock_schedule.assert_called_once_with(delay_seconds=0.5)


def test_settings_restart_endpoint(client):
    """Verify POST /api/v1/settings/restart initiates restart and returns 200 OK."""
    with patch("program.utils.restart.schedule_backend_restart") as mock_schedule:
        response = client.post(
            "/api/v1/settings/restart",
            headers={"x-api-key": "NEW_SECRET_32_CHARS_LONG_FOR_TESTING"},
        )
        assert response.status_code == 200
        assert response.json() == {"message": "Backend restart initiated."}
        mock_schedule.assert_called_once_with(delay_seconds=0.5)


@pytest.mark.asyncio
async def test_schedule_backend_restart_in_pytest_safe():
    """Verify schedule_backend_restart runs safely in test environment without exiting."""
    # Should schedule task and complete without os.kill/os._exit in pytest
    schedule_backend_restart(delay_seconds=0.01)
    await asyncio.sleep(0.05)
