"""Admission control and auto-heal for the shared streaming httpx pool."""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Literal

import httpx
import sniffio
import trio
from kink import di
from loguru import logger

from program.utils.async_client import AsyncClient
from program.utils.proxy_client import ProxyClient
from program.utils.stream_http import (
    MAX_BODY_STREAMS,
    MAX_TOTAL_STREAM_REQUESTS,
    STREAM_MAX_CONNECTIONS,
)

RequestKind = Literal["body", "scan"]

_generation = 0
_recycle_lock = threading.Lock()
_heal_in_progress = False
_shed_callback: Callable[[], Awaitable[None]] | None = None
_pool_timeout_last_warn = 0.0
_POOL_TIMEOUT_WARN_INTERVAL = 5.0

_total_limiter: trio.CapacityLimiter | None = None
_body_limiter: trio.CapacityLimiter | None = None
_limiter_lock = threading.Lock()


def _get_limiters() -> tuple[trio.CapacityLimiter, trio.CapacityLimiter]:
    global _total_limiter, _body_limiter

    with _limiter_lock:
        if _total_limiter is None:
            _total_limiter = trio.CapacityLimiter(MAX_TOTAL_STREAM_REQUESTS)
            _body_limiter = trio.CapacityLimiter(MAX_BODY_STREAMS)

        assert _body_limiter is not None
        return _total_limiter, _body_limiter


def reset_http_pool_state_for_tests() -> None:
    """Reset limiters/generation between unit tests."""

    global _total_limiter, _body_limiter, _generation, _heal_in_progress
    global _pool_timeout_last_warn, _shed_callback

    with _limiter_lock:
        _total_limiter = None
        _body_limiter = None

    with _recycle_lock:
        _generation = 0
        _heal_in_progress = False
        _pool_timeout_last_warn = 0.0
        _shed_callback = None


def register_stream_shed_callback(
    callback: Callable[[], Awaitable[None]] | None,
) -> None:
    """Register VFS callback to close idle/stalled MediaStreams during heal."""

    global _shed_callback
    _shed_callback = callback


def pool_generation() -> int:
    """Current shared-client generation (increments on recycle)."""

    return _generation


@asynccontextmanager
async def admit_stream_request(kind: RequestKind) -> AsyncIterator[None]:
    """
    Bound concurrent streaming HTTP requests under the httpx max_connections cap.

    Fail-fast with PoolTimeout when saturated so callers shed instead of wedging.
    """

    total, body = _get_limiters()

    if total.borrowed_tokens >= total.total_tokens:
        raise httpx.PoolTimeout("Streaming HTTP admission saturated")

    if kind == "body" and body.borrowed_tokens >= body.total_tokens:
        raise httpx.PoolTimeout("Streaming HTTP body admission saturated")

    if kind == "body":
        async with total:
            async with body:
                yield
    else:
        async with total:
            yield


async def _aclose_client(client: httpx.AsyncClient) -> None:
    token = sniffio.current_async_library_cvar.set("asyncio")
    try:
        await client.aclose()
    finally:
        sniffio.current_async_library_cvar.reset(token)


async def recycle_async_clients(*, reason: str) -> int:
    """
    Replace DI AsyncClient / ProxyClient singletons so a wedged pool recovers
    without process restart. Returns the new generation.
    """

    global _generation

    from program.settings import settings_manager

    old_async: httpx.AsyncClient | None = None
    old_proxy: httpx.AsyncClient | None = None

    with _recycle_lock:
        if AsyncClient in di:
            old_async = di[AsyncClient]

        di[AsyncClient] = AsyncClient()

        proxy_url = settings_manager.settings.downloaders.proxy_url

        if proxy_url:
            if ProxyClient in di:
                old_proxy = di[ProxyClient]
            di[ProxyClient] = ProxyClient(proxy_url=proxy_url)

        _generation += 1
        gen = _generation

        logger.warning(
            f"HTTP pool recycled (generation={gen}, reason={reason}, "
            f"max_connections={STREAM_MAX_CONNECTIONS}, "
            f"admission_total={MAX_TOTAL_STREAM_REQUESTS}, "
            f"admission_body={MAX_BODY_STREAMS})"
        )

    if old_async is not None:
        try:
            await _aclose_client(old_async)
        except Exception:
            logger.exception("Failed to aclose recycled AsyncClient")

    if old_proxy is not None:
        try:
            await _aclose_client(old_proxy)
        except Exception:
            logger.exception("Failed to aclose recycled ProxyClient")

    return gen


async def heal_on_pool_timeout(*, pool_repr: str = "") -> bool:
    """
    Shed stalled streams and recycle the shared client once per storm.

    Returns True when this caller performed a recycle (safe for one retry).
    """

    global _heal_in_progress, _pool_timeout_last_warn

    with _recycle_lock:
        if _heal_in_progress:
            return False
        _heal_in_progress = True

    try:
        now = trio.current_time()

        if now - _pool_timeout_last_warn >= _POOL_TIMEOUT_WARN_INTERVAL:
            logger.warning(
                "HTTP PoolTimeout — shedding stalled streams and recycling client"
                + (f": {pool_repr}" if pool_repr else "")
            )
            _pool_timeout_last_warn = now
        else:
            logger.debug(
                "HTTP PoolTimeout (warn suppressed)"
                + (f": {pool_repr}" if pool_repr else "")
            )

        if _shed_callback is not None:
            try:
                await _shed_callback()
            except Exception:
                logger.exception("Stream shed callback failed during pool heal")

        await recycle_async_clients(reason="PoolTimeout")
        return True
    finally:
        with _recycle_lock:
            _heal_in_progress = False
