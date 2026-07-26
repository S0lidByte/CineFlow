"""Smoke checks for Phase 2.C dependency bumps (apprise / fastapi)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import fastapi
from apprise import Apprise
from fastapi import FastAPI
from fastapi.testclient import TestClient

from program.settings.models import NotificationsModel


def test_apprise_version_and_json_url_smoke() -> None:
    """Apprise 1.12.x loads and accepts a local json:// target without network."""
    import apprise

    version = getattr(apprise, "__version__", "0.0.0")
    major, minor, *_ = (int(x) for x in version.split(".")[:3])
    assert (major, minor) >= (1, 12)

    app = Apprise()
    assert app.add("json://localhost/notify")
    assert len(app) == 1
    # notify may fail to connect; API must still be callable without raising TypeError
    result = app.notify(title="cineflow-smoke", body="phase-2c")
    assert result is False or result is True


def test_fastapi_testclient_smoke() -> None:
    app = FastAPI()

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert fastapi.__version__.startswith("0.140")


def test_notification_service_apprise_init_and_notify() -> None:
    settings = NotificationsModel(
        enabled=True,
        on_item_type=["movie"],
        service_urls=["json://localhost/notify"],
    )

    with patch(
        "program.services.notifications.settings_manager"
    ) as mock_settings_manager:
        mock_settings_manager.settings.notifications = settings
        from program.services.notifications import NotificationService

        service = NotificationService()
        assert service.initialized
        assert len(service.apprise) == 1

        service.apprise.notify = MagicMock(return_value=True)
        service._notify_generic("Smoke", "Phase 2.C")
        service.apprise.notify.assert_called_once_with(title="Smoke", body="Phase 2.C")
