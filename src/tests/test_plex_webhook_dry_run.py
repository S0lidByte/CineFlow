"""Plex webhook dry-run: secret gate + GUID sanitization."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from program.utils.plex_webhook import sanitize_plex_guids
from routers.secure.webhooks import verify_plex_webhook_secret


def _request_with(
    *,
    headers: dict[str, str] | None = None,
    query_string: bytes = b"",
) -> Request:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/webhook/plex",
        "raw_path": b"/api/v1/webhook/plex",
        "query_string": query_string,
        "headers": [
            (k.lower().encode("latin-1"), v.encode("latin-1"))
            for k, v in (headers or {}).items()
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
    }
    return Request(scope)


def test_sanitize_plex_guids_keeps_provider_ids_only():
    metadata = {
        "type": "movie",
        "guid": "plex://movie/5d776b9eadsomeid",
        "Guid": [
            {"id": "imdb://tt0111161"},
            {"id": "tmdb://278"},
            {"id": "tvdb://123"},
            {"id": "plex://movie/ignored"},
            {"id": "IMDB://tt009"},
        ],
    }
    assert sanitize_plex_guids(metadata) == [
        "imdb://tt009",
        "imdb://tt0111161",
        "tmdb://278",
        "tvdb://123",
    ]


def test_sanitize_plex_guids_handles_empty_and_string_guid():
    assert sanitize_plex_guids(None) == []
    assert sanitize_plex_guids({}) == []
    assert sanitize_plex_guids({"guid": "tmdb://42"}) == ["tmdb://42"]
    assert sanitize_plex_guids({"guid": "plex://movie/x"}) == []


def test_plex_webhook_secret_optional_when_unset(monkeypatch):
    monkeypatch.setattr(
        "routers.secure.webhooks.settings_manager",
        MagicMock(
            settings=MagicMock(
                content=MagicMock(plex_webhook=MagicMock(webhook_secret=""))
            )
        ),
    )
    verify_plex_webhook_secret(_request_with())


def test_plex_webhook_secret_accepts_header_or_query(monkeypatch):
    monkeypatch.setattr(
        "routers.secure.webhooks.settings_manager",
        MagicMock(
            settings=MagicMock(
                content=MagicMock(
                    plex_webhook=MagicMock(webhook_secret="plex-secret")
                )
            )
        ),
    )

    with pytest.raises(HTTPException) as exc:
        verify_plex_webhook_secret(_request_with())
    assert exc.value.status_code == 401

    verify_plex_webhook_secret(
        _request_with(headers={"x-webhook-secret": "plex-secret"})
    )
    verify_plex_webhook_secret(
        _request_with(query_string=b"webhook_secret=plex-secret")
    )

    with pytest.raises(HTTPException):
        verify_plex_webhook_secret(
            _request_with(headers={"x-webhook-secret": "wrong"})
        )
