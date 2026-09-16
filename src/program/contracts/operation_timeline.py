"""Canonical, safely redacted serialization for durable operation timeline records."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from program.contracts.telemetry import redact_sensitive_data, redact_text

if TYPE_CHECKING:
    from program.contracts.operation_ledger import OperationLedger


def _serialize_datetime(value: datetime | None) -> str | None:
    """Serialize an optional timeline timestamp for JSON transport."""
    return value.isoformat() if value is not None else None


def serialize_operation_timeline_item(operation: OperationLedger) -> dict[str, Any]:
    """Return the canonical API/SSE representation of one durable operation.

    The result intentionally mirrors the ``OperationTimelineItem`` API contract while
    remaining independent of the FastAPI router, so committed records can safely be
    delivered through both REST and SSE without an import cycle.
    """
    return {
        "id": operation.id,
        "correlation_id": operation.correlation_id,
        "media_item_id": operation.media_item_id,
        "operation_type": operation.operation_type,
        "schema_version": operation.schema_version,
        "status": operation.status,
        "attempt_count": operation.attempt_count,
        "idempotency_key": operation.idempotency_key,
        "scheduled_at": _serialize_datetime(operation.scheduled_at),
        "lease_expires_at": _serialize_datetime(operation.lease_expires_at),
        "worker_id": operation.worker_id,
        "started_at": _serialize_datetime(operation.started_at),
        "completed_at": _serialize_datetime(operation.completed_at),
        "error_classification": operation.error_classification,
        "error_message": redact_text(operation.error_message) if operation.error_message else None,
        "payload": redact_sensitive_data(operation.payload) if operation.payload else None,
        "created_at": _serialize_datetime(operation.created_at),
        "updated_at": _serialize_datetime(operation.updated_at),
    }
