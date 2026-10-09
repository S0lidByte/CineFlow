"""Shared httpx Limits/Timeouts for streaming AsyncClient and ProxyClient."""

from __future__ import annotations

import httpx

# Match historical AsyncClient caps; leave headroom under max for non-stream API use.
STREAM_MAX_CONNECTIONS = 200
STREAM_MAX_KEEPALIVE_CONNECTIONS = 50
STREAM_KEEPALIVE_EXPIRY_SECONDS = 60.0

# Admission budgets (under STREAM_MAX_CONNECTIONS).
MAX_TOTAL_STREAM_REQUESTS = 180
MAX_BODY_STREAMS = 64

STREAM_CONNECT_TIMEOUT = 5.0
STREAM_READ_TIMEOUT = 30.0
STREAM_WRITE_TIMEOUT = 10.0
STREAM_POOL_TIMEOUT = 5.0


def stream_http_limits() -> httpx.Limits:
    """Connection pool limits for debrid streaming clients."""

    return httpx.Limits(
        max_connections=STREAM_MAX_CONNECTIONS,
        max_keepalive_connections=STREAM_MAX_KEEPALIVE_CONNECTIONS,
        keepalive_expiry=STREAM_KEEPALIVE_EXPIRY_SECONDS,
    )


def stream_http_timeout(connect_timeout: float | None = None) -> httpx.Timeout:
    """Timeouts including pool acquire — fail fast when saturated.

    Args:
        connect_timeout: Optional override for connection timeout in seconds.
            If omitted or None, defaults to STREAM_CONNECT_TIMEOUT (5.0s).
    """

    return httpx.Timeout(
        connect=(
            STREAM_CONNECT_TIMEOUT
            if connect_timeout is None
            else float(connect_timeout)
        ),
        read=STREAM_READ_TIMEOUT,
        write=STREAM_WRITE_TIMEOUT,
        pool=STREAM_POOL_TIMEOUT,
    )
