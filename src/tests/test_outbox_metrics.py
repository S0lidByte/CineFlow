"""Tests for Outbox Prometheus metrics collection, fail-safety, and registry integration."""

from __future__ import annotations

import os
import tempfile
import threading
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from program.contracts import outbox_metrics as metrics
from program.contracts.dispatcher import OutboxDispatcher, _invoke_listener_safely
from program.contracts.events import OutboxLifecycleEvent
from program.contracts.operation_ledger import (
    OperationLedger,
    complete_operation,
    enqueue_operation,
    fail_operation,
    renew_lease,
)
from program.db.base_model import get_base_metadata
from program.services.streaming.prom_cache_metrics import REGISTRY


@pytest.fixture
def test_db_session():
    """Set up an isolated file-backed SQLite database in WAL mode for metrics testing."""
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
        for ext in ["", "-wal", "-shm"]:
            try:
                os.remove(f"{db_path}{ext}")
            except OSError:
                pass


def test_outbox_metrics_recorders_direct_invocation():
    """Verify direct metric helper functions execute without exception and update registry values."""
    op_type = f"test_op_{uuid.uuid4().hex[:6]}"
    before_claims = (
        REGISTRY.get_sample_value(
            "riven_outbox_claimed_total", {"operation_type": op_type}
        )
        or 0.0
    )
    metrics.record_claim(op_type, 2)
    after_claims = (
        REGISTRY.get_sample_value(
            "riven_outbox_claimed_total", {"operation_type": op_type}
        )
        or 0.0
    )
    assert after_claims == before_claims + 2.0

    metrics.record_claim_latency(0.042)
    sample_count = (
        REGISTRY.get_sample_value("riven_outbox_claim_latency_seconds_count") or 0.0
    )
    assert sample_count > 0.0

    metrics.set_active_leases(7)
    active_val = REGISTRY.get_sample_value("riven_outbox_active_leases")
    assert active_val == 7.0

    metrics.set_active_leases(0)
    assert REGISTRY.get_sample_value("riven_outbox_active_leases") == 0.0

    before_renew = (
        REGISTRY.get_sample_value(
            "riven_outbox_lease_renewals_total", {"result": "success"}
        )
        or 0.0
    )
    metrics.record_lease_renewal("success")
    after_renew = (
        REGISTRY.get_sample_value(
            "riven_outbox_lease_renewals_total", {"result": "success"}
        )
        or 0.0
    )
    assert after_renew == before_renew + 1.0

    before_stale = (
        REGISTRY.get_sample_value(
            "riven_outbox_stale_rejections_total", {"stage": "complete"}
        )
        or 0.0
    )
    metrics.record_stale_rejection("complete")
    after_stale = (
        REGISTRY.get_sample_value(
            "riven_outbox_stale_rejections_total", {"stage": "complete"}
        )
        or 0.0
    )
    assert after_stale == before_stale + 1.0

    before_retry = (
        REGISTRY.get_sample_value(
            "riven_outbox_retry_requests_total", {"status": "success"}
        )
        or 0.0
    )
    metrics.record_retry_request("success")
    after_retry = (
        REGISTRY.get_sample_value(
            "riven_outbox_retry_requests_total", {"status": "success"}
        )
        or 0.0
    )
    assert after_retry == before_retry + 1.0

    metrics.set_listener_queue_depth(3)
    assert REGISTRY.get_sample_value("riven_outbox_listener_queue_depth") == 3.0

    before_listener_fail = (
        REGISTRY.get_sample_value(
            "riven_outbox_listener_failures_total", {"event_type": "operation_started"}
        )
        or 0.0
    )
    metrics.record_listener_failure("operation_started")
    after_listener_fail = (
        REGISTRY.get_sample_value(
            "riven_outbox_listener_failures_total", {"event_type": "operation_started"}
        )
        or 0.0
    )
    assert after_listener_fail == before_listener_fail + 1.0


