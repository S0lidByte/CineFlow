"""Unit tests for the transactional OutboxDispatcher engine."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from program.contracts.dispatcher import OutboxDispatcher, notify_outbox_dispatcher
from program.contracts.errors import (
    ProviderAuthError,
    ProviderNetworkError,
    ProviderRateLimitError,
)
from program.contracts.operation_ledger import (
    OperationLedger,
    enqueue_operation,
)
from program.contracts.telemetry import get_correlation_id, set_correlation_id
from program.db.base_model import get_base_metadata
from program.db.db import db_session
from program.media.item import MediaItem


@pytest.fixture
def test_db_session(monkeypatch):
    """Set up an isolated file-backed SQLite database in WAL mode for dispatcher testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp_file:
        db_path = tmp_file.name

    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"timeout": 30.0, "check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_conn, _):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

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

    try:
        yield TestingSession, _test_session
    finally:
        engine.dispose()
        if os.path.exists(db_path):
            try:
                os.remove(db_path)
            except OSError:
                pass
        for shm_wal in [f"{db_path}-shm", f"{db_path}-wal"]:
            if os.path.exists(shm_wal):
                try:
                    os.remove(shm_wal)
                except OSError:
                    pass


def test_dispatcher_start_stop(test_db_session):
    """Test starting and gracefully stopping the dispatcher."""
    TestingSession, _test_session = test_db_session
    dispatcher = OutboxDispatcher(
        worker_id="test-worker-1",
        max_workers=2,
        poll_interval_seconds=0.1,
        db_session_cm=_test_session,
    )
    dispatcher.start()
    assert dispatcher._thread is not None
    assert dispatcher._thread.is_alive()

    dispatcher.stop(wait=True)
    assert dispatcher._thread is None
    assert dispatcher._executor is None


def test_dispatcher_executes_registered_handler(test_db_session):
    """Test that registered operation handlers execute and complete outbox tasks."""
    TestingSession, _test_session = test_db_session
    handled_events: list[dict[str, Any]] = []
    done_event = threading.Event()

    def my_handler(ledger: OperationLedger, payload: dict[str, Any]):
        handled_events.append(
            {
                "id": ledger.id,
                "type": ledger.operation_type,
                "payload": payload,
                "correlation_id": get_correlation_id(),
            }
        )
        done_event.set()

    dispatcher = OutboxDispatcher(
        worker_id="test-worker-2",
        max_workers=2,
        poll_interval_seconds=0.05,
        db_session_cm=_test_session,
    )
    dispatcher.register_handler("item.test_action", my_handler)
    dispatcher.start()

    try:
        with TestingSession() as session:
            op = enqueue_operation(
                session=session,
                operation_type="item.test_action",
                payload={"target_id": 42, "token": "secret123"},
                correlation_id="corr-test-123",
            )
            session.commit()
            op_id = op.id

        dispatcher.notify()
        assert done_event.wait(timeout=10.0), "Handler was not invoked in time"

        assert len(handled_events) == 1
        assert handled_events[0]["type"] == "item.test_action"
        assert handled_events[0]["payload"]["target_id"] == 42
        assert handled_events[0]["payload"]["token"] == "[REDACTED]"
        assert handled_events[0]["correlation_id"] == "corr-test-123"

        # Verify ledger status in DB
        time.sleep(0.2)
        with TestingSession() as session:
            saved = session.get(OperationLedger, op_id)
            assert saved is not None
            assert saved.status == "completed"
            assert saved.completed_at is not None
            assert saved.error_message is None
    finally:
        dispatcher.stop(wait=True)


def test_dispatcher_handles_transient_failure_and_backoff(test_db_session):
    """Test that transient errors (e.g. rate limits) back off and re-schedule without dead-lettering."""
    TestingSession, _test_session = test_db_session
    attempts: list[int] = []
    attempted_event = threading.Event()

    def failing_handler(ledger: OperationLedger, payload: dict[str, Any]):
        attempts.append(ledger.attempt_count)
        attempted_event.set()
        raise ProviderRateLimitError("Too Many Requests", retry_after_seconds=60.0)

    dispatcher = OutboxDispatcher(
        worker_id="test-worker-3",
        max_workers=2,
        poll_interval_seconds=0.05,
        max_retries=3,
        base_backoff_seconds=1.0,
        db_session_cm=_test_session,
    )
    dispatcher.register_handler("item.rate_limited_action", failing_handler)
    dispatcher.start()

    try:
        with TestingSession() as session:
            op = enqueue_operation(
                session=session,
                operation_type="item.rate_limited_action",
                payload={"foo": "bar"},
            )
            session.commit()
            op_id = op.id

        dispatcher.notify()
        assert attempted_event.wait(timeout=10.0), "Failing handler was not invoked"

        time.sleep(0.2)
        with TestingSession() as session:
            saved = session.get(OperationLedger, op_id)
            assert saved is not None
            assert saved.status == "pending"  # Re-scheduled for retry
            assert saved.attempt_count == 1
            assert saved.error_classification == "ProviderRateLimitError"
            assert "Too Many Requests" in (saved.error_message or "")
            sched_utc = (
                saved.scheduled_at
                if saved.scheduled_at.tzinfo is not None
                else saved.scheduled_at.replace(tzinfo=UTC)
            )
            assert sched_utc > datetime.now(UTC) + timedelta(seconds=50)
    finally:
        dispatcher.stop(wait=True)


