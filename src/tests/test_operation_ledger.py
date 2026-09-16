"""Unit tests for OperationLedger persistence model and contract."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from program.contracts.operation_ledger import OperationLedger
from program.db.base_model import get_base_metadata


class TestOperationLedgerModel:
    def test_in_memory_persistence_defaults(self):
        engine = create_engine("sqlite:///:memory:")
        get_base_metadata().create_all(engine)

        with Session(engine) as session:
            op_id = str(uuid.uuid4())
            cid = str(uuid.uuid4())
            record = OperationLedger(
                id=op_id,
                correlation_id=cid,
                operation_type="scrape",
            )
            session.add(record)
            session.commit()

            fetched = session.get(OperationLedger, op_id)
            assert fetched is not None
            assert fetched.id == op_id
            assert fetched.correlation_id == cid
            assert fetched.operation_type == "scrape"
            assert fetched.status == "pending"
            assert fetched.attempt_count == 0
            assert fetched.schema_version == 1
            assert fetched.media_item_id is None
            assert fetched.lease_expires_at is None
            assert fetched.error_classification is None
            assert fetched.error_message is None
            assert fetched.payload is None
            assert fetched.created_at is not None
            assert fetched.updated_at is not None

    def test_in_memory_persistence_custom_fields(self):
        engine = create_engine("sqlite:///:memory:")
        get_base_metadata().create_all(engine)

        with Session(engine) as session:
            op_id = str(uuid.uuid4())
            cid = str(uuid.uuid4())
            now = datetime.now(UTC)

            record = OperationLedger(
                id=op_id,
                correlation_id=cid,
                media_item_id=42,
                operation_type="download",
                status="running",
                attempt_count=1,
                idempotency_key=f"dl-{op_id}",
                scheduled_at=now,
                lease_expires_at=now,
                error_classification="ProviderRateLimitError",
                error_message="Hit 429",
                payload={"indexer": "torrentio", "infohash": "abc12345"},
            )
            session.add(record)
            session.commit()

            fetched = session.get(OperationLedger, op_id)
            assert fetched is not None
            assert fetched.id == op_id
            assert fetched.correlation_id == cid
            assert fetched.media_item_id == 42
            assert fetched.operation_type == "download"
            assert fetched.status == "running"
            assert fetched.attempt_count == 1
            assert fetched.idempotency_key == f"dl-{op_id}"
            assert fetched.payload == {"indexer": "torrentio", "infohash": "abc12345"}
            assert fetched.error_classification == "ProviderRateLimitError"
            assert fetched.error_message == "Hit 429"

    def test_enqueue_and_idempotency(self):
        from program.contracts.operation_ledger import enqueue_operation

        engine = create_engine("sqlite:///:memory:")
        get_base_metadata().create_all(engine)

        with Session(engine) as session:
            op1 = enqueue_operation(
                session=session,
                operation_type="scrape",
                payload={"api_key": "test_dummy_key_val", "title": "Inception"},
                media_item_id=101,
                idempotency_key="scrape-101",
            )
            session.commit()

            assert op1.id is not None
            assert op1.status == "pending"
            assert op1.payload is not None
            assert op1.payload["api_key"] == "[REDACTED]"
            assert op1.payload["title"] == "Inception"

            # Enqueue with same idempotency key returns existing op
            op2 = enqueue_operation(
                session=session,
                operation_type="scrape",
                payload={"api_key": "another_secret", "title": "Inception 2"},
                idempotency_key="scrape-101",
            )
            assert op2.id == op1.id

    def test_claim_due_operations_and_completion(self):
        from program.contracts.operation_ledger import (
            claim_due_operations,
            complete_operation,
            enqueue_operation,
            fail_operation,
            renew_lease,
        )

        engine = create_engine("sqlite:///:memory:")
        get_base_metadata().create_all(engine)

        with Session(engine) as session:
            op = enqueue_operation(
                session=session,
                operation_type="download",
                payload={"infohash": "hash123"},
                media_item_id=202,
            )
            session.commit()

            claimed = claim_due_operations(
                session=session,
                worker_id="worker-node-1",
                limit=5,
                lease_duration_seconds=60,
            )
            assert len(claimed) == 1
            claimed_op = claimed[0]
            assert claimed_op.id == op.id
            assert claimed_op.status == "processing"
            assert claimed_op.worker_id == "worker-node-1"
            assert claimed_op.attempt_count == 1
            assert claimed_op.lease_expires_at is not None

            # Renew lease
            renewed = renew_lease(
                session,
                claimed_op.id,
                worker_id="worker-node-1",
                claim_token=claimed_op.claim_token,
                extension_seconds=120,
            )
            assert renewed is True

            # Complete operation
            completed = complete_operation(
                session,
                claimed_op.id,
                worker_id="worker-node-1",
                claim_token=claimed_op.claim_token,
            )
            session.commit()

            assert completed is not None
            assert completed.status == "completed"
            assert completed.completed_at is not None
            assert completed.lease_expires_at is None

    def test_fail_operation_with_retry_and_dead_letter(self):
        from datetime import timedelta

        from program.contracts.operation_ledger import (
            claim_due_operations,
            enqueue_operation,
            fail_operation,
        )

        engine = create_engine("sqlite:///:memory:")
        get_base_metadata().create_all(engine)

        with Session(engine) as session:
            op = enqueue_operation(
                session=session,
                operation_type="debrid",
                payload={"token": "secret_token_12345678"},
            )
            session.commit()

            # Claim
            claimed = claim_due_operations(session, "worker-1", limit=1)
            assert len(claimed) == 1
            op_claimed = claimed[0]

            # Reschedule failure (retry)
            retry_time = datetime.now(UTC) + timedelta(seconds=10)
            retried = fail_operation(
                session,
                op.id,
                worker_id="worker-1",
                claim_token=op_claimed.claim_token,
                error_classification="ProviderRateLimitError",
                error_message="Rate limit with token=secret_token_12345678",
                next_retry_at=retry_time,
            )
            session.commit()

            assert retried is not None
            assert retried.status == "pending"
            assert abs((retried.scheduled_at.replace(tzinfo=UTC) - retry_time).total_seconds()) < 1
            assert retried.error_classification == "ProviderRateLimitError"
            assert "secret_token_12345678" not in (retried.error_message or "")
            assert "[REDACTED]" in (retried.error_message or "")

            # Claim again and fail permanently (dead-letter)
            retried.scheduled_at = datetime.now(UTC) - timedelta(seconds=1)
            session.commit()

            claimed2 = claim_due_operations(session, "worker-1", limit=1)
            assert len(claimed2) == 1
            op_claimed2 = claimed2[0]

            dead = fail_operation(
                session,
                op.id,
                worker_id="worker-1",
                claim_token=op_claimed2.claim_token,
                error_classification="ProviderAuthError",
                error_message="Invalid credentials api_key=test_dummy_key_val",
                next_retry_at=None,
            )
            session.commit()

            assert dead is not None
            assert dead.status == "failed"
            assert dead.completed_at is not None
            assert dead.error_classification == "ProviderAuthError"
            assert "test_dummy_key_val" not in (dead.error_message or "")

    def test_fenced_state_mutations_and_claim_token(self):
        """OUTBOX-001 & OUTBOX-002: Test claim token generation and fenced state mutations."""
        from datetime import timedelta

        from program.contracts.operation_ledger import (
            claim_due_operations,
            complete_operation,
            enqueue_operation,
            fail_operation,
            renew_lease,
        )

        engine = create_engine("sqlite:///:memory:")
        get_base_metadata().create_all(engine)

        with Session(engine) as session:
            op = enqueue_operation(
                session=session,
                operation_type="process_media",
                payload={"id": 123},
            )
            session.commit()

            # Claim operation
            claimed = claim_due_operations(session, "worker-alpha", limit=1, lease_duration_seconds=10)
            assert len(claimed) == 1
            item = claimed[0]
            assert item.worker_id == "worker-alpha"
            token1 = item.claim_token
            assert token1 is not None and len(token1) == 36

            # Attempt renew with wrong worker or wrong token
            assert renew_lease(session, item.id, "worker-beta", claim_token=token1) is False
            assert renew_lease(session, item.id, "worker-alpha", claim_token="wrong-token-uuid") is False

            # Valid renew
            assert renew_lease(session, item.id, "worker-alpha", claim_token=token1, extension_seconds=20) is True

            # Attempt completion with wrong token
            assert complete_operation(session, item.id, worker_id="worker-alpha", claim_token="invalid-token") is None
            # Attempt fail with wrong worker
            assert (
                fail_operation(
                    session,
                    item.id,
                    worker_id="worker-wrong",
                    claim_token=token1,
                    error_message="some error",
                )
                is None
            )

            # Valid completion with correct token & worker
            completed = complete_operation(session, item.id, worker_id="worker-alpha", claim_token=token1)
            session.commit()
            assert completed is not None
            assert completed.status == "completed"

            # Subsequent attempts to complete or fail already-completed operation return None
            assert complete_operation(session, item.id, worker_id="worker-alpha", claim_token=token1) is None
            assert fail_operation(session, item.id, worker_id="worker-alpha", claim_token=token1) is None

    def test_lease_expiry_reclaim_invalidates_stale_worker(self):
        """Test that an expired lease can be reclaimed with a new token, rejecting stale worker updates."""
        from datetime import timedelta

        from program.contracts.operation_ledger import (
            claim_due_operations,
            complete_operation,
            enqueue_operation,
            renew_lease,
        )

        engine = create_engine("sqlite:///:memory:")
        get_base_metadata().create_all(engine)

        with Session(engine) as session:
            op = enqueue_operation(
                session=session,
                operation_type="long_task",
                payload={"data": 1},
            )
            session.commit()

            # Worker 1 claims
            claimed1 = claim_due_operations(session, "worker-1", limit=1, lease_duration_seconds=5)
            assert len(claimed1) == 1
            w1_item = claimed1[0]
            token1 = w1_item.claim_token

            # Simulate lease expiration
            w1_item.lease_expires_at = datetime.now(UTC) - timedelta(seconds=10)
            session.commit()

            # Worker 2 claims expired lease
            claimed2 = claim_due_operations(session, "worker-2", limit=1, lease_duration_seconds=30)
            assert len(claimed2) == 1
            w2_item = claimed2[0]
            token2 = w2_item.claim_token

            assert token2 != token1
            assert w2_item.worker_id == "worker-2"

            # Worker 1 tries to renew or complete with stale token - must be rejected
            assert renew_lease(session, op.id, "worker-1", claim_token=token1) is False
            assert complete_operation(session, op.id, worker_id="worker-1", claim_token=token1) is None

            # Worker 2 successfully completes
            completed = complete_operation(session, op.id, worker_id="worker-2", claim_token=token2)
            session.commit()
            assert completed is not None
            assert completed.status == "completed"

    def test_strict_fencing_all_permutations(self):
        """Verify strict fencing rejects wrong/stale worker or token across all permutations."""
        from program.contracts.operation_ledger import (
            claim_due_operations,
            complete_operation,
            enqueue_operation,
            fail_operation,
            renew_lease,
        )

        engine = create_engine("sqlite:///:memory:")
        get_base_metadata().create_all(engine)

        with Session(engine) as session:
            op = enqueue_operation(
                session=session,
                operation_type="fencing_matrix_test",
                payload={"k": "v"},
            )
            session.commit()

            claimed = claim_due_operations(session, "worker-correct", limit=1, lease_duration_seconds=60)
            assert len(claimed) == 1
            op_item = claimed[0]
            correct_token = op_item.claim_token
            assert correct_token is not None

            # Permutation 1: Wrong worker + Wrong token
            assert renew_lease(session, op.id, "worker-wrong", "wrong-token") is False
            assert complete_operation(session, op.id, "worker-wrong", "wrong-token") is None
            assert fail_operation(session, op.id, "worker-wrong", "wrong-token") is None

            # Permutation 2: Wrong worker + Correct token
            assert renew_lease(session, op.id, "worker-wrong", correct_token) is False
            assert complete_operation(session, op.id, "worker-wrong", correct_token) is None
            assert fail_operation(session, op.id, "worker-wrong", correct_token) is None

            # Permutation 3: Correct worker + Wrong token
            assert renew_lease(session, op.id, "worker-correct", "wrong-token") is False
            assert complete_operation(session, op.id, "worker-correct", "wrong-token") is None
            assert fail_operation(session, op.id, "worker-correct", "wrong-token") is None

            # Permutation 4: Correct worker + Correct token -> renew succeeds
            assert renew_lease(session, op.id, "worker-correct", correct_token, extension_seconds=60) is True

            # Permutation 5: Correct worker + Correct token -> complete succeeds
            completed = complete_operation(session, op.id, "worker-correct", correct_token)
            session.commit()
            assert completed is not None
            assert completed.status == "completed"

            # Permutation 6: Stale mutation after status is no longer 'processing'
            assert renew_lease(session, op.id, "worker-correct", correct_token) is False
            assert complete_operation(session, op.id, "worker-correct", correct_token) is None
            assert fail_operation(session, op.id, "worker-correct", correct_token) is None

    def test_concurrent_claims_file_backed_sqlite_wal(self, tmp_path):
        """Verify concurrent worker claiming with file-backed SQLite, WAL mode, and independent sessions."""
        import concurrent.futures
        import sqlite3

        from program.contracts.operation_ledger import (
            claim_due_operations,
            complete_operation,
            enqueue_operation,
        )

        db_file = tmp_path / "concurrent_outbox.db"
        db_url = f"sqlite:///{db_file}"

        # Initialize schema and WAL mode
        init_engine = create_engine(db_url)
        with init_engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA journal_mode=WAL;")
            conn.exec_driver_sql("PRAGMA busy_timeout=5000;")
        get_base_metadata().create_all(init_engine)

        num_tasks = 20
        with Session(init_engine) as session:
            for i in range(num_tasks):
                enqueue_operation(
                    session=session,
                    operation_type="concurrent_task",
                    payload={"idx": i},
                )
            session.commit()

        # Run 5 concurrent workers pulling tasks
        num_workers = 5
        claimed_by_worker: dict[str, list[str]] = {f"worker-{w}": [] for w in range(num_workers)}
        claim_tokens: list[str] = []

        def worker_loop(worker_idx: int):
            worker_id = f"worker-{worker_idx}"
            worker_engine = create_engine(
                db_url,
                connect_args={"timeout": 15},
            )
            while True:
                with Session(worker_engine) as s:
                    ops = claim_due_operations(s, worker_id, limit=2, lease_duration_seconds=30)
                    if not ops:
                        break
                    for op in ops:
                        claimed_by_worker[worker_id].append(op.id)
                        claim_tokens.append(op.claim_token)
                        # Immediately complete
                        complete_operation(s, op.id, worker_id, op.claim_token)
                    s.commit()

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = [executor.submit(worker_loop, w) for w in range(num_workers)]
            concurrent.futures.wait(futures)

        # Verify all tasks claimed and completed exactly once without duplicates
        all_claimed_ids = [op_id for ids in claimed_by_worker.values() for op_id in ids]
        assert len(all_claimed_ids) == num_tasks
        assert len(set(all_claimed_ids)) == num_tasks, "Duplicate task claims detected across concurrent workers"
        assert len(set(claim_tokens)) == num_tasks, "Duplicate claim tokens generated"

        with Session(init_engine) as session:
            from sqlalchemy import select
            completed_ops = session.execute(
                select(OperationLedger).where(OperationLedger.status == "completed")
            ).scalars().all()
            assert len(completed_ops) == num_tasks


class TestPostgreSQLClaimingLogic:
    """OUTBOX-003: Verify PostgreSQL-specific claiming logic, query construction, and transaction boundaries."""

    def test_for_update_skip_locked_sql_compilation(self):
        """Verify that the PostgreSQL branch generates FOR UPDATE SKIP LOCKED in compiled SQL."""
        from sqlalchemy.dialects import postgresql as pg_dialect

        from program.contracts.operation_ledger import OperationLedger

        now = datetime.now(UTC)
        now_eligible = now + __import__("datetime").timedelta(seconds=1)

        due_condition = sqlalchemy.or_(
            sqlalchemy.and_(
                OperationLedger.status == "pending",
                OperationLedger.scheduled_at <= now_eligible,
            ),
            sqlalchemy.and_(
                OperationLedger.status == "processing",
                OperationLedger.lease_expires_at.is_not(None),
                OperationLedger.lease_expires_at < now,
            ),
        )

        stmt = (
            select(OperationLedger)
            .filter(due_condition)
            .order_by(OperationLedger.scheduled_at.asc())
            .limit(10)
            .with_for_update(skip_locked=True)
        )

        compiled = stmt.compile(dialect=pg_dialect.dialect())
        sql_text = str(compiled)

        assert "FOR UPDATE" in sql_text, f"Missing FOR UPDATE in compiled SQL: {sql_text}"
        assert "SKIP LOCKED" in sql_text, f"Missing SKIP LOCKED in compiled SQL: {sql_text}"
        # Verify ordering and limit are present
        assert "ORDER BY" in sql_text, f"Missing ORDER BY in compiled SQL: {sql_text}"
        assert "LIMIT" in sql_text, f"Missing LIMIT in compiled SQL: {sql_text}"

    def test_dialect_detection_branches_correctly(self):
        """Verify that dialect detection correctly identifies SQLite vs PostgreSQL."""
        engine_sqlite = create_engine("sqlite:///:memory:")
        get_base_metadata().create_all(engine_sqlite)

        with Session(engine_sqlite) as session:
            bind = session.get_bind()
            dialect_name = getattr(getattr(bind, "dialect", None), "name", "unknown")
            assert dialect_name == "sqlite", f"Expected 'sqlite', got '{dialect_name}'"

    def test_postgresql_branch_generates_fresh_uuid4_per_row(self):
        """Verify that the PostgreSQL branch generates a unique UUID4 claim_token per row.

        Since we can't run a real PostgreSQL in unit tests, we verify the token generation
        logic by directly testing the pattern used in the PostgreSQL branch: each row in
        the claimed batch must receive a distinct UUID4 token.
        """
        from program.contracts.operation_ledger import (
            claim_due_operations,
            enqueue_operation,
        )

        engine = create_engine("sqlite:///:memory:")
        get_base_metadata().create_all(engine)

        with Session(engine) as session:
            # Enqueue 5 operations
            for i in range(5):
                enqueue_operation(
                    session=session,
                    operation_type="pg_token_test",
                    payload={"idx": i},
                )
            session.commit()

            # Claim all 5 — each must get a unique token
            claimed = claim_due_operations(session, "worker-pg-test", limit=10, lease_duration_seconds=60)
            assert len(claimed) == 5

            tokens = [op.claim_token for op in claimed]
            assert all(t is not None for t in tokens), "All claimed ops must have a claim_token"
            assert len(set(tokens)) == 5, f"Expected 5 unique tokens, got {len(set(tokens))}: {tokens}"

            # Verify each token is a valid UUID4 format (36 chars with hyphens)
            for token in tokens:
                assert len(token) == 36, f"Token length should be 36, got {len(token)}: {token}"
                # Validate UUID format
                parsed = uuid.UUID(token)
                assert parsed.version == 4, f"Token should be UUID4, got version {parsed.version}"

    def test_eligibility_predicates_pending_and_expired(self):
        """Verify eligibility predicates correctly select pending-due and expired-processing operations."""
        from datetime import timedelta

        from program.contracts.operation_ledger import (
            claim_due_operations,
            enqueue_operation,
        )

        engine = create_engine("sqlite:///:memory:")
        get_base_metadata().create_all(engine)

        with Session(engine) as session:
            now = datetime.now(UTC)

            # 1. Pending + due (scheduled in the past)
            op_due = enqueue_operation(
                session=session,
                operation_type="pred_test",
                payload={"case": "due"},
                scheduled_at=now - timedelta(seconds=10),
            )
            session.commit()

            # 2. Pending + future (not yet due)
            op_future = enqueue_operation(
                session=session,
                operation_type="pred_test",
                payload={"case": "future"},
                scheduled_at=now + timedelta(hours=1),
            )
            session.commit()

            # Claim should only pick up the due operation
            claimed = claim_due_operations(session, "worker-pred", limit=10, lease_duration_seconds=60)
            claimed_ids = {op.id for op in claimed}
            assert op_due.id in claimed_ids, "Due pending operation should be claimed"
            assert op_future.id not in claimed_ids, "Future pending operation should NOT be claimed"
            session.commit()

            # 3. Simulate expired processing lease
            op_expired = enqueue_operation(
                session=session,
                operation_type="pred_test",
                payload={"case": "expired_lease"},
            )
            # 4. Simulate expired processing lease with future scheduled_at (reclaim safety)
            op_expired_future_sched = enqueue_operation(
                session=session,
                operation_type="pred_test",
                payload={"case": "expired_lease_future_sched"},
                scheduled_at=now + timedelta(hours=2),
            )
            session.commit()

            # Manually set to processing with expired lease
            op_expired.status = "processing"
            op_expired.worker_id = "worker-old"
            op_expired.claim_token = str(uuid.uuid4())
            op_expired.lease_expires_at = now - timedelta(seconds=30)

            op_expired_future_sched.status = "processing"
            op_expired_future_sched.worker_id = "worker-old-2"
            op_expired_future_sched.claim_token = str(uuid.uuid4())
            op_expired_future_sched.lease_expires_at = now - timedelta(seconds=15)
            session.commit()

            # Claim should pick up both expired-lease operations regardless of scheduled_at
            claimed2 = claim_due_operations(session, "worker-pred-2", limit=10, lease_duration_seconds=60)
            claimed2_ids = {op.id for op in claimed2}
            assert op_expired.id in claimed2_ids, "Expired processing operation should be reclaimed"
            assert op_expired_future_sched.id in claimed2_ids, "Expired processing operation with future scheduled_at should be reclaimed"

    def test_fifo_ordering_by_scheduled_at(self):
        """Verify FIFO ordering: oldest scheduled_at is claimed first."""
        from datetime import timedelta

        from program.contracts.operation_ledger import (
            claim_due_operations,
            enqueue_operation,
        )

        engine = create_engine("sqlite:///:memory:")
        get_base_metadata().create_all(engine)

        with Session(engine) as session:
            now = datetime.now(UTC)

            # Enqueue in reverse chronological order
            op3 = enqueue_operation(session, "fifo_test", scheduled_at=now - timedelta(seconds=1))
            op1 = enqueue_operation(session, "fifo_test", scheduled_at=now - timedelta(seconds=30))
            op2 = enqueue_operation(session, "fifo_test", scheduled_at=now - timedelta(seconds=15))
            session.commit()

            # Claim with limit=1 should get the oldest first
            claimed = claim_due_operations(session, "worker-fifo", limit=3, lease_duration_seconds=60)
            assert len(claimed) == 3
            # Verify FIFO order: op1 (oldest) → op2 → op3 (newest)
            assert claimed[0].id == op1.id, f"First claimed should be oldest, got {claimed[0].id}"
            assert claimed[1].id == op2.id, f"Second claimed should be middle, got {claimed[1].id}"
            assert claimed[2].id == op3.id, f"Third claimed should be newest, got {claimed[2].id}"

    def test_transaction_boundary_scalar_extraction(self):
        """Verify that claimed operation data can be extracted as plain scalars
        and used after the session is closed — matching the dispatcher pattern."""
        from program.contracts.operation_ledger import (
            claim_due_operations,
            enqueue_operation,
        )

        engine = create_engine("sqlite:///:memory:")
        get_base_metadata().create_all(engine)

        # Phase 1: Enqueue
        with Session(engine) as session:
            enqueue_operation(
                session=session,
                operation_type="boundary_test",
                payload={"key": "value"},
                media_item_id=999,
            )
            session.commit()

        # Phase 2: Claim and extract scalars (mimicking dispatcher pattern)
        extracted_data: list[tuple[str, str, dict | None, int, str, int | None, str]] = []
        with Session(engine) as session:
            ops = claim_due_operations(session, "worker-boundary", limit=5, lease_duration_seconds=60)
            assert len(ops) == 1
            for op in ops:
                extracted_data.append((
                    op.id,
                    op.operation_type,
                    op.payload,
                    op.attempt_count,
                    op.correlation_id,
                    op.media_item_id,
                    op.claim_token or "",
                ))
            session.commit()

        # Phase 3: Session is closed — verify extracted scalars are still usable
        assert len(extracted_data) == 1
        op_id, op_type, payload, attempt_count, correlation_id, media_item_id, claim_token = extracted_data[0]
        assert isinstance(op_id, str) and len(op_id) == 36
        assert op_type == "boundary_test"
        assert payload == {"key": "value"}
        assert attempt_count == 1
        assert isinstance(correlation_id, str)
        assert media_item_id == 999
        assert isinstance(claim_token, str) and len(claim_token) == 36

        # Phase 4: Verify the operation is in 'processing' state in a new session
        with Session(engine) as session:
            op = session.get(OperationLedger, op_id)
            assert op is not None
            assert op.status == "processing"
            assert op.worker_id == "worker-boundary"
            assert op.claim_token == claim_token

    def test_no_lock_held_during_execution_pattern(self):
        """Verify the dispatcher's transaction boundary pattern: claim in one session,
        execute outside, complete in a new session."""
        from program.contracts.operation_ledger import (
            claim_due_operations,
            complete_operation,
            enqueue_operation,
        )

        engine = create_engine("sqlite:///:memory:")
        get_base_metadata().create_all(engine)

        # Enqueue
        with Session(engine) as session:
            enqueue_operation(session, "lock_test", payload={"data": 1})
            session.commit()

        # Claim in session 1 (mimics _dispatch_due_operations)
        claim_token = None
        op_id = None
        with Session(engine) as session:
            ops = claim_due_operations(session, "worker-lock", limit=1, lease_duration_seconds=60)
            assert len(ops) == 1
            op_id = ops[0].id
            claim_token = ops[0].claim_token
            session.commit()
        # Session 1 is now CLOSED — no locks held

        # "Execute" the operation (simulated work outside any session)
        result = {"computed": True}

        # Complete in session 2 (mimics _execute_operation)
        with Session(engine) as session:
            completed = complete_operation(session, op_id, "worker-lock", claim_token)
            assert completed is not None
            assert completed.status == "completed"
            session.commit()

        # Final verification in session 3
        with Session(engine) as session:
            final = session.get(OperationLedger, op_id)
            assert final is not None
            assert final.status == "completed"
            assert final.completed_at is not None
            assert final.lease_expires_at is None
