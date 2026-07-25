"""Prometheus mirrors of streaming disk-cache counters.

Low-cardinality process metrics only — no path, URL, or per-item labels.
Dual-writes from ``Cache.Metrics`` when ``metrics_enabled`` is true.
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    generate_latest,
)

REGISTRY = CollectorRegistry()

HITS = Counter(
    "riven_cache_hits_total",
    "Streaming disk cache hit count since process start",
    registry=REGISTRY,
)
MISSES = Counter(
    "riven_cache_misses_total",
    "Streaming disk cache miss count since process start",
    registry=REGISTRY,
)
BYTES_FROM_CACHE = Counter(
    "riven_cache_bytes_from_cache_total",
    "Bytes served from the streaming disk cache since process start",
    registry=REGISTRY,
)
BYTES_WRITTEN = Counter(
    "riven_cache_bytes_written_total",
    "Bytes written into the streaming disk cache since process start",
    registry=REGISTRY,
)
EVICTIONS = Counter(
    "riven_cache_evictions_total",
    "Streaming disk cache eviction count since process start",
    registry=REGISTRY,
)
SIZE_BYTES = Gauge(
    "riven_cache_size_bytes",
    "Current streaming disk cache payload size in bytes",
    registry=REGISTRY,
)
ENTRIES = Gauge(
    "riven_cache_entries",
    "Current number of chunk entries in the streaming disk cache index",
    registry=REGISTRY,
)


def record_hit(nbytes: int = 0) -> None:
    HITS.inc()
    if nbytes > 0:
        BYTES_FROM_CACHE.inc(nbytes)


def record_miss() -> None:
    MISSES.inc()


def record_bytes_written(nbytes: int) -> None:
    if nbytes > 0:
        BYTES_WRITTEN.inc(nbytes)


def record_evictions(count: int = 1) -> None:
    if count > 0:
        EVICTIONS.inc(count)


def set_size_gauges(*, total_bytes: int, entries: int) -> None:
    SIZE_BYTES.set(max(0, total_bytes))
    ENTRIES.set(max(0, entries))


def render_metrics() -> bytes:
    return generate_latest(REGISTRY)


__all__ = [
    "CONTENT_TYPE_LATEST",
    "REGISTRY",
    "record_bytes_written",
    "record_evictions",
    "record_hit",
    "record_miss",
    "render_metrics",
    "set_size_gauges",
]