def test_dispatcher_handles_permanent_failure_dead_letter(test_db_session):
    """Test that permanent errors (e.g. ProviderAuthError) dead-letter immediately."""
    TestingSession, _test_session = test_db_session
    failed_event = threading.Event()

    def auth_fail_handler(ledger: OperationLedger, payload: dict[str, Any]):
        failed_event.set()
        raise ProviderAuthError("Invalid API key secret_abcdef123456")

    dispatcher = OutboxDispatcher(
        worker_id="test-worker-4",
        max_workers=2,
        poll_interval_seconds=0.05,
        max_retries=3,
        db_session_cm=_test_session,
    )
    dispatcher.register_handler("item.auth_failed_action", auth_fail_handler)
    dispatcher.start()

    try:
        with TestingSession() as session:
            op = enqueue_operation(
                session=session,
                operation_type="item.auth_failed_action",
                payload={"user": "admin"},
            )
            session.commit()
            op_id = op.id

        dispatcher.notify()
        assert failed_event.wait(timeout=10.0), "Auth fail handler was not invoked"

        time.sleep(0.2)
        with TestingSession() as session:
            saved = session.get(OperationLedger, op_id)
            assert saved is not None
            assert saved.status == "failed"  # Dead-lettered
            assert saved.error_classification == "ProviderAuthError"
            assert "[REDACTED]" in (saved.error_message or "")
            assert "secret_abcdef123456" not in (saved.error_message or "")
    finally:
        dispatcher.stop(wait=True)


def test_dispatcher_publishes_canonical_sse_items_and_isolates_listener_failures(
    test_db_session, monkeypatch
):
    """Observer failures cannot prevent a committed operation update from reaching the UI."""
    TestingSession, test_session = test_db_session
    emitted_messages: list[dict[str, Any]] = []
    completed = threading.Event()

    def capture_sse(_channel: str, message: str) -> None:
        emitted_messages.append(json.loads(message))

    monkeypatch.setattr(
        "program.contracts.dispatcher.sse_manager.publish_event", capture_sse
    )

    dispatcher = OutboxDispatcher(
        worker_id="test-worker-canonical-sse",
        max_workers=1,
        poll_interval_seconds=0.05,
        db_session_cm=test_session,
    )
    dispatcher.register_lifecycle_listener(
        lambda _event: (_ for _ in ()).throw(RuntimeError("observer failure"))
    )
    dispatcher.register_handler(
        "item.canonical_sse", lambda _ledger, _payload: completed.set()
    )
    dispatcher.start()

    try:
        with TestingSession() as session:
            operation = enqueue_operation(
                session,
                operation_type="item.canonical_sse",
                payload={"nested": {"token": "test_dummy_token_val"}},
                correlation_id="corr-canonical-sse",
            )
            session.commit()
            operation_id = operation.id

        dispatcher.notify()
        assert completed.wait(timeout=10.0), "Handler was not invoked"

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not any(
            message.get("event_type") == "operation_completed"
            for message in emitted_messages
        ):
            time.sleep(0.02)

        completed_message = next(
            message
            for message in emitted_messages
            if message.get("event_type") == "operation_completed"
        )
        assert completed_message["id"] == operation_id
        assert completed_message["correlation_id"] == "corr-canonical-sse"
        assert completed_message["status"] == "completed"
        assert completed_message["attempt_count"] == 1
        assert completed_message["payload"]["nested"]["token"] == "[REDACTED]"
        for field_name in (
            "schema_version",
            "scheduled_at",
            "lease_expires_at",
            "worker_id",
            "started_at",
            "completed_at",
            "created_at",
            "updated_at",
        ):
            assert field_name in completed_message

        with TestingSession() as session:
            saved = session.get(OperationLedger, operation_id)
            assert saved is not None
            assert saved.status == "completed"
    finally:
        dispatcher.stop(wait=True)


def test_dispatcher_handles_raw_httpx_429_and_503_transient_retries(test_db_session):
    """Test that raw httpx.HTTPStatusError (429 and 503) are normalized and scheduled for retry."""
    import httpx

    TestingSession, _test_session = test_db_session
    attempts_429: list[int] = []
    attempted_429_event = threading.Event()

    def http_429_handler(ledger: OperationLedger, payload: dict[str, Any]):
        attempts_429.append(ledger.attempt_count)
        attempted_429_event.set()
        req = httpx.Request("GET", "https://api.example.com/stream")
        resp = httpx.Response(429, headers={"Retry-After": "45"}, request=req)
        raise httpx.HTTPStatusError("Too Many Requests", request=req, response=resp)

    dispatcher = OutboxDispatcher(
        worker_id="test-worker-http-errors",
        max_workers=2,
        poll_interval_seconds=0.05,
        max_retries=3,
        base_backoff_seconds=1.0,
        db_session_cm=_test_session,
    )
    dispatcher.register_handler("item.http_429_action", http_429_handler)
    dispatcher.start()

    try:
        with TestingSession() as session:
            op = enqueue_operation(
                session=session,
                operation_type="item.http_429_action",
                payload={"target": "url"},
            )
            session.commit()
            op_id = op.id

        dispatcher.notify()
        assert attempted_429_event.wait(timeout=10.0), "429 handler was not invoked"

        time.sleep(0.2)
        with TestingSession() as session:
            saved = session.get(OperationLedger, op_id)
            assert saved is not None
            assert saved.status == "pending"  # Re-scheduled, not dead-lettered
            assert saved.attempt_count == 1
            assert saved.error_classification == "ProviderRateLimitError"
            sched_utc = (
                saved.scheduled_at
                if saved.scheduled_at.tzinfo is not None
                else saved.scheduled_at.replace(tzinfo=UTC)
            )
            assert sched_utc > datetime.now(UTC) + timedelta(seconds=40)
    finally:
        dispatcher.stop(wait=True)

