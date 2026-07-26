"""Prometheus counters for Plex → Trakt history sync (low cardinality)."""

from __future__ import annotations

from prometheus_client import Counter

from program.services.streaming.prom_cache_metrics import REGISTRY

HISTORY_POSTS = Counter(
    "cineflow_plex_trakt_history_posts_total",
    "Plex scrobble → Trakt /sync/history outcomes",
    ["result"],
    registry=REGISTRY,
)


def record_history_result(result: str) -> None:
    """Increment outcome counter. Labels: success|failed|skipped|idempotent|dry_run."""

    HISTORY_POSTS.labels(result=result).inc()
