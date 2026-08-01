"""Unit tests for Settings connection probes."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from program.utils import connection_tests as ct
from program.utils.connection_tests import ConnectionTestResponse


class _FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        payload: object | None = None,
        text: str = "",
    ):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _settings_ns(**kwargs: object) -> SimpleNamespace:
    return SimpleNamespace(**kwargs)


@pytest.fixture
def mock_settings(monkeypatch: pytest.MonkeyPatch):
    root = _settings_ns(
        downloaders=_settings_ns(
            proxy_url="",
            real_debrid=_settings_ns(enabled=True, api_key="rd-secret-key"),
        ),
        updaters=_settings_ns(
            plex=_settings_ns(
                enabled=True,
                token="plex-token-secret",
                url="http://plex.local:32400",
            )
        ),
        scraping=_settings_ns(
            jackett=_settings_ns(
                enabled=True,
                url="http://jackett.local:9117",
                api_key="jackett-secret",
            ),
            prowlarr=_settings_ns(
                enabled=True,
                url="http://prowlarr.local:9696",
                api_key="prowlarr-secret",
            ),
        ),
        post_processing=_settings_ns(
            subtitle=_settings_ns(
                providers=_settings_ns(
                    opensubtitles=_settings_ns(
                        enabled=True,
                        username="os-user",
                        password="os-pass-secret",
                        user_agent="VLSub 0.11.1",
                        allow_anonymous=False,
                    ),
                    subdl=_settings_ns(enabled=True, api_key="subdl-secret"),
                )
            )
        ),
    )
    monkeypatch.setattr(ct.settings_manager, "settings", root)
    return root


def test_safe_message_redacts_secret_markers():
    assert ct._safe_message("bad api_key=abc") == "Connection failed"
    assert ct._safe_message("https://host/?apikey=x") == "Connection failed"
    assert ct._safe_message("Unauthorized") == "Unauthorized"


def test_response_model_shape():
    payload = ConnectionTestResponse(ok=True, latency_ms=12, message="Connected")
    assert payload.model_dump() == {
        "ok": True,
        "latency_ms": 12,
        "message": "Connected",
    }


def test_real_debrid_missing_key(mock_settings):
    mock_settings.downloaders.real_debrid.api_key = ""
    result = ct._probe_real_debrid()
    assert result.ok is False
    assert result.message == "API key not configured"
    assert "secret" not in result.message.lower()


def test_real_debrid_ok(mock_settings):
    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False
    fake_client.get.return_value = _FakeResponse(
        200, payload={"username": "cineflow"}
    )

    with patch.object(ct.httpx, "Client", return_value=fake_client):
        result = ct._probe_real_debrid()

    assert result.ok is True
    assert result.message == "Connected as cineflow"
    assert "rd-secret" not in result.message
    call_kwargs = fake_client.get.call_args
    assert call_kwargs.args[0] == "/user"
    assert "Bearer rd-secret-key" in call_kwargs.kwargs["headers"]["Authorization"]


def test_real_debrid_unauthorized(mock_settings):
    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False
    fake_client.get.return_value = _FakeResponse(401)

    with patch.object(ct.httpx, "Client", return_value=fake_client):
        result = ct._probe_real_debrid()

    assert result.ok is False
    assert result.message == "Unauthorized"


def test_plex_ok(mock_settings):
    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False
    fake_client.get.return_value = _FakeResponse(200, payload={"MediaContainer": {}})

    with patch.object(ct.httpx, "Client", return_value=fake_client):
        result = ct._probe_plex()

    assert result.ok is True
    assert "Plex" in result.message
    assert "plex-token" not in result.message
    url = fake_client.get.call_args.args[0]
    assert url.endswith("/account")
    assert "token" not in url.lower()


def test_jackett_ok(mock_settings):
    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False
    fake_client.get.return_value = _FakeResponse(200, payload={"app_version": "0.0"})

    with patch.object(ct.httpx, "Client", return_value=fake_client):
        result = ct._probe_jackett()

    assert result.ok is True
    assert result.message == "Connected to Jackett"
    params = fake_client.get.call_args.kwargs["params"]
    assert params["apikey"] == "jackett-secret"
    assert "jackett-secret" not in result.message


def test_prowlarr_falls_back_to_system_status(mock_settings):
    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False
    fake_client.get.side_effect = [
        _FakeResponse(404),
        _FakeResponse(200, payload={"version": "1"}),
    ]

    with patch.object(ct.httpx, "Client", return_value=fake_client):
        result = ct._probe_prowlarr()

    assert result.ok is True
    assert result.message == "Connected to Prowlarr"
    assert fake_client.get.call_count == 2


def test_opensubtitles_ok(mock_settings):
    fake_server = MagicMock()
    fake_server.LogIn.return_value = {"status": "200 OK", "token": "tok"}

    with patch.object(ct, "ServerProxy", return_value=fake_server):
        result = ct._probe_opensubtitles()

    assert result.ok is True
    assert result.message == "Authenticated"
    assert "os-pass" not in result.message


def test_opensubtitles_incomplete_credentials(mock_settings):
    mock_settings.post_processing.subtitle.providers.opensubtitles.password = ""
    result = ct._probe_opensubtitles()
    assert result.ok is False
    assert result.message == "Incomplete credentials"


def test_subdl_ok(mock_settings):
    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False
    fake_client.get.return_value = _FakeResponse(200, payload={"status": True})

    with patch.object(ct.httpx, "Client", return_value=fake_client):
        result = ct._probe_subdl()

    assert result.ok is True
    assert result.message == "Connected to SubDL"
    params = fake_client.get.call_args.kwargs["params"]
    assert params["api_key"] == "subdl-secret"
    assert "subdl-secret" not in result.message


def test_subdl_auth_error_payload(mock_settings):
    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False
    fake_client.get.return_value = _FakeResponse(
        200, payload={"status": False, "error": "invalid api_key provided"}
    )

    with patch.object(ct.httpx, "Client", return_value=fake_client):
        result = ct._probe_subdl()

    assert result.ok is False
    # Secret marker in upstream error must be scrubbed.
    assert result.message == "Connection failed"
    assert "api_key" not in result.message.lower()


def test_run_connection_test_timeout(mock_settings):
    def slow() -> ConnectionTestResponse:
        import time

        time.sleep(10)
        return ConnectionTestResponse(ok=True, latency_ms=1, message="late")

    with (
        patch.dict(ct._PROBES, {"real_debrid": slow}),
        patch.object(ct, "PROBE_TIMEOUT_SECONDS", 0.05),
    ):
        result = ct.run_connection_test("real_debrid")

    assert result.ok is False
    assert result.message == "Timed out"


def test_http_timeout_maps_to_message(mock_settings):
    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False
    fake_client.get.side_effect = httpx.ReadTimeout("read timed out")

    with patch.object(ct.httpx, "Client", return_value=fake_client):
        result = ct._probe_jackett()

    assert result.ok is False
    assert result.message == "Timed out"
