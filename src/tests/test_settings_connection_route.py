"""Production route regression tests for Settings connection probes."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import app_router

TEST_BFF_API_KEY = "test-bff-service-key"
TEST_ACTOR_SECRET = "test-actor-context-secret"


def _signed_bff_headers(roles: str) -> dict[str, str]:
    actor_id = "settings-user"
    actor_client = "cineflow-web-bff"
    timestamp = str(int(time.time()))
    payload = json.dumps(
        {
            "actor_id": actor_id,
            "actor_roles": roles,
            "actor_client": actor_client,
            "actor_timestamp": timestamp,
        },
        separators=(",", ":"),
    ).encode()
    signature = hmac.new(
        TEST_ACTOR_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    return {
        "x-api-key": TEST_BFF_API_KEY,
        "x-actor-id": actor_id,
        "x-actor-roles": roles,
        "x-actor-client": actor_client,
        "x-actor-timestamp": timestamp,
        "x-actor-signature": signature,
    }


@pytest.fixture
def production_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("BFF_API_KEY", TEST_BFF_API_KEY)
    monkeypatch.setenv("ACTOR_CONTEXT_SECRET", TEST_ACTOR_SECRET)
    app = FastAPI()
    app.include_router(app_router)
    return TestClient(app)


def test_settings_connection_uses_production_role_guard_and_thread_dispatch(
    monkeypatch: pytest.MonkeyPatch, production_client: TestClient
) -> None:
    """Only settings writers can invoke the production-mounted sync probe route."""

    calls: list[tuple[Callable[..., object], tuple[object, ...]]] = []

    def fake_probe(service: str) -> dict[str, object]:
        assert service == "zilean"
        return {"ok": True, "latency_ms": 12, "message": "Connected"}

    async def fake_to_thread(
        func: Callable[..., object], /, *args: object, **kwargs: object
    ) -> object:
        assert not kwargs
        calls.append((func, args))
        return func(*args)

    monkeypatch.setattr("routers.secure.settings.run_connection_test", fake_probe)
    monkeypatch.setattr("routers.secure.settings.asyncio.to_thread", fake_to_thread)

    allowed = production_client.post(
        "/api/v1/settings/test-connection/zilean",
        headers=_signed_bff_headers("settings:write"),
    )
    assert allowed.status_code == 200
    assert allowed.json() == {"ok": True, "latency_ms": 12, "message": "Connected"}
    assert calls == [(fake_probe, ("zilean",))]

    denied = production_client.post(
        "/api/v1/settings/test-connection/zilean",
        headers=_signed_bff_headers("library:read"),
    )
    assert denied.status_code == 403
    assert denied.json()["detail"] == "Insufficient actor permissions"
    assert len(calls) == 1
