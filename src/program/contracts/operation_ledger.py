"""Operation ledger persistence model and transactional outbox operations."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import sqlalchemy
from sqlalchemy import or_, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Mapped, Session, mapped_column

from program.contracts.telemetry import (
    get_correlation_id,
    redact_sensitive_data,
    redact_text,
)
from program.db.base_model import Base


class OperationLedger(Base):
    """
    Additive durable record of a requested asynchronous operation.

    Supports dual-dialect transactional outbox dispatching (PostgreSQL row-level locking
    and SQLite optimistic leasing), error classification, lease recovery, and credential redaction.
    """

    __tablename__ = "OperationLedger"
    __table_args__ = (
        sqlalchemy.Index("ix_OperationLedger_claim", "status", "scheduled_at"),
        sqlalchemy.Index("ix_OperationLedger_lease", "status", "lease_expires_at"),
        sqlalchemy.Index(
            "ix_OperationLedger_created_at_desc", sqlalchemy.text("created_at DESC")
        ),
        sqlalchemy.Index(
            "ix_OperationLedger_status_created_at",
            "status",
            sqlalchemy.text("created_at DESC"),
        ),
    )

    id: Mapped[str] = mapped_column(sqlalchemy.String(36), primary_key=True)
    correlation_id: Mapped[str] = mapped_column(sqlalchemy.String(128), nullable=False)
    # Kept intentionally decoupled, matching ScheduledTask: the dispatcher may
    # retain operation history after a media item is removed.
    media_item_id: Mapped[int | None] = mapped_column(
        sqlalchemy.Integer, nullable=True, index=True
    )
    operation_type: Mapped[str] = mapped_column(sqlalchemy.String(64), nullable=False)
    schema_version: Mapped[int] = mapped_column(
        sqlalchemy.Integer, nullable=False, default=1, server_default="1"
    )
    status: Mapped[str] = mapped_column(
        sqlalchemy.String(32),
        nullable=False,
        default="pending",
        server_default="pending",
        index=True,
    )
    attempt_count: Mapped[int] = mapped_column(
        sqlalchemy.Integer, nullable=False, default=0, server_default="0"
    )
    idempotency_key: Mapped[str | None] = mapped_column(
        sqlalchemy.String(256), nullable=True, unique=True
    )
    scheduled_at: Mapped[datetime] = mapped_column(
        sqlalchemy.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        index=True,
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        sqlalchemy.DateTime(timezone=True), nullable=True, index=True
    )
    worker_id: Mapped[str | None] = mapped_column(sqlalchemy.String(64), nullable=True)
    claim_token: Mapped[str | None] = mapped_column(
        sqlalchemy.String(36), nullable=True, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(
        sqlalchemy.DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        sqlalchemy.DateTime(timezone=True), nullable=True
    )
    error_classification: Mapped[str | None] = mapped_column(
        sqlalchemy.String(64), nullable=True
    )
    error_message: Mapped[str | None] = mapped_column(sqlalchemy.Text, nullable=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(
        sqlalchemy.JSON, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        sqlalchemy.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    updated_at: Mapped[datetime] = mapped_column(
        sqlalchemy.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


def enqueue_operation(
    session: Session,
    operation_type: str,
    payload: dict[str, Any] | None = None,
    *,
    media_item_id: int | None = None,
    idempotency_key: str | None = None,
    scheduled_at: datetime | None = None,
    correlation_id: str | None = None,
    schema_version: int = 1,
) -> OperationLedger:
    """
    Atomically enqueue a new operation in the active SQLAlchemy database session.

    Ensures correlation ID propagation and recursive credential redaction.
    If an idempotency_key is provided and already exists, returns the existing record.
    """
    if idempotency_key:
        existing = session.execute(
            select(OperationLedger).filter(
                OperationLedger.idempotency_key == idempotency_key
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

    cid = correlation_id or get_correlation_id()
    now = datetime.now(UTC)
    sched = scheduled_at or now

    redacted_payload = redact_sensitive_data(payload) if payload is not None else None

    operation = OperationLedger(
        id=str(uuid.uuid4()),
        correlation_id=cid,
        media_item_id=media_item_id,
        operation_type=operation_type,
        schema_version=schema_version,
        status="pending",
        attempt_count=0,
        idempotency_key=idempotency_key,
        scheduled_at=sched,
        lease_expires_at=None,
        worker_id=None,
        claim_token=None,
        started_at=None,
        completed_at=None,
        error_classification=None,
        error_message=None,
        payload=redacted_payload,
        created_at=now,
        updated_at=now,
    )
    session.add(operation)
    try:
        from sqlalchemy import event

        from program.contracts.dispatcher import (
            notify_outbox_dispatcher,
            publish_outbox_lifecycle_event,
        )
        from program.contracts.operation_timeline import (
            serialize_operation_timeline_item,
        )

        serialized_snapshot = serialize_operation_timeline_item(operation)

        def _after_commit_hook(_s: Any) -> None:
            publish_outbox_lifecycle_event("operation_enqueued", serialized_snapshot)
            notify_outbox_dispatcher()

        event.listen(session, "after_commit", _after_commit_hook, once=True)
    except Exception:
        try:
            from program.contracts.dispatcher import notify_outbox_dispatcher

            notify_outbox_dispatcher()
        except Exception:
            pass

    return operation


def claim_due_operations(
    session: Session,
    worker_id: str,
    limit: int = 10,
    lease_duration_seconds: int = 300,
) -> list[OperationLedger]:
    """
    Claim due pending operations or expired in-flight leases using dialect-aware locking.

    PostgreSQL: Uses `FOR UPDATE SKIP LOCKED` for non-blocking concurrent worker claims.
    SQLite / Other: Uses atomic conditional updates to prevent concurrent worker races.
    """
    now = datetime.now(UTC)
    # Small buffer to prevent sub-millisecond clock precision races on immediate scheduling
    now_eligible = now + timedelta(seconds=1)
    lease_exp = now + timedelta(seconds=lease_duration_seconds)

    due_condition = or_(
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
        .limit(limit)
    )

    try:
        bind = session.get_bind()
        dialect_name = getattr(getattr(bind, "dialect", None), "name", "unknown")
    except Exception:
        dialect_name = "unknown"

    if dialect_name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
        records = list(session.execute(stmt).scalars().all())
        for op in records:
            token = str(uuid.uuid4())
            op.status = "processing"
            op.worker_id = worker_id
            op.claim_token = token
            op.started_at = now
            op.lease_expires_at = lease_exp
            op.attempt_count += 1
            op.updated_at = now
        if records:
            session.flush()
        return records

    candidates = list(session.execute(stmt).scalars().all())
    claimed_records: list[OperationLedger] = []
    for cand in candidates:
        token = str(uuid.uuid4())
        update_stmt = (
            sqlalchemy.update(OperationLedger)
            .where(
                OperationLedger.id == cand.id,
                due_condition,
            )
            .values(
                status="processing",
                worker_id=worker_id,
                claim_token=token,
                started_at=now,
                lease_expires_at=lease_exp,
                attempt_count=OperationLedger.attempt_count + 1,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        res = cast(CursorResult[Any], session.execute(update_stmt))
        if res.rowcount and res.rowcount > 0:
            session.expire(cand)
            reloaded = session.get(OperationLedger, cand.id)
            if reloaded is not None:
                claimed_records.append(reloaded)

    if claimed_records:
        session.flush()

    return claimed_records


def complete_operation(
    session: Session,
    operation_id: str,
    worker_id: str,
    claim_token: str,
) -> OperationLedger | None:
    """Mark an in-flight operation as successfully completed with strict ownership fencing."""
    now = datetime.now(UTC)
    update_stmt = (
        sqlalchemy.update(OperationLedger)
        .where(
            OperationLedger.id == operation_id,
            OperationLedger.status == "processing",
            OperationLedger.claim_token == claim_token,
            OperationLedger.worker_id == worker_id,
        )
        .values(
            status="completed",
            completed_at=now,
            lease_expires_at=None,
            error_classification=None,
            error_message=None,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    res = cast(CursorResult[Any], session.execute(update_stmt))
    if not res.rowcount or res.rowcount == 0:
        return None

    # Invalidate session cache for this entity if loaded
    cached = session.get(OperationLedger, operation_id)
    if cached is not None:
        session.expire(cached)

    session.flush()
    return session.get(OperationLedger, operation_id)


def fail_operation(
    session: Session,
    operation_id: str,
    worker_id: str,
    claim_token: str,
    *,
    error_classification: str | None = None,
    error_message: str | None = None,
    next_retry_at: datetime | None = None,
) -> OperationLedger | None:
    """
    Record an operation failure with strict ownership fencing, either rescheduling as pending for retry
    or marking failed (dead-letter).
    """
    now = datetime.now(UTC)
    redacted_msg = redact_text(error_message) if error_message else None

    if next_retry_at is not None:
        values = {
            "status": "pending",
            "scheduled_at": next_retry_at,
            "lease_expires_at": None,
            "worker_id": None,
            "claim_token": None,
            "error_classification": error_classification,
            "error_message": redacted_msg,
            "updated_at": now,
        }
    else:
        values = {
            "status": "failed",
            "completed_at": now,
            "lease_expires_at": None,
            "error_classification": error_classification,
            "error_message": redacted_msg,
            "updated_at": now,
        }

    update_stmt = (
        sqlalchemy.update(OperationLedger)
        .where(
            OperationLedger.id == operation_id,
            OperationLedger.status == "processing",
            OperationLedger.claim_token == claim_token,
            OperationLedger.worker_id == worker_id,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    res = cast(CursorResult[Any], session.execute(update_stmt))
    if not res.rowcount or res.rowcount == 0:
        return None

    cached = session.get(OperationLedger, operation_id)
    if cached is not None:
        session.expire(cached)

    session.flush()
    return session.get(OperationLedger, operation_id)


def renew_lease(
    session: Session,
    operation_id: str,
    worker_id: str,
    claim_token: str,
    *,
    extension_seconds: int = 300,
) -> bool:
    """Extend lease expiration for an active long-running operation with strict ownership fencing."""
    now = datetime.now(UTC)
    stmt = (
        sqlalchemy.update(OperationLedger)
        .where(
            OperationLedger.id == operation_id,
            OperationLedger.status == "processing",
            OperationLedger.claim_token == claim_token,
            OperationLedger.worker_id == worker_id,
        )
        .values(
            lease_expires_at=now + timedelta(seconds=extension_seconds),
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    result = cast(CursorResult[Any], session.execute(stmt))
    if result.rowcount and result.rowcount > 0:
        cached = session.get(OperationLedger, operation_id)
        if cached is not None:
            session.expire(cached)
        session.flush()
        return True
    return False


def get_active_outbox_item_ids(
    session: Session,
    operation_type: str = "retry_library_item",
) -> set[int]:
    """
    Return MediaItem IDs associated with pending or processing outbox operations.

    Used for candidate exclusion prior to enqueuing new durable retry operations.
    Note: The authoritative duplicate-prevention mechanism is the UNIQUE
    constraint on OperationLedger.idempotency_key.
    """
    stmt = (
        select(OperationLedger.media_item_id)
        .filter(
            OperationLedger.operation_type == operation_type,
            OperationLedger.status.in_(["pending", "processing", "in_progress"]),
            OperationLedger.media_item_id.is_not(None),
        )
    )
    result = session.execute(stmt).scalars().all()
    return {item_id for item_id in result if item_id is not None}
