"""Operational event contracts and structured execution payloads for CineFlow."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, cast

from program.contracts.telemetry import (
    generate_correlation_id,
    get_correlation_id,
    redact_sensitive_data,
)


class OperationType(StrEnum):
    """Normalized categories of high-level asynchronous operations."""

    INDEX = "index"
    SCRAPE = "scrape"
    DOWNLOAD = "download"
    SYMLINK = "symlink"
    STREAM = "stream"
    METADATA_REFRESH = "metadata_refresh"
    MAINTENANCE = "maintenance"
    OUTBOX_DISPATCH = "outbox_dispatch"
    CUSTOM = "custom"


class OperationStatus(StrEnum):
    """Lifecycle status of an operation execution."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class OutboxLifecycleEventType(StrEnum):
    """Canonical lifecycle transition events for durable outbox operations."""

    ENQUEUED = "operation_enqueued"
    STARTED = "operation_started"
    COMPLETED = "operation_completed"
    FAILED = "operation_failed"
    RETRYING = "operation_retrying"


@dataclass(frozen=True, slots=True)
class OutboxLifecycleEvent:
    """Post-commit notification for an immutable durable outbox state transition."""

    event_type: str
    operation: dict[str, Any]
    version: int = 1
    event_name: str | None = None
    operation_id: str | None = None
    claim_token: str | None = None
    status: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    payload: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.event_name is None:
            object.__setattr__(self, "event_name", self.event_type)
        if self.operation_id is None and "id" in self.operation:
            object.__setattr__(self, "operation_id", str(self.operation["id"]))
        if self.status is None and "status" in self.operation:
            object.__setattr__(self, "status", str(self.operation["status"]))
        if self.payload is None:
            object.__setattr__(self, "payload", self.operation)


OutboxLifecycleListener = Callable[[OutboxLifecycleEvent], None]


@dataclass(slots=True)
class OperationEvent:
    """
    Structured event record representing an atomic operation or lifecycle transition.

    Includes correlation tracking, execution metrics, and automated redaction of sensitive payloads.
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    correlation_id: str = field(default_factory=get_correlation_id)
    operation_type: OperationType = OperationType.CUSTOM
    status: OperationStatus = OperationStatus.PENDING
    item_id: int | str | None = None
    title: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    duration_ms: float | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=lambda: cast(dict[str, Any], {}))

    def mark_completed(self, status: OperationStatus = OperationStatus.SUCCESS, error: str | None = None) -> None:
        """Mark the event completed and calculate elapsed duration if applicable."""
        self.status = status
        self.error = error
        elapsed = (datetime.now(UTC) - self.created_at).total_seconds() * 1000.0
        self.duration_ms = max(0.0, round(elapsed, 2))

    def to_dict(self, redact_secrets: bool = True) -> dict[str, Any]:
        """Convert the event to a dictionary, automatically redacting credentials and tokens if requested."""
        raw_dict = {
            "id": self.id,
            "correlation_id": self.correlation_id,
            "operation_type": self.operation_type.value,
            "status": self.status.value,
            "item_id": self.item_id,
            "title": self.title,
            "created_at": self.created_at.isoformat(),
            "duration_ms": self.duration_ms,
            "error": self.error,
            "metadata": self.metadata,
        }
        return redact_sensitive_data(raw_dict) if redact_secrets else raw_dict


def create_operation_event(
    operation_type: OperationType,
    item_id: int | str | None = None,
    title: str | None = None,
    correlation_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> OperationEvent:
    """Factory function to build a structured OperationEvent with sensible defaults."""
    cid = correlation_id or get_correlation_id() or generate_correlation_id()
    return OperationEvent(
        correlation_id=cid,
        operation_type=operation_type,
        status=OperationStatus.RUNNING,
        item_id=item_id,
        title=title,
        metadata=metadata or {},
    )
