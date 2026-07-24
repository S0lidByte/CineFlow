"""Phase 1 security: auth compare_digest, CORS config, DB reset gate."""

from __future__ import annotations

import hmac
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import auth
from program.db.db import db_reset_allowed, reset_database
from program.utils.cors import build_cors_config


def test_api_key_matches_uses_compare_digest(monkeypatch):
    monkeypatch.setattr(
        auth.settings_manager,
        "settings",
        MagicMock(api_key="a" * 32),
    )
    assert auth.api_key_matches("a" * 32) is True
    assert auth.api_key_matches("b" * 32) is False
    assert auth.api_key_matches(None) is False
    assert auth.api_key_matches("") is False


def test_api_key_matches_rejects_wrong_length(monkeypatch):
    monkeypatch.setattr(
        auth.settings_manager,
        "settings",
        MagicMock(api_key="a" * 32),
    )
    assert auth.api_key_matches("short") is False


def test_resolve_api_key_accepts_header_or_bearer_only():
    auth.resolve_api_key(header=True, bearer=False)
    auth.resolve_api_key(header=False, bearer=True)
    with pytest.raises(HTTPException) as exc:
        auth.resolve_api_key(header=False, bearer=False)
    assert exc.value.status_code == 401


def test_resolve_webhook_api_key_allows_query():
    auth.resolve_webhook_api_key(header=False, bearer=False, query=True)
    with pytest.raises(HTTPException):
        auth.resolve_webhook_api_key(header=False, bearer=False, query=False)


def test_resolve_ws_api_key_uses_query(monkeypatch):
    monkeypatch.setattr(
        auth.settings_manager,
        "settings",
        MagicMock(api_key="w" * 32),
    )
    auth.resolve_ws_api_key(api_key="w" * 32)
    with pytest.raises(HTTPException):
        auth.resolve_ws_api_key(api_key="x" * 32)


def test_build_cors_config_wildcard_disables_credentials():
    cfg = build_cors_config(["*"])
    assert cfg["allow_origins"] == ["*"]
    assert cfg["allow_credentials"] is False


def test_build_cors_config_empty_is_wildcard():
    cfg = build_cors_config([])
    assert cfg["allow_origins"] == ["*"]
    assert cfg["allow_credentials"] is False


def test_build_cors_config_explicit_origins_keeps_credentials():
    cfg = build_cors_config(["http://localhost:3000", "https://app.example"])
    assert cfg["allow_origins"] == [
        "http://localhost:3000",
        "https://app.example",
    ]
    assert cfg["allow_credentials"] is True


def test_db_reset_allowed_env(monkeypatch):
    monkeypatch.delenv("RIVEN_ALLOW_DB_RESET", raising=False)
    assert db_reset_allowed() is False
    monkeypatch.setenv("RIVEN_ALLOW_DB_RESET", "1")
    assert db_reset_allowed() is True
    monkeypatch.setenv("RIVEN_ALLOW_DB_RESET", "true")
    assert db_reset_allowed() is True
    monkeypatch.setenv("RIVEN_ALLOW_DB_RESET", "false")
    assert db_reset_allowed() is False


def test_reset_database_blocked_without_flag(monkeypatch):
    monkeypatch.delenv("RIVEN_ALLOW_DB_RESET", raising=False)
    assert reset_database() is False


def test_hmac_compare_digest_available():
    assert hmac.compare_digest("same", "same") is True
    assert hmac.compare_digest("same", "diff") is False