def test_outbox_dispatcher_instruments_claims_and_active_leases(test_db_session):
    """Verify that OutboxDispatcher claims update riven_outbox_claimed_total and active leases."""
    _, session_cm = test_db_session

    op_type = f"metric_scrape_{uuid.uuid4().hex[:6]}"
    with session_cm() as session:
        enqueue_operation(session, operation_type=op_type, payload={"item": 1})
        enqueue_operation(session, operation_type=op_type, payload={"item": 2})
        session.commit()

    before_claims = (
        REGISTRY.get_sample_value(
            "riven_outbox_claimed_total", {"operation_type": op_type}
        )
        or 0.0
    )

    dispatcher = OutboxDispatcher(
        max_workers=2,
        poll_interval_seconds=0.1,
        lease_duration_seconds=5,
        db_session_cm=session_cm,
    )

    completed_events = threading.Event()
    seen = []

    def _handler(ledger, payload):
        seen.append(payload.get("item"))
        if len(seen) >= 2:
            completed_events.set()

    dispatcher.register_handler(op_type, _handler)

    dispatcher.start()
    try:
        assert completed_events.wait(timeout=5.0), (
            "Timed out waiting for outbox operations"
        )
        # Allow cleanup of leases
        time.sleep(0.3)
    finally:
        dispatcher.stop(wait=True)

    after_claims = (
        REGISTRY.get_sample_value(
            "riven_outbox_claimed_total", {"operation_type": op_type}
        )
        or 0.0
    )
    assert after_claims == before_claims + 2.0
    assert REGISTRY.get_sample_value("riven_outbox_active_leases") == 0.0


def test_outbox_dispatcher_instruments_heartbeat_renewal_metrics(test_db_session):
    """Verify that active lease heartbeats record renewal metrics."""
    _, session_cm = test_db_session

    op_type = f"hb_metric_{uuid.uuid4().hex[:6]}"
    with session_cm() as session:
        enqueue_operation(session, operation_type=op_type, payload={"work": "long"})
        session.commit()

    before_renewals = (
        REGISTRY.get_sample_value(
            "riven_outbox_lease_renewals_total", {"result": "success"}
        )
        or 0.0
    )

    dispatcher = OutboxDispatcher(
        max_workers=1,
        poll_interval_seconds=0.1,
        lease_duration_seconds=1,  # heartbeat every 0.33s
        db_session_cm=session_cm,
    )

    proceed_event = threading.Event()

    def _handler(ledger, payload):
        # Hold for > 1 renewal intervals (0.33s * 2 = 0.66s)
        time.sleep(0.8)
        proceed_event.set()

    dispatcher.register_handler(op_type, _handler)

    dispatcher.start()
    try:
        assert proceed_event.wait(timeout=5.0)
        time.sleep(0.3)
    finally:
        dispatcher.stop(wait=True)

    after_renewals = (
        REGISTRY.get_sample_value(
            "riven_outbox_lease_renewals_total", {"result": "success"}
        )
        or 0.0
    )
    assert after_renewals > before_renewals


def test_outbox_dispatcher_instruments_stale_rejection_metrics(test_db_session):
    """Verify that lost ownership during completion triggers stale rejection metric."""
    _, session_cm = test_db_session

    op_type = f"stale_metric_{uuid.uuid4().hex[:6]}"
    with session_cm() as session:
        op = enqueue_operation(
            session, operation_type=op_type, payload={"corrupt": True}
        )
        session.commit()
        op_id = op.id

    before_stale = (
        REGISTRY.get_sample_value(
            "riven_outbox_stale_rejections_total", {"stage": "complete"}
        )
        or 0.0
    )

    dispatcher = OutboxDispatcher(
        max_workers=1,
        poll_interval_seconds=0.1,
        lease_duration_seconds=5,
        db_session_cm=session_cm,
    )

    handler_called = threading.Event()

    def _handler(ledger, payload):
        # Steal / corrupt the lease token in DB while handler runs
        with session_cm() as session:
            db_op = session.get(OperationLedger, ledger.id)
            if db_op:
                db_op.claim_token = "stolen-token"
                session.commit()
        handler_called.set()

    dispatcher.register_handler(op_type, _handler)

    dispatcher.start()
    try:
        assert handler_called.wait(timeout=5.0)
        time.sleep(0.5)
    finally:
        dispatcher.stop(wait=True)

    after_stale = (
        REGISTRY.get_sample_value(
            "riven_outbox_stale_rejections_total", {"stage": "complete"}
        )
        or 0.0
    )
    assert after_stale == before_stale + 1.0


