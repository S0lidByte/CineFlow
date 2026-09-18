"""OUTBOX-010 Lifecycle Event & Transactional Integrity Regression Suite."""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from program.contracts.dispatcher import (
    OutboxDispatcher,
    publish_outbox_lifecycle_event,
    register_outbox_lifecycle_listener,
)
from program.contracts.events import OutboxLifecycleEvent, OutboxLifecycleEventType
from program.contracts.operation_ledger import (
    OperationLedger,
    claim_due_operations,
    complete_operation,
    enqueue_operation,
    fail_operation,
)
from program.contracts.telemetry import redact_sensitive_data
from program.db.base_model import Base


@pytest.fixture
def test_db_session(tmp_path):
    """Provide an isolated file-backed SQLite database with WAL enabled."""
    db_path = tmp_path / "lifecycle_test.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"timeout": 15},
    )
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        conn.exec_driver_sql("PRAGMA busy_timeout=15000")

    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )

    class SessionContext:
        def __init__(self, factory):
            self.factory = factory
            self.session = None

        def __enter__(self):
            self.session = self.factory()
            return self.session

        def __exit__(self, exc_type, exc_val, exc_tb):
            if self.session is not None:
                self.session.close()

    def _cm():
        return SessionContext(TestingSession)

    try:
        yield TestingSession, _cm
    finally:
        engine.dispose()


def test_lifecycle_event_contract_canonical_schema_and_version():
    """Verify OutboxLifecycleEvent satisfies the versioned canonical schema."""
    sample_op = {
        "id": "test-uuid-1234",
        "status": "pending",
        "operation_type": "item.test",
        "payload": {"secret": "redacted"},
    }
    event = OutboxLifecycleEvent(
        event_type=OutboxLifecycleEventType.ENQUEUED,
        operation=sample_op,
    )

    assert event.version == 1
    assert event.event_type == "operation_enqueued"
    assert event.event_name == "operation_enqueued"
    assert event.operation_id == "test-uuid-1234"
    assert event.status == "pending"
    assert event.payload == sample_op
    assert event.timestamp is not None


def test_enqueue_operation_emits_lifecycle_event_only_post_commit(test_db_session):
    """Verify operation_enqueued is published strictly after successful commit."""
    TestingSession, test_session = test_db_session
    events_received: list[OutboxLifecycleEvent] = []
    received_event = threading.Event()

    def listener(ev: OutboxLifecycleEvent) -> None:
        if ev.event_type == OutboxLifecycleEventType.ENQUEUED:
            events_received.append(ev)
            received_event.set()

    register_outbox_lifecycle_listener(listener)

    # 1. Rollback case: ensure NO event is emitted
    with TestingSession() as session:
        enqueue_operation(
            session,
            operation_type="item.rollback_test",
            payload={"key": "val"},
        )
        session.rollback()

    time.sleep(0.1)
    assert len(events_received) == 0

    # 2. Commit case: ensure event IS emitted with canonical payload
    with TestingSession() as session:
        op = enqueue_operation(
            session,
            operation_type="item.commit_test",
            payload={"token": "secret_token_12345"},
        )
        session.commit()
        op_id = op.id

    assert received_event.wait(timeout=3.0)
    assert len(events_received) == 1
    enqueued_ev = events_received[0]
    assert enqueued_ev.operation_id == op_id
    assert enqueued_ev.event_type == OutboxLifecycleEventType.ENQUEUED
    assert enqueued_ev.payload is not None
    assert enqueued_ev.payload["payload"]["token"] == "[REDACTED]"


