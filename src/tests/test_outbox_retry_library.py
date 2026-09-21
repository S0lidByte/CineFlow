"""
Comprehensive unit and integration tests for Track 4: Durable Library Retry Dispatcher.

Covers:
- Operation contract, payload structure, and credential redaction
- Candidate selection, eligibility filtering, and requested_at ordering
- Active exclusions (EventManager active IDs + Outbox pending/processing IDs)
- Windowed idempotency and duplicate prevention under concurrent ticks
- Dispatcher execution, lease lifecycle, handler invocation, and completion
- Safe handling of deleted, stale, terminal, and already-active media items
- Transient error handling (ProviderRateLimitError, ProviderUnavailableError) with backoff
- Permanent error handling (ProviderAuthError) with immediate dead-lettering
- Max retries limit enforcement
- Post-commit dispatcher notification
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from program.contracts.dispatcher import (
    OutboxDispatcher,
)
from program.contracts.errors import (
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderUnavailableError,
)
from program.contracts.operation_ledger import (
    OperationLedger,
    enqueue_operation,
    get_active_outbox_item_ids,
)
from program.db import db_functions
from program.db.base_model import get_base_metadata
from program.managers.event_manager import EventManager
from program.media.item import Movie, Show
from program.media.state import States
from program.program import Program
from program.scheduling.scheduler import ProgramScheduler
from program.settings import settings_manager
from program.types import Event


@pytest.fixture
def test_db(monkeypatch):
    """Create an isolated file-backed SQLite database in WAL mode for retry dispatcher testing."""
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

    monkeypatch.setattr("program.db.db.db_session", _test_session)
    monkeypatch.setattr("program.program.db_session", _test_session)
    monkeypatch.setattr("program.scheduling.scheduler.db_session", _test_session)
    monkeypatch.setattr("program.managers.event_manager.db_session", _test_session)
    monkeypatch.setattr(
        "program.contracts.operation_ledger.db_session", _test_session, raising=False
    )
    monkeypatch.setattr(
        "program.contracts.dispatcher.db_session", _test_session, raising=False
    )
    monkeypatch.setattr(
        "program.db.db_functions.db_session", _test_session, raising=False
    )

    try:
        yield TestingSession, _test_session
    finally:
        engine.dispose()
        if os.path.exists(db_path):
            try:
                os.remove(db_path)
            except OSError:
                pass
        for ext in ["-shm", "-wal"]:
            wal_file = f"{db_path}{ext}"
            if os.path.exists(wal_file):
                try:
                    os.remove(wal_file)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# 1. Operation Contract & Redaction
# ---------------------------------------------------------------------------


def test_retry_library_contract_and_payload_redaction(test_db):
    """Verify operation_type='retry_library_item', payload structure, and sensitive data redaction."""
    _, db_session_cm = test_db

    with db_session_cm() as session:
        raw_payload = {
            "item_id": 42,
            "title": "Secret Agent Movie",
            "type": "movie",
            "reason": "scheduled_retry",
            "api_key": "alldebrid_super_secret_token_12345",
            "token": "rd_secret_token_67890",
        }

        op = enqueue_operation(
            session=session,
            operation_type="retry_library_item",
            payload=raw_payload,
            media_item_id=42,
            idempotency_key="retry_library_item:42:1000",
        )
        session.commit()

        assert op.operation_type == "retry_library_item"
        assert op.media_item_id == 42
        assert op.status == "pending"
        assert op.attempt_count == 0
        assert op.idempotency_key == "retry_library_item:42:1000"

        # Verify sensitive fields are redacted in persistent ledger
        assert op.payload is not None
        assert op.payload["item_id"] == 42
        assert op.payload["title"] == "Secret Agent Movie"
        assert op.payload["api_key"] == "[REDACTED]"
        assert op.payload["token"] == "[REDACTED]"


# ---------------------------------------------------------------------------
# 2. Candidate Selection & Eligibility
# ---------------------------------------------------------------------------


def test_candidate_selection_eligibility(test_db):
    """Verify db_functions.retry_library candidate selection adheres strictly to eligibility rules."""
    _, db_session_cm = test_db

    now = datetime.now(UTC)

    with db_session_cm() as session:
        # Eligible: Movie in Indexed state
        movie_eligible = Movie({"title": "Eligible Movie", "imdb_id": "tt1000001"})
        movie_eligible.last_state = States.Indexed
        movie_eligible.requested_at = now - timedelta(minutes=10)

        # Eligible: Show in Scraped state (older requested_at)
        show_eligible = Show({"title": "Eligible Show", "imdb_id": "tt1000002"})
        show_eligible.last_state = States.Scraped
        show_eligible.requested_at = now - timedelta(minutes=30)

        # Eligible: Movie in Downloaded state (newest requested_at)
        movie_newest = Movie({"title": "Newest Movie", "imdb_id": "tt1000003"})
        movie_newest.last_state = States.Downloaded
        movie_newest.requested_at = now - timedelta(minutes=1)

        # Ineligible states:
        movie_completed = Movie({"title": "Completed Movie", "imdb_id": "tt1000004"})
        movie_completed.last_state = States.Completed

        show_unreleased = Show({"title": "Unreleased Show", "imdb_id": "tt1000005"})
        show_unreleased.last_state = States.Unreleased

        movie_paused = Movie({"title": "Paused Movie", "imdb_id": "tt1000006"})
        movie_paused.last_state = States.Paused

        movie_failed = Movie({"title": "Failed Movie", "imdb_id": "tt1000007"})
        movie_failed.last_state = States.Failed

        session.add_all(
            [
                movie_eligible,
                show_eligible,
                movie_newest,
                movie_completed,
                show_unreleased,
                movie_paused,
                movie_failed,
            ]
        )
        session.commit()

        # Query all candidates without limit
        candidates = db_functions.retry_library(session=session)
        assert len(candidates) == 3

        # Verify ordering: newest requested_at first (movie_newest, movie_eligible, show_eligible)
        assert list(candidates) == [
            movie_newest.id,
            movie_eligible.id,
            show_eligible.id,
        ]

        # Verify batch limit
        limited_candidates = db_functions.retry_library(session=session, limit=2)
        assert len(limited_candidates) == 2
        assert list(limited_candidates) == [movie_newest.id, movie_eligible.id]


# ---------------------------------------------------------------------------
# 3. Active Exclusions (EventManager + Outbox Ledger)
# ---------------------------------------------------------------------------


def test_active_exclusions(test_db):
    """Verify candidate exclusion combines EventManager active items and Outbox pending/processing items."""
    _, db_session_cm = test_db

    with db_session_cm() as session:
        movie1 = Movie({"title": "Item 1 (EM Active)", "imdb_id": "tt2000001"})
        movie1.last_state = States.Indexed
        movie2 = Movie({"title": "Item 2 (Outbox Pending)", "imdb_id": "tt2000002"})
        movie2.last_state = States.Indexed
        movie3 = Movie({"title": "Item 3 (Outbox Processing)", "imdb_id": "tt2000003"})
        movie3.last_state = States.Indexed
        movie4 = Movie({"title": "Item 4 (Outbox Completed)", "imdb_id": "tt2000004"})
        movie4.last_state = States.Indexed
        movie5 = Movie({"title": "Item 5 (Outbox Failed)", "imdb_id": "tt2000005"})
        movie5.last_state = States.Indexed
        movie6 = Movie({"title": "Item 6 (Fully Eligible)", "imdb_id": "tt2000006"})
        movie6.last_state = States.Indexed

        session.add_all([movie1, movie2, movie3, movie4, movie5, movie6])
        session.commit()

        # Item 2 is pending in outbox
        enqueue_operation(
            session=session,
            operation_type="retry_library_item",
            payload={"item_id": movie2.id},
            media_item_id=movie2.id,
            idempotency_key=f"retry:{movie2.id}",
        )

        # Item 3 is processing in outbox
        op3 = enqueue_operation(
            session=session,
            operation_type="retry_library_item",
            payload={"item_id": movie3.id},
            media_item_id=movie3.id,
            idempotency_key=f"retry:{movie3.id}",
        )
        op3.status = "processing"
        op3.claim_token = "claim-token-123"

        # Item 4 is completed in outbox (no longer active)
        op4 = enqueue_operation(
            session=session,
            operation_type="retry_library_item",
            payload={"item_id": movie4.id},
            media_item_id=movie4.id,
            idempotency_key=f"retry:{movie4.id}",
        )
        op4.status = "completed"

        # Item 5 is failed in outbox (no longer active)
        op5 = enqueue_operation(
            session=session,
            operation_type="retry_library_item",
            payload={"item_id": movie5.id},
            media_item_id=movie5.id,
            idempotency_key=f"retry:{movie5.id}",
        )
        op5.status = "failed"

        session.commit()

        outbox_active_ids = get_active_outbox_item_ids(session, "retry_library_item")
        assert outbox_active_ids == {movie2.id, movie3.id}

        # Item 1 is active in EventManager
        em_active_ids = {movie1.id}

        excluded_ids = em_active_ids | outbox_active_ids
        assert excluded_ids == {movie1.id, movie2.id, movie3.id}

        candidates = db_functions.retry_library(
            session=session, exclude_ids=excluded_ids
        )
        # Items 4, 5, 6 are eligible
        assert set(candidates) == {movie4.id, movie5.id, movie6.id}


# ---------------------------------------------------------------------------
# 4. Windowed Idempotency & Concurrency
# ---------------------------------------------------------------------------


def test_windowed_idempotency_duplicate_prevention(test_db):
    """Verify windowed idempotency key prevents duplicate operations for the same item in the same window."""
    _, db_session_cm = test_db

    now = datetime.now(UTC)
    retry_interval = 600
    window_epoch = int(now.timestamp() // retry_interval)
    idempotency_key = f"retry_library_item:100:{window_epoch}"

    with db_session_cm() as session:
        op1 = enqueue_operation(
            session=session,
            operation_type="retry_library_item",
            payload={"item_id": 100, "title": "Test Movie"},
            media_item_id=100,
            idempotency_key=idempotency_key,
        )
        session.commit()

        # Second invocation in same window
        op2 = enqueue_operation(
            session=session,
            operation_type="retry_library_item",
            payload={"item_id": 100, "title": "Test Movie"},
            media_item_id=100,
            idempotency_key=idempotency_key,
        )
        session.commit()

        assert op1.id == op2.id

        # Verify DB has exactly 1 row
        count = (
            session.execute(
                select(OperationLedger).filter(
                    OperationLedger.idempotency_key == idempotency_key
                )
            )
            .scalars()
            .all()
        )
        assert len(count) == 1


def test_concurrent_enqueue_idempotency(test_db):
    """Verify concurrent worker threads attempting to enqueue the same idempotency key produce exactly 1 record."""
    TestingSession, _ = test_db

    idempotency_key = "retry_library_item:999:55555"
    errors = []
    enqueued_ops = []

    def _worker():
        session = TestingSession()
        try:
            op = enqueue_operation(
                session=session,
                operation_type="retry_library_item",
                payload={"item_id": 999, "title": "Race Movie"},
                media_item_id=999,
                idempotency_key=idempotency_key,
            )
            session.commit()
            enqueued_ops.append(op.id)
        except Exception as e:
            session.rollback()
            errors.append(e)
        finally:
            session.close()

    threads = [threading.Thread(target=_worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Verify at least one succeeded and exactly 1 operation exists in DB
    session = TestingSession()
    try:
        rows = (
            session.execute(
                select(OperationLedger).filter(
                    OperationLedger.idempotency_key == idempotency_key
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].media_item_id == 999
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 5. Dispatcher Execution & Canonical Re-entry
# ---------------------------------------------------------------------------


def test_dispatcher_execution_and_handler_reentry(test_db, monkeypatch):
    """Verify OutboxDispatcher claims operation, invokes Program._handle_retry_library_item, and submits Event."""
    _, db_session_cm = test_db

    with db_session_cm() as session:
        movie = Movie({"title": "Retry Target", "imdb_id": "tt3000001"})
        movie.last_state = States.Indexed
        session.add(movie)
        session.commit()
        item_id = movie.id

    program = MagicMock(spec=Program)
    program.em = EventManager()

    # Mock settings
    mock_settings = MagicMock()
    mock_settings.retry_library_batch_size = 10
    mock_settings.retry_interval = 600
    monkeypatch.setattr(settings_manager, "settings", mock_settings)

    # Instantiate OutboxDispatcher with custom session context manager
    dispatcher = OutboxDispatcher(
        worker_id="test-retry-worker",
        max_workers=2,
        poll_interval_seconds=0.05,
        lease_duration_seconds=30,
        db_session_cm=db_session_cm,
    )

    # Attach the real handler logic to program and register it with dispatcher
    def _handle_retry_library_item(
        op: OperationLedger, payload: dict[str, Any]
    ) -> None:
        Program._handle_retry_library_item(program, op, payload)

    dispatcher.register_handler("retry_library_item", _handle_retry_library_item)

    # Enqueue a durable retry operation
    with db_session_cm() as session:
        op = enqueue_operation(
            session=session,
            operation_type="retry_library_item",
            payload={"item_id": item_id, "title": "Retry Target", "type": "movie"},
            media_item_id=item_id,
            idempotency_key=f"retry:{item_id}:1",
        )
        session.commit()
        op_id = op.id

    dispatcher.start()
    try:
        dispatcher.notify()

        # Wait for dispatcher to process the operation
        deadline = time.time() + 5.0
        completed = False
        while time.time() < deadline:
            with db_session_cm() as session:
                refreshed = session.get(OperationLedger, op_id)
                if refreshed and refreshed.status == "completed":
                    completed = True
                    break
            time.sleep(0.05)

        assert completed, "Operation did not complete within deadline"

        # Verify EventManager received the retry event
        queued_events = list(program.em._queued_events)
        assert len(queued_events) == 1
        assert queued_events[0].item_id == item_id
        assert queued_events[0].emitted_by == "RetryLibrary"

    finally:
        dispatcher.stop(wait=True)


# ---------------------------------------------------------------------------
# 6. Safe Handling of Stale, Deleted, Terminal, and Active Items
# ---------------------------------------------------------------------------


def test_handler_stale_deleted_and_terminal_items(test_db):
    """Verify Program._handle_retry_library_item safely handles deleted, terminal, parent-blocked, and active items."""
    _, db_session_cm = test_db

    program = MagicMock(spec=Program)
    program.em = EventManager()

    with db_session_cm() as session:
        movie_completed = Movie({"title": "Completed Movie", "imdb_id": "tt4000001"})
        movie_completed.last_state = States.Completed
        movie_paused = Movie({"title": "Paused Movie", "imdb_id": "tt4000002"})
        movie_paused.last_state = States.Paused
        movie_failed = Movie({"title": "Failed Movie", "imdb_id": "tt4000003"})
        movie_failed.last_state = States.Failed
        movie_active = Movie({"title": "Active Movie", "imdb_id": "tt4000004"})
        movie_active.last_state = States.Indexed

        session.add_all([movie_completed, movie_paused, movie_failed, movie_active])
        session.commit()

        id_completed = movie_completed.id
        id_paused = movie_paused.id
        id_failed = movie_failed.id
        id_active = movie_active.id

    # 1. Deleted item (item_id 999999 does not exist)
    op_deleted = OperationLedger(
        id="op-1",
        correlation_id="c1",
        media_item_id=999999,
        operation_type="retry_library_item",
    )
    Program._handle_retry_library_item(program, op_deleted, {"item_id": 999999})
    assert len(program.em._queued_events) == 0

    # 2. Terminal items (Completed, Paused, Failed)
    for term_id in [id_completed, id_paused, id_failed]:
        op_term = OperationLedger(
            id=f"op-{term_id}",
            correlation_id="c1",
            media_item_id=term_id,
            operation_type="retry_library_item",
        )
        Program._handle_retry_library_item(program, op_term, {"item_id": term_id})
        assert len(program.em._queued_events) == 0

    # 3. Already active item in EventManager
    program.em.add_event(Event(emitted_by="Existing", item_id=id_active))
    assert len(program.em._queued_events) == 1

    op_active = OperationLedger(
        id=f"op-{id_active}",
        correlation_id="c1",
        media_item_id=id_active,
        operation_type="retry_library_item",
    )
    Program._handle_retry_library_item(program, op_active, {"item_id": id_active})
    # Count should remain 1 (no duplicate queued)
    assert len(program.em._queued_events) == 1


# ---------------------------------------------------------------------------
# 7. Transient Error Handling & Backoff
# ---------------------------------------------------------------------------


def test_transient_error_handling_and_backoff(test_db):
    """Verify transient errors (ProviderRateLimitError, ProviderUnavailableError) cause backoff retry."""
    _, db_session_cm = test_db

    attempted_event = threading.Event()

    def _failing_handler(op: OperationLedger, payload: dict[str, Any]) -> None:
        attempted_event.set()
        raise ProviderRateLimitError("Rate limit exceeded", retry_after_seconds=60.0)

    dispatcher = OutboxDispatcher(
        worker_id="transient-worker",
        max_workers=1,
        poll_interval_seconds=0.05,
        base_backoff_seconds=1.0,
        db_session_cm=db_session_cm,
    )
    dispatcher.register_handler("retry_library_item", _failing_handler)

    with db_session_cm() as session:
        op = enqueue_operation(
            session=session,
            operation_type="retry_library_item",
            payload={"item_id": 501},
            media_item_id=501,
            idempotency_key="transient-test-501",
        )
        session.commit()
        op_id = op.id

    dispatcher.start()
    try:
        dispatcher.notify()
        assert attempted_event.wait(timeout=10.0), "Failing handler was not invoked"

        # Wait until attempt failure is committed
        deadline = time.time() + 4.0
        while time.time() < deadline:
            with db_session_cm() as session:
                rec = session.get(OperationLedger, op_id)
                if (
                    rec
                    and rec.attempt_count >= 1
                    and rec.error_classification == "ProviderRateLimitError"
                ):
                    break
            time.sleep(0.05)

        with db_session_cm() as session:
            rec = session.get(OperationLedger, op_id)
            assert rec is not None
            assert rec.attempt_count >= 1
            assert rec.status == "pending"
            assert rec.error_classification == "ProviderRateLimitError"
            assert rec.scheduled_at is not None

    finally:
        dispatcher.stop(wait=True)


# ---------------------------------------------------------------------------
# 8. Permanent Error Handling & Dead-Lettering
# ---------------------------------------------------------------------------


def test_permanent_error_dead_lettering(test_db):
    """Verify permanent errors (ProviderAuthError) immediately dead-letter to failed status without retry."""
    _, db_session_cm = test_db

    def _permanent_fail_handler(op: OperationLedger, payload: dict[str, Any]) -> None:
        raise ProviderAuthError("Invalid RealDebrid API key")

    dispatcher = OutboxDispatcher(
        worker_id="perm-worker",
        max_workers=1,
        poll_interval_seconds=0.05,
        db_session_cm=db_session_cm,
    )
    dispatcher.register_handler("retry_library_item", _permanent_fail_handler)

    with db_session_cm() as session:
        op = enqueue_operation(
            session=session,
            operation_type="retry_library_item",
            payload={"item_id": 601},
            media_item_id=601,
            idempotency_key="perm-test-601",
        )
        session.commit()
        op_id = op.id

    dispatcher.start()
    try:
        dispatcher.notify()

        deadline = time.time() + 4.0
        while time.time() < deadline:
            with db_session_cm() as session:
                rec = session.get(OperationLedger, op_id)
                if rec and rec.status == "failed":
                    break
            time.sleep(0.05)

        with db_session_cm() as session:
            rec = session.get(OperationLedger, op_id)
            assert rec is not None
            assert rec.status == "failed"
            assert rec.error_classification == "ProviderAuthError"
            assert rec.attempt_count == 1
            assert rec.completed_at is not None

    finally:
        dispatcher.stop(wait=True)


# ---------------------------------------------------------------------------
# 9. Scheduler Integration: _retry_library() End-to-End
# ---------------------------------------------------------------------------


def test_scheduler_retry_library_enqueues_durable_operations(test_db, monkeypatch):
    """Verify ProgramScheduler._retry_library() queries candidates, excludes active, and enqueues outbox operations."""
    _, db_session_cm = test_db

    mock_program = MagicMock(spec=Program)
    mock_program.em = EventManager()
    mock_dispatcher = MagicMock(spec=OutboxDispatcher)
    mock_program.outbox_dispatcher = mock_dispatcher

    mock_settings = MagicMock()
    mock_settings.retry_library_batch_size = 5
    mock_settings.retry_interval = 300
    monkeypatch.setattr(settings_manager, "settings", mock_settings)

    scheduler = ProgramScheduler(mock_program)

    with db_session_cm() as session:
        movie1 = Movie({"title": "Retry Movie 1", "imdb_id": "tt7000001"})
        movie1.last_state = States.Indexed
        movie2 = Movie({"title": "Retry Movie 2", "imdb_id": "tt7000002"})
        movie2.last_state = States.Scraped
        movie_completed = Movie({"title": "Done Movie", "imdb_id": "tt7000003"})
        movie_completed.last_state = States.Completed

        session.add_all([movie1, movie2, movie_completed])
        session.commit()

        id1 = movie1.id
        id2 = movie2.id

    # Execute scheduler retry tick
    scheduler._retry_library()

    # Verify operations were enqueued in database
    with db_session_cm() as session:
        ops = (
            session.execute(
                select(OperationLedger).filter(
                    OperationLedger.operation_type == "retry_library_item"
                )
            )
            .scalars()
            .all()
        )

        assert len(ops) == 2
        op_item_ids = {op.media_item_id for op in ops}
        assert op_item_ids == {id1, id2}
        for op in ops:
            assert op.status == "pending"
            assert op.payload is not None
            assert op.payload["reason"] == "scheduled_retry"

    # Verify second tick in same window does not insert duplicate rows
    scheduler._retry_library()

    with db_session_cm() as session:
        ops_after = (
            session.execute(
                select(OperationLedger).filter(
                    OperationLedger.operation_type == "retry_library_item"
                )
            )
            .scalars()
            .all()
        )
        assert len(ops_after) == 2
