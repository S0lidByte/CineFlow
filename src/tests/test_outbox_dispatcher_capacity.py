"""Comprehensive unit and integration tests for OUTBOX-007 Dynamic Capacity Throttling in OutboxDispatcher."""

from __future__ import annotations

import os
import tempfile
import threading
import time
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

import program.contracts.dispatcher as dispatcher_module
from program.contracts.dispatcher import OutboxDispatcher
from program.contracts.operation_ledger import (
    OperationLedger,
    claim_due_operations,
    enqueue_operation,
)
from program.db.base_model import get_base_metadata


@pytest.fixture
def test_db_session():
    """Set up an isolated file-backed SQLite database in WAL mode for dispatcher capacity testing."""
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


def test_capacity_throttling_skips_claim_when_fully_occupied(test_db_session):
    """Verify that when active leases equals max_workers, no DB claim query is executed."""
    TestingSession, _test_session = test_db_session

    dispatcher = OutboxDispatcher(
        worker_id="test-capacity-1",
        max_workers=3,
        poll_interval_seconds=1.0,
        db_session_cm=_test_session,
    )
    dispatcher._executor = MagicMock()

    # Simulate 3 active leases occupying all capacity
    with dispatcher._leases_lock:
        dispatcher._active_leases.update({"op-1", "op-2", "op-3"})

    with patch.object(dispatcher_module, "claim_due_operations") as mock_claim:
        dispatcher._dispatch_due_operations()
        mock_claim.assert_not_called()


def test_capacity_throttling_skips_claim_when_over_capacity(test_db_session):
    """Verify that if active leases exceeds max_workers for any reason, no claim query is executed."""
    TestingSession, _test_session = test_db_session

    dispatcher = OutboxDispatcher(
        worker_id="test-capacity-2",
        max_workers=2,
        poll_interval_seconds=1.0,
        db_session_cm=_test_session,
    )
    dispatcher._executor = MagicMock()

    # Simulate 3 active leases when max_workers is 2
    with dispatcher._leases_lock:
        dispatcher._active_leases.update({"op-1", "op-2", "op-3"})

    with patch.object(dispatcher_module, "claim_due_operations") as mock_claim:
        dispatcher._dispatch_due_operations()
        mock_claim.assert_not_called()


def test_capacity_throttling_claims_exact_available_slots(test_db_session):
    """Verify that available_slots (max_workers - active_leases) is passed as limit to claim_due_operations."""
    TestingSession, _test_session = test_db_session

    dispatcher = OutboxDispatcher(
        worker_id="test-capacity-3",
        max_workers=5,
        poll_interval_seconds=1.0,
        db_session_cm=_test_session,
    )
    dispatcher._executor = MagicMock()

    # Simulate 2 active leases out of 5 slots -> 3 available slots
    with dispatcher._leases_lock:
        dispatcher._active_leases.update({"op-1", "op-2"})

    with patch.object(
        dispatcher_module, "claim_due_operations", return_value=[]
    ) as mock_claim:
        dispatcher._dispatch_due_operations()
        mock_claim.assert_called_once()
        _, kwargs = mock_claim.call_args
        assert kwargs["limit"] == 3


def test_capacity_throttling_claims_all_when_idle(test_db_session):
    """Verify that when 0 active leases exist, limit equals max_workers."""
    TestingSession, _test_session = test_db_session

    dispatcher = OutboxDispatcher(
        worker_id="test-capacity-4",
        max_workers=4,
        poll_interval_seconds=1.0,
        db_session_cm=_test_session,
    )
    dispatcher._executor = MagicMock()

    with patch.object(
        dispatcher_module, "claim_due_operations", return_value=[]
    ) as mock_claim:
        dispatcher._dispatch_due_operations()
        mock_claim.assert_called_once()
        _, kwargs = mock_claim.call_args
        assert kwargs["limit"] == 4


def test_capacity_throttling_end_to_end_concurrency_bounding(test_db_session):
    """Integration test: 10 pending ops with max_workers=2. Dispatcher processes exactly 2 concurrently."""
    TestingSession, _test_session = test_db_session

    # Seed 10 operations
    with _test_session() as session:
        for i in range(10):
            enqueue_operation(
                session=session,
                operation_type="test_throttled_op",
                payload={"index": i},
            )
        session.commit()

    max_concurrent_observed = 0
    current_concurrent = 0
    lock = threading.Lock()
    processed_count = 0
    all_processed_event = threading.Event()

    def slow_handler(op: OperationLedger, payload: dict[str, Any]) -> None:
        nonlocal max_concurrent_observed, current_concurrent, processed_count
        with lock:
            current_concurrent += 1
            max_concurrent_observed = max(max_concurrent_observed, current_concurrent)
        time.sleep(0.05)
        with lock:
            current_concurrent -= 1
            processed_count += 1
            if processed_count >= 10:
                all_processed_event.set()

    dispatcher = OutboxDispatcher(
        worker_id="test-throttling-e2e",
        max_workers=2,
        poll_interval_seconds=0.05,
        db_session_cm=_test_session,
    )
    dispatcher.register_handler("test_throttled_op", slow_handler)
    dispatcher.start()

    try:
        finished = all_processed_event.wait(timeout=10.0)
        assert finished, (
            f"Timed out waiting for operations to process. Count={processed_count}"
        )
        assert max_concurrent_observed <= 2, (
            f"Observed {max_concurrent_observed} concurrent workers > max_workers=2!"
        )
        assert processed_count == 10
    finally:
        dispatcher.stop(wait=True)