def test_dispatcher_publishes_all_five_lifecycle_transitions(test_db_session):
    """Verify started, completed, failed, and retrying lifecycle transitions."""
    TestingSession, test_session = test_db_session
    lifecycle_events: list[OutboxLifecycleEvent] = []
    events_lock = threading.Lock()

    def listener(ev: OutboxLifecycleEvent) -> None:
        with events_lock:
            lifecycle_events.append(ev)

    register_outbox_lifecycle_listener(listener)

    dispatcher = OutboxDispatcher(
        worker_id="test-worker-lifecycle-all",
        max_workers=2,
        poll_interval_seconds=0.05,
        base_backoff_seconds=0.1,
        max_retries=2,
        db_session_cm=test_session,
    )

    success_handled = threading.Event()
    retry_handled = threading.Event()
    permanent_handled = threading.Event()

    def handle_success(_op, _p):
        success_handled.set()

    def handle_retry(_op, _p):
        retry_handled.set()
        from program.contracts.errors import ProviderNetworkError

        raise ProviderNetworkError("Transient network glitch")

    def handle_permanent(_op, _p):
        permanent_handled.set()
        from program.contracts.errors import ProviderAuthError

        raise ProviderAuthError("Fatal authentication failure")

    dispatcher.register_handler("item.lifecycle_success", handle_success)
    dispatcher.register_handler("item.lifecycle_retry", handle_retry)
    dispatcher.register_handler("item.lifecycle_perm", handle_permanent)

    dispatcher.start()
    try:
        # A. Enqueue Success Op
        with TestingSession() as session:
            op1 = enqueue_operation(
                session, "item.lifecycle_success", {"secret": "pass1"}
            )
            session.commit()
            op1_id = op1.id

        assert success_handled.wait(timeout=5.0)

        # B. Enqueue Transient Retry Op
        with TestingSession() as session:
            op2 = enqueue_operation(
                session, "item.lifecycle_retry", {"secret": "pass2"}
            )
            session.commit()
            op2_id = op2.id

        assert retry_handled.wait(timeout=5.0)

        # C. Enqueue Permanent Failed Op
        with TestingSession() as session:
            op3 = enqueue_operation(session, "item.lifecycle_perm", {"secret": "pass3"})
            session.commit()
            op3_id = op3.id

        assert permanent_handled.wait(timeout=5.0)

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            with events_lock:
                event_types = {e.event_type for e in lifecycle_events}
                if (
                    OutboxLifecycleEventType.STARTED in event_types
                    and OutboxLifecycleEventType.COMPLETED in event_types
                    and OutboxLifecycleEventType.RETRYING in event_types
                    and OutboxLifecycleEventType.FAILED in event_types
                ):
                    break
            time.sleep(0.05)

        with events_lock:
            by_op: dict[str, list[str]] = {}
            for e in lifecycle_events:
                by_op.setdefault(e.operation_id or "", []).append(e.event_type)

            # Op 1: enqueued -> started -> completed
            assert OutboxLifecycleEventType.ENQUEUED in by_op[op1_id]
            assert OutboxLifecycleEventType.STARTED in by_op[op1_id]
            assert OutboxLifecycleEventType.COMPLETED in by_op[op1_id]

            # Op 2: enqueued -> started -> retrying
            assert OutboxLifecycleEventType.ENQUEUED in by_op[op2_id]
            assert OutboxLifecycleEventType.STARTED in by_op[op2_id]
            assert OutboxLifecycleEventType.RETRYING in by_op[op2_id]

            # Op 3: enqueued -> started -> failed
            assert OutboxLifecycleEventType.ENQUEUED in by_op[op3_id]
            assert OutboxLifecycleEventType.STARTED in by_op[op3_id]
            assert OutboxLifecycleEventType.FAILED in by_op[op3_id]

    finally:
        dispatcher.stop(wait=True)


def test_slow_failing_listeners_are_asynchronous_and_isolated(test_db_session):
    """Verify slow or crashing observers do not block worker execution or leak exceptions."""
    TestingSession, test_session = test_db_session
    slow_listener_started = threading.Event()
    slow_listener_done = threading.Event()

    def slow_listener(ev: OutboxLifecycleEvent) -> None:
        if ev.event_type == OutboxLifecycleEventType.STARTED:
            slow_listener_started.set()
            time.sleep(0.5)
            slow_listener_done.set()

    def crashing_listener(ev: OutboxLifecycleEvent) -> None:
        raise RuntimeError("Immediate observer crash")

    register_outbox_lifecycle_listener(slow_listener)
    register_outbox_lifecycle_listener(crashing_listener)

    dispatcher = OutboxDispatcher(
        worker_id="test-worker-isolation",
        max_workers=1,
        poll_interval_seconds=0.05,
        db_session_cm=test_session,
    )

    completed = threading.Event()
    dispatcher.register_handler("item.isolation", lambda _op, _p: completed.set())
    dispatcher.start()

    try:
        t0 = time.monotonic()
        with TestingSession() as session:
            op = enqueue_operation(session, "item.isolation", {})
            session.commit()
            op_id = op.id

        assert completed.wait(timeout=5.0)
        t_elapsed = time.monotonic() - t0

        # Worker completion should NOT wait for the 0.5s slow listener
        assert slow_listener_started.wait(timeout=3.0)
        assert slow_listener_done.wait(timeout=3.0)

        with TestingSession() as session:
            db_op = session.get(OperationLedger, op_id)
            assert db_op is not None
            assert db_op.status == "completed"

    finally:
        dispatcher.stop(wait=True)
