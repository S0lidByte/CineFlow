"""Unit tests for the operational timeline REST endpoints."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from program.contracts.operation_ledger import OperationLedger, enqueue_operation
from program.db.base_model import get_base_metadata
from routers import app_router

TEST_BFF_API_KEY = "test-bff-service-key"
TEST_ACTOR_SECRET = "test-actor-context-secret"


def _signed_bff_headers(roles: str) -> dict[str, str]:
    actor_id = "operations-user"
    actor_client = "cineflow-web-bff"
    timestamp = str(int(datetime.now(UTC).timestamp()))
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
def api_client(monkeypatch):
    """Set up TestClient with an isolated in-memory SQLite database and test API key."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    get_base_metadata().create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    from contextlib import contextmanager

    @contextmanager
    def _test_session():
        session = TestingSession()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr("routers.secure.operations.db_session", _test_session)
    monkeypatch.setattr("program.contracts.dispatcher.db_session", _test_session)
    monkeypatch.setenv("BFF_API_KEY", TEST_BFF_API_KEY)
    monkeypatch.setenv("ACTOR_CONTEXT_SECRET", TEST_ACTOR_SECRET)

    app = FastAPI()
    app.include_router(app_router)
    client = TestClient(app)
    return client, TestingSession


def test_operations_timeline_list_and_filter(api_client):
    client, session_factory = api_client

    with session_factory() as session:
        op1 = enqueue_operation(
            session=session,
            operation_type="scraper.search",
            payload={"query": "Inception", "api_key": "supersecretkey123456"},
            correlation_id="corr-1",
        )
        op2 = enqueue_operation(
            session=session,
            operation_type="downloader.add",
            payload={"torrent": "magnet:?xt=urn:btih:..."},
            correlation_id="corr-2",
        )
        op2.status = "failed"
        op2.error_classification = "ProviderRateLimitError"
        op2.error_message = "Rate limit exceeded token=secrettokenvalue12345"
        session.commit()

    # Query all operations
    response = client.get(
        "/api/v1/operations/timeline",
        headers=_signed_bff_headers("library:read"),
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2

    # Verify secret redactions in responses
    for item in data["items"]:
        if item["operation_type"] == "scraper.search":
            assert item["payload"]["api_key"] == "[REDACTED]"
            assert item["payload"]["query"] == "Inception"
        elif item["operation_type"] == "downloader.add":
            assert item["status"] == "failed"
            assert "[REDACTED]" in item["error_message"]
            assert "secrettokenvalue12345" not in item["error_message"]

    # Filter by status
    resp_failed = client.get(
        "/api/v1/operations/timeline?status=failed",
        headers=_signed_bff_headers("library:read"),
    )
    assert resp_failed.status_code == 200
    failed_data = resp_failed.json()
    assert failed_data["total"] == 1
    assert failed_data["items"][0]["operation_type"] == "downloader.add"


def test_get_operation_detail_and_retry(api_client):
    client, session_factory = api_client

    with session_factory() as session:
        op = enqueue_operation(
            session=session,
            operation_type="downloader.fetch",
            payload={"link": "https://debrid.test/file?token=mytoken123456789"},
            correlation_id="corr-retry-test",
        )
        op.status = "failed"
        op.error_classification = "ProviderUnavailableError"
        op.error_message = "503 Service Unavailable"
        session.commit()
        op_id = op.id

    # Get single item detail
    resp_detail = client.get(
        f"/api/v1/operations/timeline/{op_id}",
        headers=_signed_bff_headers("library:read"),
    )
    assert resp_detail.status_code == 200
    detail = resp_detail.json()
    assert detail["id"] == op_id
    assert detail["status"] == "failed"
    assert detail["payload"]["link"] == "https://debrid.test/file?token=[REDACTED]"

    # Retry the failed operation
    from program.services.streaming.prom_cache_metrics import REGISTRY

    before_retries = (
        REGISTRY.get_sample_value(
            "riven_outbox_retry_requests_total", {"status": "success"}
        )
        or 0.0
    )

    with patch("routers.secure.operations.notify_outbox_dispatcher") as mock_notify:
        resp_retry = client.post(
            f"/api/v1/operations/timeline/{op_id}/retry",
            headers=_signed_bff_headers("platform:admin"),
        )
        assert resp_retry.status_code == 200
        retry_data = resp_retry.json()
        assert retry_data["success"] is True
        assert retry_data["operation"]["status"] == "pending"
        mock_notify.assert_called_once()

    after_retries = (
        REGISTRY.get_sample_value(
            "riven_outbox_retry_requests_total", {"status": "success"}
        )
        or 0.0
    )
    assert after_retries == before_retries + 1.0

    # Verify DB state updated
    with session_factory() as session:
        updated = session.get(OperationLedger, op_id)
        assert updated is not None
        assert updated.status == "pending"
        assert updated.error_message is None


def test_retry_requires_platform_admin_and_preserves_unauthorized_state(api_client):
    client, session_factory = api_client

    with session_factory() as session:
        op = enqueue_operation(
            session=session,
            operation_type="downloader.fetch",
            payload={"link": "https://debrid.test/file"},
            correlation_id="corr-rbac-retry",
        )
        op.status = "failed"
        op.attempt_count = 4
        op.worker_id = "failed-worker"
        op.claim_token = "failed-claim-token"
        op.lease_expires_at = datetime.now(UTC)
        session.commit()
        op_id = op.id

    expected_state = ("failed", 4, "failed-worker", "failed-claim-token")
    for headers, expected_status in (
        ({}, 401),
        (_signed_bff_headers("library:read"), 403),
    ):
        response = client.post(
            f"/api/v1/operations/timeline/{op_id}/retry", headers=headers
        )
        assert response.status_code == expected_status
        with session_factory() as session:
            unchanged = session.get(OperationLedger, op_id)
            assert unchanged is not None
            assert (
                unchanged.status,
                unchanged.attempt_count,
                unchanged.worker_id,
                unchanged.claim_token,
            ) == expected_state


def test_admin_retry_preserves_atomic_failed_state_guard(api_client):
    client, session_factory = api_client
    from program.services.streaming.prom_cache_metrics import REGISTRY

    before_rejected = (
        REGISTRY.get_sample_value(
            "riven_outbox_retry_requests_total", {"status": "rejected"}
        )
        or 0.0
    )

    for operation_status in ("pending", "processing", "completed"):
        with session_factory() as session:
            op = enqueue_operation(
                session=session,
                operation_type="downloader.fetch",
                payload={"link": "https://debrid.test/file"},
                correlation_id=f"corr-{operation_status}-retry",
            )
            op.status = operation_status
            op.attempt_count = 2
            op.worker_id = (
                "processing-worker" if operation_status == "processing" else None
            )
            op.claim_token = (
                "processing-claim" if operation_status == "processing" else None
            )
            session.commit()
            op_id = op.id

        response = client.post(
            f"/api/v1/operations/timeline/{op_id}/retry",
            headers=_signed_bff_headers("platform:admin"),
        )
        assert response.status_code == 400
        with session_factory() as session:
            unchanged = session.get(OperationLedger, op_id)
            assert unchanged is not None
            assert unchanged.status == operation_status
            assert unchanged.attempt_count == 2
            assert unchanged.worker_id == (
                "processing-worker" if operation_status == "processing" else None
            )
            assert unchanged.claim_token == (
                "processing-claim" if operation_status == "processing" else None
            )

    after_rejected = (
        REGISTRY.get_sample_value(
            "riven_outbox_retry_requests_total", {"status": "rejected"}
        )
        or 0.0
    )
    assert after_rejected == before_rejected + 3.0