def test_outbox_lifecycle_listener_failure_records_metric():
    """Verify that an exception in an isolated lifecycle listener increments listener failure metric."""
    event_type = f"test_ev_{uuid.uuid4().hex[:6]}"
    before_fails = (
        REGISTRY.get_sample_value(
            "riven_outbox_listener_failures_total", {"event_type": event_type}
        )
        or 0.0
    )

    def _broken_listener(event):
        raise RuntimeError("Intentional isolated listener explosion")

    event = OutboxLifecycleEvent(
        event_type=event_type,
        operation={"id": str(uuid.uuid4()), "status": "pending"},
    )
    _invoke_listener_safely(_broken_listener, event, event.operation_id)

    after_fails = (
        REGISTRY.get_sample_value(
            "riven_outbox_listener_failures_total", {"event_type": event_type}
        )
        or 0.0
    )
    assert after_fails == before_fails + 1.0


def test_render_metrics_includes_all_outbox_metrics():
    """Verify that render_metrics() output contains all 8 governed outbox metric names and definitions."""
    from program.services.streaming import prom_cache_metrics as prom

    metrics.record_claim("test_metric_presence")
    metrics.record_claim_latency(0.01)
    metrics.set_active_leases(1)
    metrics.record_lease_renewal("success")
    metrics.record_stale_rejection("complete")
    metrics.record_retry_request("success")
    metrics.set_listener_queue_depth(0)
    metrics.record_listener_failure("test_metric_presence")

    body = prom.render_metrics().decode("utf-8")

    expected_metrics = [
        "riven_outbox_claimed_total",
        "riven_outbox_claim_latency_seconds",
        "riven_outbox_active_leases",
        "riven_outbox_lease_renewals_total",
        "riven_outbox_stale_rejections_total",
        "riven_outbox_retry_requests_total",
        "riven_outbox_listener_queue_depth",
        "riven_outbox_listener_failures_total",
    ]

    for m in expected_metrics:
        assert m in body, (
            f"Expected metric '{m}' was not found in render_metrics() output"
        )


def test_outbox_metrics_fail_safety_never_raises():
    """Verify that recorder functions gracefully swallow unexpected collector exceptions."""
    from unittest.mock import patch

    with patch.object(
        metrics.OUTBOX_CLAIMED_TOTAL,
        "labels",
        side_effect=RuntimeError("Prometheus error"),
    ):
        metrics.record_claim("any_op")  # Should not raise

    with patch.object(
        metrics.OUTBOX_CLAIM_LATENCY_SECONDS,
        "observe",
        side_effect=RuntimeError("Prometheus error"),
    ):
        metrics.record_claim_latency(0.1)  # Should not raise

    with patch.object(
        metrics.OUTBOX_ACTIVE_LEASES,
        "set",
        side_effect=RuntimeError("Prometheus error"),
    ):
        metrics.set_active_leases(5)  # Should not raise

    with patch.object(
        metrics.OUTBOX_LEASE_RENEWALS_TOTAL,
        "labels",
        side_effect=RuntimeError("Prometheus error"),
    ):
        metrics.record_lease_renewal("success")  # Should not raise

    with patch.object(
        metrics.OUTBOX_STALE_REJECTIONS_TOTAL,
        "labels",
        side_effect=RuntimeError("Prometheus error"),
    ):
        metrics.record_stale_rejection("complete")  # Should not raise

    with patch.object(
        metrics.OUTBOX_RETRY_REQUESTS_TOTAL,
        "labels",
        side_effect=RuntimeError("Prometheus error"),
    ):
        metrics.record_retry_request("success")  # Should not raise

    with patch.object(
        metrics.OUTBOX_LISTENER_QUEUE_DEPTH,
        "set",
        side_effect=RuntimeError("Prometheus error"),
    ):
        metrics.set_listener_queue_depth(1)  # Should not raise

    with patch.object(
        metrics.OUTBOX_LISTENER_FAILURES_TOTAL,
        "labels",
        side_effect=RuntimeError("Prometheus error"),
    ):
        metrics.record_listener_failure("test")  # Should not raise
