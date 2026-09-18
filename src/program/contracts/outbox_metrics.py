"""Prometheus metrics collectors and fail-safe recorders for CineFlow outbox subsystem."""

from __future__ import annotations

from loguru import logger
from prometheus_client import Counter, Gauge, Histogram

from program.services.streaming.prom_cache_metrics import REGISTRY

OUTBOX_CLAIMED_TOTAL = Counter(
    "riven_outbox_claimed_total",
    "Total number of outbox operations claimed by background workers",
    ["operation_type"],
    registry=REGISTRY,
)

OUTBOX_CLAIM_LATENCY_SECONDS = Histogram(
    "riven_outbox_claim_latency_seconds",
    "Latency of outbox claim database queries in seconds",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
    registry=REGISTRY,
)

OUTBOX_ACTIVE_LEASES = Gauge(
    "riven_outbox_active_leases",
    "Current number of active operation leases held by workers",
    registry=REGISTRY,
)

OUTBOX_LEASE_RENEWALS_TOTAL = Counter(
    "riven_outbox_lease_renewals_total",
    "Total number of outbox lease renewal attempts",
    ["result"],
    registry=REGISTRY,
)

OUTBOX_STALE_REJECTIONS_TOTAL = Counter(
    "riven_outbox_stale_rejections_total",
    "Total number of outbox state transitions rejected due to lost ownership or stale fence",
    ["stage"],
    registry=REGISTRY,
)

OUTBOX_RETRY_REQUESTS_TOTAL = Counter(
    "riven_outbox_retry_requests_total",
    "Total number of manual or automated outbox retry requests processed",
    ["status"],
    registry=REGISTRY,
)

OUTBOX_LISTENER_QUEUE_DEPTH = Gauge(
    "riven_outbox_listener_queue_depth",
    "Current number of pending tasks in the outbox lifecycle listener executor queue",
    registry=REGISTRY,
)

OUTBOX_LISTENER_FAILURES_TOTAL = Counter(
    "riven_outbox_listener_failures_total",
    "Total number of exceptions raised during outbox lifecycle listener execution",
    ["event_type"],
    registry=REGISTRY,
)


def record_claim(operation_type: str, count: int = 1) -> None:
    """Increment the total count of claimed operations for a given operation type."""
    try:
        OUTBOX_CLAIMED_TOTAL.labels(operation_type=operation_type or "unknown").inc(
            count
        )
    except Exception as exc:
        logger.debug(f"Failed to record outbox claim metric: {exc}")


def record_claim_latency(duration_seconds: float) -> None:
    """Observe the duration of an outbox claim query in seconds."""
    try:
        OUTBOX_CLAIM_LATENCY_SECONDS.observe(max(0.0, float(duration_seconds)))
    except Exception as exc:
        logger.debug(f"Failed to record outbox claim latency metric: {exc}")


def set_active_leases(count: int) -> None:
    """Set the current gauge of active leases held by dispatcher workers."""
    try:
        OUTBOX_ACTIVE_LEASES.set(max(0, count))
    except Exception as exc:
        logger.debug(f"Failed to set active leases metric: {exc}")


def record_lease_renewal(result: str = "success") -> None:
    """Record the result of an active lease renewal attempt."""
    try:
        OUTBOX_LEASE_RENEWALS_TOTAL.labels(result=result).inc()
    except Exception as exc:
        logger.debug(f"Failed to record lease renewal metric: {exc}")


def record_stale_rejection(stage: str) -> None:
    """Record an operation state mutation rejection caused by stale claim token or lost ownership."""
    try:
        OUTBOX_STALE_REJECTIONS_TOTAL.labels(stage=stage).inc()
    except Exception as exc:
        logger.debug(f"Failed to record stale rejection metric: {exc}")


def record_retry_request(status: str) -> None:
    """Record an outbox retry request invocation and status outcome."""
    try:
        OUTBOX_RETRY_REQUESTS_TOTAL.labels(status=status).inc()
    except Exception as exc:
        logger.debug(f"Failed to record retry request metric: {exc}")


def set_listener_queue_depth(depth: int) -> None:
    """Set the gauge tracking listener thread pool queue depth."""
    try:
        OUTBOX_LISTENER_QUEUE_DEPTH.set(max(0, depth))
    except Exception as exc:
        logger.debug(f"Failed to set listener queue depth metric: {exc}")


def record_listener_failure(event_type: str) -> None:
    """Record an unhandled exception thrown inside an isolated lifecycle listener."""
    try:
        OUTBOX_LISTENER_FAILURES_TOTAL.labels(event_type=event_type or "unknown").inc()
    except Exception as exc:
        logger.debug(f"Failed to record listener failure metric: {exc}")


__all__ = [
    "OUTBOX_ACTIVE_LEASES",
    "OUTBOX_CLAIMED_TOTAL",
    "OUTBOX_CLAIM_LATENCY_SECONDS",
    "OUTBOX_LEASE_RENEWALS_TOTAL",
    "OUTBOX_LISTENER_FAILURES_TOTAL",
    "OUTBOX_LISTENER_QUEUE_DEPTH",
    "OUTBOX_RETRY_REQUESTS_TOTAL",
    "OUTBOX_STALE_REJECTIONS_TOTAL",
    "record_claim",
    "record_claim_latency",
    "record_lease_renewal",
    "record_listener_failure",
    "record_retry_request",
    "record_stale_rejection",
    "set_active_leases",
    "set_listener_queue_depth",
]
