"""Phase 3: Retry-After cap and Overseerr webhook secret."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from program.utils.request import MAX_RETRY_AFTER_SECONDS, SmartSession
from routers.secure.webhooks import verify_overseerr_webhook_secret


def _request_with_headers(headers: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/webhook/overseerr",
        "raw_path": b"/api/v1/webhook/overseerr",
        "query_string": b"",
        "headers": [
            (k.lower().encode("latin-1"), v.encode("latin-1"))
            for k, v in headers.items()
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
    }
    return Request(scope)


def test_retry_after_seconds_are_capped():
    session = SmartSession(retries=1)
    response = httpx.Response(429, headers={"Retry-After": "999"})
    delay = session._compute_retry_delay(response, attempt=1)
    assert delay == MAX_RETRY_AFTER_SECONDS


def test_retry_after_below_cap_unchanged():
    session = SmartSession(retries=1)
    response = httpx.Response(429, headers={"Retry-After": "2"})
    delay = session._compute_retry_delay(response, attempt=1)
    assert delay == 2.0


def test_webhook_secret_optional_when_unset(monkeypatch):
    monkeypatch.setattr(
        "routers.secure.webhooks.settings_manager",
        MagicMock(
            settings=MagicMock(
                content=MagicMock(overseerr=MagicMock(webhook_secret=""))
            )
        ),
    )
    verify_overseerr_webhook_secret(_request_with_headers({}))


def test_webhook_secret_required_when_configured(monkeypatch):
    monkeypatch.setattr(
        "routers.secure.webhooks.settings_manager",
        MagicMock(
            settings=MagicMock(
                content=MagicMock(
                    overseerr=MagicMock(webhook_secret="super-secret")
                )
            )
        ),
    )
    with pytest.raises(HTTPException) as exc:
        verify_overseerr_webhook_secret(_request_with_headers({}))
    assert exc.value.status_code == 401

    verify_overseerr_webhook_secret(
        _request_with_headers({"x-webhook-secret": "super-secret"})
    )

    with pytest.raises(HTTPException):
        verify_overseerr_webhook_secret(
            _request_with_headers({"x-webhook-secret": "wrong"})
        )
