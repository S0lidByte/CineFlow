"""File-backed SQLite WAL concurrency certification for the outbox ledger.

Governed scenarios:
1. 2 workers / 1 item -> exactly one claim.
2. 10 workers / 1 item -> exactly one claim.
3. 10 workers / N items -> each item claimed at most once.
4. Expired lease -> reclaimed with a new claim token.
5. Stale worker completion -> rejected.
6. Stale worker failure -> rejected.
7. Stale worker renewal -> rejected.
8. Current owner completion -> succeeds.
9. Current owner renewal -> succeeds.
10. Admin retry racing with processing -> HTTP 400 and ownership unchanged.

Additional retained coverage certifies FIFO ordering, WAL lock contention, active-lease
protection, and concurrent terminal dead-letter routing.
"""

import os
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
import sqlalchemy
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

import auth
from program.contracts.operation_ledger import (
    OperationLedger,
    claim_due_operations,
    complete_operation,
    enqueue_operation,
    fail_operation,
    renew_lease,
)
from program.db.base_model import get_base_metadata
from routers.secure.operations import router as operations_router


@pytest.fixture
def sqlite_file_db():
    """Create a temporary file-backed SQLite database configured with WAL mode and 10s busy timeout."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp_file:
        db_path = tmp_file.name

    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"timeout": 15},
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.close()

    get_base_metadata().create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    yield engine, session_factory

    engine.dispose()
    for suffix in ["", "-wal", "-shm"]:
        p = f"{db_path}{suffix}"
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass


class TestSQLiteOutboxConcurrency:
    """Certifies atomic claiming and strict fencing on file-backed SQLite in WAL mode."""

    @staticmethod
    def _race_claims(
        session_factory, *, workers: int, limit: int
    ) -> list[tuple[str, str, str]]:
        barrier = threading.Barrier(workers)

        def claim(worker_number: int) -> list[tuple[str, str, str]]:
            worker_id = f"race-worker-{worker_number}"
            barrier.wait()
            with session_factory() as session:
                operations = claim_due_operations(
                    session,
                    worker_id=worker_id,
                    limit=limit,
                    lease_duration_seconds=60,
                )
                results = [
                    (op.id, worker_id, op.claim_token or "") for op in operations
                ]
                session.commit()
                return results

        with ThreadPoolExecutor(max_workers=workers) as executor:
            return [
                claim_result
                for future in [
                    executor.submit(claim, index) for index in range(workers)
                ]
                for claim_result in future.result()
            ]

    def test_governed_01_two_workers_one_item_exactly_one_claim(self, sqlite_file_db):
        """Governed scenario 1: 2 workers racing for 1 item yield exactly one claim."""
        _, session_factory = sqlite_file_db
        with session_factory() as session:
            operation = enqueue_operation(session, "single-two", {})
            session.commit()
            operation_id = operation.id

        claims = self._race_claims(session_factory, workers=2, limit=1)

        assert len(claims) == 1
        assert claims[0][0] == operation_id
        assert claims[0][2]

    def test_governed_02_ten_workers_one_item_exactly_one_claim(self, sqlite_file_db):
        """Governed scenario 2: 10 workers racing for 1 item yield exactly one claim."""
        _, session_factory = sqlite_file_db
        with session_factory() as session:
            operation = enqueue_operation(session, "single-ten", {})
            session.commit()
            operation_id = operation.id

        claims = self._race_claims(session_factory, workers=10, limit=1)

        assert len(claims) == 1
        assert claims[0][0] == operation_id
        assert claims[0][2]

    def test_governed_03_ten_workers_n_items_claim_each_at_most_once(
        self, sqlite_file_db
    ):
        """Governed scenario 3: 10 workers claim N items without duplicate ownership."""
        _, session_factory = sqlite_file_db
        operation_count = 50
        with session_factory() as session:
            for index in range(operation_count):
                enqueue_operation(session, "many-ten", {"index": index})
            session.commit()

        claims = self._race_claims(session_factory, workers=10, limit=operation_count)
        claimed_ids = [operation_id for operation_id, _, _ in claims]
        claim_tokens = [claim_token for _, _, claim_token in claims]

        assert len(claimed_ids) == operation_count
        assert len(claimed_ids) == len(set(claimed_ids))
        assert len(claim_tokens) == len(set(claim_tokens))

    def test_additional_fifo_claim_ordering(self, sqlite_file_db):
        """Additional coverage: operations are claimed FIFO by scheduled_at."""
        _, session_factory = sqlite_file_db

        base_time = datetime.now(UTC)
        with session_factory() as session:
            op_3 = enqueue_operation(
                session,
                "debrid",
                {"task": 3},
                scheduled_at=base_time - timedelta(seconds=10),
            )
            op_1 = enqueue_operation(
                session,
                "debrid",
                {"task": 1},
                scheduled_at=base_time - timedelta(seconds=30),
            )
            op_2 = enqueue_operation(
                session,
                "debrid",
                {"task": 2},
                scheduled_at=base_time - timedelta(seconds=20),
            )
            session.commit()
            id1, id2, id3 = op_1.id, op_2.id, op_3.id

        with session_factory() as session:
            claimed = claim_due_operations(
                session, worker_id="worker-fifo", limit=10, lease_duration_seconds=60
            )
            session.commit()
            claimed_ids = [c.id for c in claimed]

        assert claimed_ids == [id1, id2, id3]

    def test_additional_optimistic_concurrency_lock_contention_wal(
        self, sqlite_file_db
    ):
        """Scenario 3: Concurrent claim and complete operations proceed without DB lock errors."""
        _, session_factory = sqlite_file_db
        num_ops = 40
        num_workers = 8

        with session_factory() as session:
            for i in range(num_ops):
                enqueue_operation(session, "index", {"i": i})
            session.commit()

        completed_count = 0
        lock = threading.Lock()

        def worker_loop(w_idx: int):
            nonlocal completed_count
            worker_id = f"worker-wal-{w_idx}"
            for _ in range(10):
                with session_factory() as session:
                    claimed = claim_due_operations(
                        session, worker_id=worker_id, limit=3, lease_duration_seconds=30
                    )
                    session.commit()

                    for op in claimed:
                        # complete right away
                        res = complete_operation(
                            session,
                            op.id,
                            worker_id=worker_id,
                            claim_token=op.claim_token,
                        )
                        session.commit()
                        if res is not None:
                            with lock:
                                completed_count += 1

        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = [executor.submit(worker_loop, w) for w in range(num_workers)]
            for f in as_completed(futures):
                f.result()

        with session_factory() as session:
            remaining = (
                session.query(OperationLedger)
                .filter(OperationLedger.status == "pending")
                .count()
            )
            completed_in_db = (
                session.query(OperationLedger)
                .filter(OperationLedger.status == "completed")
                .count()
            )

        assert completed_in_db == completed_count
        assert completed_in_db + remaining == num_ops

    def test_governed_04_expired_lease_reclaimed_with_new_token(self, sqlite_file_db):
        """Governed scenario 4: an expired lease is reclaimed with a new claim token."""
        _, session_factory = sqlite_file_db

        with session_factory() as session:
            op = enqueue_operation(session, "stream_cache", {"item": 1})
            session.commit()
            op_id = op.id

        # Worker 1 claims with very short lease (1s)
        with session_factory() as session:
            claimed_w1 = claim_due_operations(
                session, worker_id="worker-dead", limit=1, lease_duration_seconds=1
            )
            session.commit()
            assert len(claimed_w1) == 1
            w1_token = claimed_w1[0].claim_token

        # Artificially age the lease to expire it in DB
        with session_factory() as session:
            record = session.get(OperationLedger, op_id)
            assert record is not None
            record.lease_expires_at = datetime.now(UTC) - timedelta(seconds=10)
            session.commit()

        # Worker 2 attempts claim; should succeed and acquire new claim_token
        with session_factory() as session:
            claimed_w2 = claim_due_operations(
                session, worker_id="worker-revived", limit=1, lease_duration_seconds=60
            )
            session.commit()
            assert len(claimed_w2) == 1
            w2_token = claimed_w2[0].claim_token

        assert w2_token != w1_token
        assert claimed_w2[0].worker_id == "worker-revived"

    def test_additional_active_lease_protection(self, sqlite_file_db):
        """Additional coverage: an active, unexpired lease cannot be reclaimed."""
        _, session_factory = sqlite_file_db

        with session_factory() as session:
            op = enqueue_operation(session, "sync", {"x": 1})
            session.commit()
            op_id = op.id

        # Worker 1 claims with 120s lease
        with session_factory() as session:
            claimed_w1 = claim_due_operations(
                session, worker_id="worker-active", limit=1, lease_duration_seconds=120
            )
            session.commit()
            assert len(claimed_w1) == 1

        # Worker 2 attempts claim; must get nothing
        with session_factory() as session:
            claimed_w2 = claim_due_operations(
                session,
                worker_id="worker-interloper",
                limit=10,
                lease_duration_seconds=60,
            )
            session.commit()
            assert len(claimed_w2) == 0

    def test_governed_05_stale_worker_completion_rejected_and_08_owner_succeeds(
        self, sqlite_file_db
    ):
        """Governed scenarios 5 and 8: stale completion fails; the current owner succeeds."""
        _, session_factory = sqlite_file_db

        with session_factory() as session:
            op = enqueue_operation(session, "meta_refresh", {"target": "show"})
            session.commit()
            op_id = op.id

        with session_factory() as session:
            claimed = claim_due_operations(
                session, worker_id="worker-real", limit=1, lease_duration_seconds=60
            )
            session.commit()
            real_token = claimed[0].claim_token

        with session_factory() as session:
            # 1. Wrong worker, right token
            res1 = complete_operation(
                session, op_id, worker_id="worker-fake", claim_token=real_token
            )
            assert res1 is None

            # 2. Right worker, wrong token
            res2 = complete_operation(
                session, op_id, worker_id="worker-real", claim_token="stale-uuid-token"
            )
            assert res2 is None

            # 3. Right worker, right token -> Success
            res3 = complete_operation(
                session, op_id, worker_id="worker-real", claim_token=real_token
            )
            session.commit()
            assert res3 is not None
            assert res3.status == "completed"

    def test_governed_06_stale_worker_failure_rejected(self, sqlite_file_db):
        """Governed scenario 6: stale worker failure is rejected without mutation."""
        _, session_factory = sqlite_file_db

        with session_factory() as session:
            op = enqueue_operation(session, "download", {"torrent_id": 42})
            session.commit()
            op_id = op.id

        with session_factory() as session:
            claimed = claim_due_operations(
                session,
                worker_id="worker-downloader",
                limit=1,
                lease_duration_seconds=60,
            )
            session.commit()
            token = claimed[0].claim_token

        with session_factory() as session:
            # Stale fail attempt by impostor
            res_impostor = fail_operation(
                session,
                op_id,
                worker_id="worker-impostor",
                claim_token=token,
                error_classification="NetworkError",
                error_message="Boom",
            )
            assert res_impostor is None

            # Valid fail attempt by rightful worker
            res_real = fail_operation(
                session,
                op_id,
                worker_id="worker-downloader",
                claim_token=token,
                error_classification="NetworkError",
                error_message="Real error",
                next_retry_at=datetime.now(UTC) + timedelta(seconds=30),
            )
            session.commit()
            assert res_real is not None
            assert res_real.status == "pending"

    def test_governed_09_current_owner_renewal_succeeds(self, sqlite_file_db):
        """Governed scenario 9: current owner renewal extends lease expiry."""
        _, session_factory = sqlite_file_db

        with session_factory() as session:
            op = enqueue_operation(session, "long_job", {"steps": 100})
            session.commit()
            op_id = op.id

        with session_factory() as session:
            claimed = claim_due_operations(
                session, worker_id="worker-long", limit=1, lease_duration_seconds=30
            )
            session.commit()
            token = claimed[0].claim_token
            initial_lease = claimed[0].lease_expires_at

        with session_factory() as session:
            renewed = renew_lease(
                session,
                op_id,
                worker_id="worker-long",
                claim_token=token,
                extension_seconds=120,
            )
            session.commit()
            assert renewed is True

            record = session.get(OperationLedger, op_id)
            assert record is not None
            assert record.lease_expires_at > initial_lease

    def test_governed_07_stale_worker_renewal_rejected(self, sqlite_file_db):
        """Governed scenario 7: stale owner cannot renew after lease reclamation."""
        _, session_factory = sqlite_file_db

        with session_factory() as session:
            op = enqueue_operation(session, "stolen_job", {})
            session.commit()
            op_id = op.id

        # Worker 1 claims
        with session_factory() as session:
            claimed_w1 = claim_due_operations(
                session, worker_id="worker-slow", limit=1, lease_duration_seconds=1
            )
            session.commit()
            w1_token = claimed_w1[0].claim_token

        # Expire worker 1 lease
        with session_factory() as session:
            rec = session.get(OperationLedger, op_id)
            assert rec is not None
            rec.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            session.commit()

        # Worker 2 steals job
        with session_factory() as session:
            claimed_w2 = claim_due_operations(
                session, worker_id="worker-fast", limit=1, lease_duration_seconds=60
            )
            session.commit()
            assert len(claimed_w2) == 1

        # Worker 1 attempts to renew using old token and worker_id -> MUST FAIL
        with session_factory() as session:
            renew_result = renew_lease(
                session,
                op_id,
                worker_id="worker-slow",
                claim_token=w1_token,
                extension_seconds=60,
            )
            session.commit()
            assert renew_result is False

    def test_governed_10_retry_racing_processing_returns_400_and_preserves_owner(
        self, sqlite_file_db, monkeypatch
    ):
        """Governed scenario 10: admin retry cannot steal a processing operation."""
        _, session_factory = sqlite_file_db
        owner_id = "worker-processing-owner"

        with session_factory() as session:
            operation = enqueue_operation(session, "retry-race", {})
            session.commit()
            operation_id = operation.id

        with session_factory() as session:
            claimed = claim_due_operations(
                session,
                worker_id=owner_id,
                limit=1,
                lease_duration_seconds=60,
            )
            session.commit()
            assert len(claimed) == 1
            owner_token = claimed[0].claim_token
            original_lease = claimed[0].lease_expires_at

        @contextmanager
        def independent_session():
            session = session_factory()
            try:
                yield session
            finally:
                session.close()

        monkeypatch.setattr("routers.secure.operations.db_session", independent_session)
        monkeypatch.setattr(
            auth.settings_manager, "settings", MagicMock(api_key="r" * 32)
        )
        app = FastAPI()
        app.include_router(operations_router, prefix="/api/v1")
        client = TestClient(app)
        barrier = threading.Barrier(2)

        def retry_attempt():
            barrier.wait()
            return client.post(
                f"/api/v1/operations/timeline/{operation_id}/retry",
                headers={"x-api-key": "r" * 32},
            )

        def owner_observation():
            barrier.wait()
            with session_factory() as session:
                operation = session.get(OperationLedger, operation_id)
                assert operation is not None
                return operation.status, operation.worker_id, operation.claim_token

        with ThreadPoolExecutor(max_workers=2) as executor:
            retry_future = executor.submit(retry_attempt)
            owner_future = executor.submit(owner_observation)
            retry_response = retry_future.result()
            observed = owner_future.result()

        assert retry_response.status_code == 400
        assert observed == ("processing", owner_id, owner_token)
        with session_factory() as session:
            unchanged = session.get(OperationLedger, operation_id)
            assert unchanged is not None
            assert unchanged.status == "processing"
            assert unchanged.worker_id == owner_id
            assert unchanged.claim_token == owner_token
            assert unchanged.lease_expires_at == original_lease

    def test_additional_dead_letter_routing_under_concurrency(self, sqlite_file_db):
        """Additional coverage: concurrent terminal failures route to failed status."""
        _, session_factory = sqlite_file_db
        num_terminal_ops = 20

        with session_factory() as session:
            for i in range(num_terminal_ops):
                enqueue_operation(session, "unrecoverable_op", {"i": i})
            session.commit()

        def fail_worker(w_idx: int):
            worker_id = f"worker-dlq-{w_idx}"
            while True:
                with session_factory() as session:
                    claimed = claim_due_operations(
                        session, worker_id=worker_id, limit=5, lease_duration_seconds=30
                    )
                    session.commit()

                    if not claimed:
                        break

                    for op in claimed:
                        # Mark terminal failure (next_retry_at=None)
                        res = fail_operation(
                            session,
                            op.id,
                            worker_id=worker_id,
                            claim_token=op.claim_token,
                            error_classification="PermanentCorruptionError",
                            error_message="Cannot process payload",
                            next_retry_at=None,
                        )
                        session.commit()

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(fail_worker, w) for w in range(5)]
            for f in as_completed(futures):
                f.result()

        with session_factory() as session:
            failed_records = (
                session.query(OperationLedger)
                .filter(OperationLedger.status == "failed")
                .all()
            )
            assert len(failed_records) == num_terminal_ops
            for rec in failed_records:
                assert rec.error_classification == "PermanentCorruptionError"
                assert rec.lease_expires_at is None
                assert rec.completed_at is not None