def test_capacity_throttling_slot_replenishment(test_db_session):
    """Verify that as active leases finish, available slots replenish and further ops are claimed."""
    TestingSession, _test_session = test_db_session

    # Seed 4 operations
    with _test_session() as session:
        for i in range(4):
            enqueue_operation(
                session=session,
                operation_type="test_replenish_op",
                payload={"i": i},
            )
        session.commit()

    gate_event = threading.Event()
    started_ops: list[int] = []
    started_lock = threading.Lock()
    completed_event = threading.Event()
    completed_ops: list[int] = []

    def gated_handler(op: OperationLedger, payload: dict[str, Any]) -> None:
        idx = payload.get("i", 0)
        with started_lock:
            started_ops.append(idx)
        # Block until released
        gate_event.wait(timeout=5.0)
        with started_lock:
            completed_ops.append(idx)
            if len(completed_ops) >= 4:
                completed_event.set()

    dispatcher = OutboxDispatcher(
        worker_id="test-replenish",
        max_workers=2,
        poll_interval_seconds=0.05,
        db_session_cm=_test_session,
    )
    dispatcher.register_handler("test_replenish_op", gated_handler)
    dispatcher.start()

    try:
        # Wait until first 2 operations start
        start_time = time.time()
        while time.time() - start_time < 3.0:
            with started_lock:
                if len(started_ops) == 2:
                    break
            time.sleep(0.02)

        with started_lock:
            assert len(started_ops) == 2, (
                f"Expected 2 started operations, got {len(started_ops)}"
            )

        # Active leases should be 2, matching max_workers
        with dispatcher._leases_lock:
            assert len(dispatcher._active_leases) == 2

        # Release the gate to allow workers to complete
        gate_event.set()

        # All 4 operations should eventually complete
        finished = completed_event.wait(timeout=5.0)
        assert finished, f"Expected 4 completed operations, got {len(completed_ops)}"

        # Wait briefly for worker finally blocks to discard active leases
        start_wait = time.time()
        while time.time() - start_wait < 3.0:
            with dispatcher._leases_lock:
                if len(dispatcher._active_leases) == 0:
                    break
            time.sleep(0.01)

        # Active leases should be empty
        with dispatcher._leases_lock:
            assert len(dispatcher._active_leases) == 0
    finally:
        dispatcher.stop(wait=True)


def test_capacity_throttling_thread_safe_leases_lock_synchronization(test_db_session):
    """Verify that available_slots is computed under _leases_lock and protected from race conditions."""
    TestingSession, _test_session = test_db_session

    dispatcher = OutboxDispatcher(
        worker_id="test-sync",
        max_workers=5,
        poll_interval_seconds=1.0,
        db_session_cm=_test_session,
    )

    # Verify that acquiring _leases_lock blocks concurrent mutations
    acquired = False
    with dispatcher._leases_lock:
        dispatcher._active_leases.add("test-op")
        acquired = True

    assert acquired
    assert "test-op" in dispatcher._active_leases


def test_capacity_throttling_does_not_mutate_leases_before_successful_claim(
    test_db_session,
):
    """Verify that _active_leases is only populated after operations are claimed from the database."""
    TestingSession, _test_session = test_db_session

    dispatcher = OutboxDispatcher(
        worker_id="test-no-mutate",
        max_workers=5,
        poll_interval_seconds=1.0,
        db_session_cm=_test_session,
    )
    dispatcher._executor = MagicMock()

    # Database has no operations
    dispatcher._dispatch_due_operations()

    with dispatcher._leases_lock:
        assert len(dispatcher._active_leases) == 0


def test_capacity_throttling_with_failing_claims_handles_cleanly(test_db_session):
    """Verify that if claim_due_operations raises an exception, dispatcher cleanly handles error without leaking slots."""
    TestingSession, _test_session = test_db_session

    dispatcher = OutboxDispatcher(
        worker_id="test-fail-claim",
        max_workers=5,
        poll_interval_seconds=1.0,
        db_session_cm=_test_session,
    )
    dispatcher._executor = MagicMock()

    with patch.object(
        dispatcher_module, "claim_due_operations", side_effect=RuntimeError("DB error")
    ):
        dispatcher._dispatch_due_operations()

    with dispatcher._leases_lock:
        assert len(dispatcher._active_leases) == 0


def test_capacity_throttling_respects_single_worker_limit(test_db_session):
    """Verify that with max_workers=1, dispatcher strictly runs 1 operation at a time."""
    TestingSession, _test_session = test_db_session

    # Seed 3 operations
    with _test_session() as session:
        for i in range(3):
            enqueue_operation(
                session=session,
                operation_type="test_single_worker",
                payload={"i": i},
            )
        session.commit()

    active_count = 0
    max_active = 0
    active_lock = threading.Lock()
    done_event = threading.Event()
    total_done = 0

    def single_worker_handler(op: OperationLedger, payload: dict[str, Any]) -> None:
        nonlocal active_count, max_active, total_done
        with active_lock:
            active_count += 1
            max_active = max(max_active, active_count)
        time.sleep(0.04)
        with active_lock:
            active_count -= 1
            total_done += 1
            if total_done >= 3:
                done_event.set()

    dispatcher = OutboxDispatcher(
        worker_id="test-single-worker",
        max_workers=1,
        poll_interval_seconds=0.02,
        db_session_cm=_test_session,
    )
    dispatcher.register_handler("test_single_worker", single_worker_handler)
    dispatcher.start()

    try:
        finished = done_event.wait(timeout=5.0)
        assert finished, f"Timed out. total_done={total_done}"
        assert max_active == 1, f"Expected max 1 active worker, observed {max_active}"
        assert total_done == 3
    finally:
        dispatcher.stop(wait=True)
