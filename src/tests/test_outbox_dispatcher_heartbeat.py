"""Active lease heartbeat tests for ``OutboxDispatcher``."""

from __future__ import annotations

import os
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from program.contracts import dispatcher as dispatcher_module
from program.contracts.dispatcher import OutboxDispatcher
from program.contracts.errors import ProviderAuthError
from program.contracts.operation_ledger import OperationLedger, enqueue_operation
from program.db.base_model import get_base_metadata


@pytest.fixture
def heartbeat_db() -> Iterator[tuple[sessionmaker[Session], Callable[[], Any]]]:
    """Provide isolated file-backed SQLite WAL sessions for heartbeat tests."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp_file:
        db_path = tmp_file.name

    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"timeout": 30.0, "check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_conn: Any, _: Any) -> None:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.close()

    get_base_metadata().create_all(engine)
    testing_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    @contextmanager
    def session_context() -> Iterator[Session]:
        session = testing_session()
        try:
            yield session
        finally:
            session.close()

    try:
        yield testing_session, session_context
    finally:
        engine.dispose()
        for path in (db_path, f"{db_path}-shm", f"{db_path}-wal"):
            if os.path.exists(path):
                os.remove(path)


def _enqueue(testing_session: sessionmaker[Session], operation_type: str) -> str:
    with testing_session() as session:
        operation = enqueue_operation(
            session, operation_type=operation_type, payload={}
        )
        session.commit()
        return operation.id


def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _dispatcher(
    session_context: Callable[[], Any], *, worker_id: str = "heartbeat-worker"
) -> OutboxDispatcher:
    return OutboxDispatcher(
        worker_id=worker_id,
        max_workers=2,
        poll_interval_seconds=0.01,
        lease_duration_seconds=1,
        db_session_cm=session_context,
    )


def test_heartbeat_interval_is_one_third_of_lease_duration(heartbeat_db) -> None:
    """The governed heartbeat interval is exactly one third of the configured lease."""
    _, session_context = heartbeat_db
    dispatcher = OutboxDispatcher(
        lease_duration_seconds=9, db_session_cm=session_context
    )

    assert dispatcher._heartbeat_interval_seconds == 3.0


def test_long_running_operation_renews_its_fenced_lease(heartbeat_db) -> None:
    """A handler lasting beyond one interval renews its committed database lease."""
    testing_session, session_context = heartbeat_db
    started = threading.Event()
    release = threading.Event()
    dispatcher = _dispatcher(session_context)

    def handler(_operation: OperationLedger, _payload: dict[str, Any]) -> None:
        started.set()
        assert release.wait(timeout=5.0)

    dispatcher.register_handler("heartbeat.renew", handler)
    operation_id = _enqueue(testing_session, "heartbeat.renew")
    dispatcher.start()
    try:
        assert started.wait(timeout=5.0)
        with testing_session() as session:
            first_expiry = session.get(OperationLedger, operation_id).lease_expires_at
        assert _wait_until(
            lambda: _lease_expiry_after(testing_session, operation_id, first_expiry),
            timeout=3.0,
        )
    finally:
        release.set()
        dispatcher.stop(wait=True)


def test_heartbeat_uses_claim_worker_and_token_fencing(
    heartbeat_db, monkeypatch
) -> None:
    """Every renewal passes the owning worker ID and claim token unchanged."""
    testing_session, session_context = heartbeat_db
    renewal_calls: list[tuple[str, str, str, int]] = []
    original = dispatcher_module.renew_lease
    started = threading.Event()
    release = threading.Event()
    dispatcher = _dispatcher(session_context, worker_id="fenced-heartbeat-worker")

    def recording_renewal(
        session, operation_id, worker_id, claim_token, *, extension_seconds=300
    ):
        renewal_calls.append((operation_id, worker_id, claim_token, extension_seconds))
        return original(
            session,
            operation_id,
            worker_id,
            claim_token,
            extension_seconds=extension_seconds,
        )

    monkeypatch.setattr(dispatcher_module, "renew_lease", recording_renewal)
    dispatcher.register_handler(
        "heartbeat.fenced", lambda _op, _payload: (started.set(), release.wait(5.0))
    )
    operation_id = _enqueue(testing_session, "heartbeat.fenced")
    dispatcher.start()
    try:
        assert started.wait(timeout=5.0)
        assert _wait_until(lambda: bool(renewal_calls), timeout=3.0)
        with testing_session() as session:
            operation = session.get(OperationLedger, operation_id)
            assert operation is not None
            assert renewal_calls[0] == (
                operation_id,
                "fenced-heartbeat-worker",
                operation.claim_token,
                1,
            )
    finally:
        release.set()
        dispatcher.stop(wait=True)


def test_normal_completion_stops_heartbeat(heartbeat_db, monkeypatch) -> None:
    """A completed operation leaves no heartbeat registration or later renewal calls."""
    testing_session, session_context = heartbeat_db
    renewals: list[str] = []
    original = dispatcher_module.renew_lease
    dispatcher = _dispatcher(session_context)

    def recording_renewal(*args, **kwargs):
        renewals.append(args[1])
        return original(*args, **kwargs)

    monkeypatch.setattr(dispatcher_module, "renew_lease", recording_renewal)
    done = threading.Event()
    dispatcher.register_handler("heartbeat.complete", lambda _op, _payload: done.set())
    operation_id = _enqueue(testing_session, "heartbeat.complete")
    dispatcher.start()
    try:
        assert done.wait(timeout=5.0)
        assert _wait_until(
            lambda: _status_is(testing_session, operation_id, "completed")
        )
        time.sleep(0.5)
        assert renewals == []
        assert dispatcher._lease_heartbeats == {}
    finally:
        dispatcher.stop(wait=True)


def test_failure_stops_heartbeat(heartbeat_db) -> None:
    """A handler exception is fenced into failure without leaving a heartbeat behind."""
    testing_session, session_context = heartbeat_db
    dispatcher = _dispatcher(session_context)
    dispatcher.register_handler(
        "heartbeat.failure",
        lambda _op, _payload: (_ for _ in ()).throw(ProviderAuthError("invalid")),
    )
    operation_id = _enqueue(testing_session, "heartbeat.failure")
    dispatcher.start()
    try:
        assert _wait_until(lambda: _status_is(testing_session, operation_id, "failed"))
        assert dispatcher._lease_heartbeats == {}
    finally:
        dispatcher.stop(wait=True)


def test_lost_ownership_stops_renewing_without_unfenced_writes(
    heartbeat_db, monkeypatch
) -> None:
    """A failed fenced renewal is terminal for that heartbeat and causes no extra mutations."""
    testing_session, session_context = heartbeat_db
    started = threading.Event()
    release = threading.Event()
    calls = 0
    dispatcher = _dispatcher(session_context)

    def denied_renewal(*_args, **_kwargs) -> bool:
        nonlocal calls
        calls += 1
        return False

    monkeypatch.setattr(dispatcher_module, "renew_lease", denied_renewal)
    dispatcher.register_handler(
        "heartbeat.lost", lambda _op, _payload: (started.set(), release.wait(5.0))
    )
    _enqueue(testing_session, "heartbeat.lost")
    dispatcher.start()
    try:
        assert started.wait(timeout=5.0)
        assert _wait_until(lambda: calls == 1, timeout=3.0)
        time.sleep(0.5)
        assert calls == 1
    finally:
        release.set()
        dispatcher.stop(wait=True)


def test_renewal_exception_stops_heartbeat(heartbeat_db, monkeypatch) -> None:
    """A database renewal exception stops the cooperative heartbeat instead of retrying unfenced."""
    testing_session, session_context = heartbeat_db
    started = threading.Event()
    release = threading.Event()
    calls = 0
    dispatcher = _dispatcher(session_context)

    def exploding_renewal(*_args, **_kwargs) -> bool:
        nonlocal calls
        calls += 1
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(dispatcher_module, "renew_lease", exploding_renewal)
    dispatcher.register_handler(
        "heartbeat.exception", lambda _op, _payload: (started.set(), release.wait(5.0))
    )
    _enqueue(testing_session, "heartbeat.exception")
    dispatcher.start()
    try:
        assert started.wait(timeout=5.0)
        assert _wait_until(lambda: calls == 1, timeout=3.0)
        time.sleep(0.5)
        assert calls == 1
    finally:
        release.set()
        dispatcher.stop(wait=True)


def test_shutdown_stops_and_joins_active_heartbeats(heartbeat_db) -> None:
    """Dispatcher shutdown signals all active heartbeat threads before returning."""
    testing_session, session_context = heartbeat_db
    started = threading.Event()
    dispatcher = _dispatcher(session_context)

    def shutdown_aware_handler(
        _operation: OperationLedger, _payload: dict[str, Any]
    ) -> None:
        started.set()
        while not dispatcher._stop_event.wait(timeout=0.01):
            pass

    dispatcher.register_handler("heartbeat.shutdown", shutdown_aware_handler)
    _enqueue(testing_session, "heartbeat.shutdown")
    dispatcher.start()
    try:
        assert started.wait(timeout=5.0)
        assert _wait_until(lambda: bool(dispatcher._lease_heartbeats))
        dispatcher.stop(wait=True)
        assert dispatcher._lease_heartbeats == {}
    finally:
        dispatcher.stop(wait=True)


def test_concurrent_operations_receive_independent_heartbeats(
    heartbeat_db, monkeypatch
) -> None:
    """Two long-running operations each receive independent fenced renewals."""
    testing_session, session_context = heartbeat_db
    started = threading.Event()
    release = threading.Event()
    renewed_ids: set[str] = set()
    original = dispatcher_module.renew_lease
    dispatcher = _dispatcher(session_context)

    def recording_renewal(
        session, operation_id, worker_id, claim_token, *, extension_seconds=300
    ):
        renewed_ids.add(operation_id)
        return original(
            session,
            operation_id,
            worker_id,
            claim_token,
            extension_seconds=extension_seconds,
        )

    monkeypatch.setattr(dispatcher_module, "renew_lease", recording_renewal)
    active_handlers = 0
    handler_lock = threading.Lock()

    def handler(_operation: OperationLedger, _payload: dict[str, Any]) -> None:
        nonlocal active_handlers
        with handler_lock:
            active_handlers += 1
            if active_handlers == 2:
                started.set()
        assert release.wait(timeout=5.0)

    dispatcher.register_handler("heartbeat.parallel", handler)
    operation_ids = {_enqueue(testing_session, "heartbeat.parallel") for _ in range(2)}
    dispatcher.start()
    try:
        assert started.wait(timeout=5.0)
        assert _wait_until(lambda: renewed_ids == operation_ids, timeout=3.0)
    finally:
        release.set()
        dispatcher.stop(wait=True)


def test_dispatcher_without_claims_creates_no_heartbeat(heartbeat_db) -> None:
    """Idle polling never creates a lease heartbeat."""
    _, session_context = heartbeat_db
    dispatcher = _dispatcher(session_context)
    dispatcher.start()
    try:
        time.sleep(0.1)
        assert dispatcher._lease_heartbeats == {}
    finally:
        dispatcher.stop(wait=True)


def _lease_expiry_after(
    testing_session: sessionmaker[Session],
    operation_id: str,
    previous_expiry: datetime | None,
) -> bool:
    with testing_session() as session:
        operation = session.get(OperationLedger, operation_id)
        assert operation is not None
        return (
            operation.lease_expires_at is not None
            and operation.lease_expires_at > previous_expiry
        )


def _status_is(
    testing_session: sessionmaker[Session], operation_id: str, status: str
) -> bool:
    with testing_session() as session:
        operation = session.get(OperationLedger, operation_id)
        return operation is not None and operation.status == status
