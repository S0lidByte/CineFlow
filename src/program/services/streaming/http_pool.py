"""Admission control, connection pooling, and auto-heal for streaming httpx clients.

Provides both:
1. Mount-scoped, Trio-native `TrioStreamingHttpPool` for pyfuse3 VFS media streaming.
2. Legacy process-global admission and recycling for non-VFS callers.
"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
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
    stream_http_limits,
    stream_http_timeout,
)

RequestKind = Literal["body", "scan"]


@dataclass(frozen=True, slots=True)
class GenerationLease:
    """Represents a leased client connection tied to a specific pool generation."""

    generation: int
    client: httpx.AsyncClient
    use_proxy: bool


class TrioStreamingHttpPool:
    """
    Mount-scoped, Trio-native HTTP connection pool and admission manager for VFS streaming.

    Operates natively under Trio without mutating global dependency injection (kink.di).
    """

    def __init__(
        self,
        *,
        proxy_url: str | None = None,
        max_total_requests: int = MAX_TOTAL_STREAM_REQUESTS,
        max_body_streams: int = MAX_BODY_STREAMS,
        warn_interval: float = 5.0,
    ) -> None:
        if proxy_url is None:
            try:
                from program.settings import settings_manager

                self._proxy_url: str | None = settings_manager.settings.downloaders.proxy_url
            except Exception:
                self._proxy_url = None
        else:
            self._proxy_url = proxy_url

        self._max_total_requests = max_total_requests
        self._max_body_streams = max_body_streams
        self._warn_interval = warn_interval

        self._total_limiter = trio.CapacityLimiter(max_total_requests)
        self._body_limiter = trio.CapacityLimiter(max_body_streams)

        self._generation = 1
        self._clients: dict[bool, httpx.AsyncClient] = {}
        self._retired_generations: dict[int, list[httpx.AsyncClient]] = {}
        self._active_leases_by_gen: dict[int, int] = {}
        self._lock = trio.Lock()
        self._heal_in_progress = False
        # Followers await the current recovery rather than beginning another
        # recycle after the leader releases the lock.
        self._heal_finished = trio.Event()
        self._heal_finished.set()
        self._pool_timeout_last_warn = 0.0
        self._shed_callback: Callable[[], Awaitable[None]] | None = None
        self._closed = False

        self._clients = self._create_generation_clients()

    @property
    def generation(self) -> int:
        """Current active generation number."""
        return self._generation

    @property
    def active_leases(self) -> int:
        """Total active leased connections across all generations."""
        return sum(self._active_leases_by_gen.values())

    def register_stream_shed_callback(
        self,
        callback: Callable[[], Awaitable[None]] | None,
    ) -> None:
        """Register callback to close idle/stalled MediaStreams during heal."""
        self._shed_callback = callback

    def _create_client(self, *, proxy_url: str | None = None) -> httpx.AsyncClient:
        """Create a native Trio-compatible AsyncClient with streaming limits and timeouts."""
        return httpx.AsyncClient(
            http2=True,
            follow_redirects=True,
            proxy=proxy_url,
            limits=stream_http_limits(),
            timeout=stream_http_timeout(),
        )

    def _create_generation_clients(self) -> dict[bool, httpx.AsyncClient]:
        """Instantiate client map for a fresh generation."""
        clients: dict[bool, httpx.AsyncClient] = {
            False: self._create_client(proxy_url=None),
        }
        if self._proxy_url:
            clients[True] = self._create_client(proxy_url=self._proxy_url)
        return clients

    def get_client(self, *, use_proxy: bool = False) -> httpx.AsyncClient:
        """Retrieve current active client for the given proxy requirement."""
        if use_proxy and True in self._clients:
            return self._clients[True]
        return self._clients[False]

    def acquire_lease(self, *, use_proxy: bool = False) -> GenerationLease:
        """Acquire a generation-tracked lease for a streaming connection."""
        client = self.get_client(use_proxy=use_proxy)
        gen = self._generation
        self._active_leases_by_gen[gen] = self._active_leases_by_gen.get(gen, 0) + 1
        return GenerationLease(generation=gen, client=client, use_proxy=use_proxy)

    async def release_lease(self, lease: GenerationLease) -> None:
        """Release a lease and drain its retired generation when it becomes idle."""
        gen = lease.generation
        if gen in self._active_leases_by_gen:
            self._active_leases_by_gen[gen] -= 1
            if self._active_leases_by_gen[gen] <= 0:
                del self._active_leases_by_gen[gen]

        # A healed generation must remain available while its active response is
        # reading, then close promptly once its final lease releases.  Deferring
        # this until a later heal or teardown leaks mount-owned client resources.
        await self._close_idle_retired_generations()

    @asynccontextmanager
    async def admit(self, kind: RequestKind) -> AsyncIterator[None]:
        """
        Bound concurrent streaming HTTP requests under the httpx max_connections cap.

        Fail-fast with PoolTimeout when saturated so callers shed instead of wedging.
        """
        total = self._total_limiter
        body = self._body_limiter

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

    async def _close_idle_retired_generations(self) -> None:
        """Close retired clients for generations that no longer have active leases."""
        gens_to_close: list[int] = []
        async with self._lock:
            for gen in list(self._retired_generations.keys()):
                if self._active_leases_by_gen.get(gen, 0) <= 0:
                    gens_to_close.append(gen)

        for gen in gens_to_close:
            clients: list[httpx.AsyncClient] = []
            async with self._lock:
                if (
                    gen in self._retired_generations
                    and self._active_leases_by_gen.get(gen, 0) <= 0
                ):
                    clients = self._retired_generations.pop(gen, [])
            for client in clients:
                try:
                    await client.aclose()
                except Exception:
                    logger.exception("Failed to aclose retired VFS client")

    async def heal_on_pool_timeout(
        self,
        *,
        failed_generation: int | None = None,
        pool_repr: str = "",
    ) -> bool:
        """
        Shed stalled streams and recycle the mount-scoped pool once per storm.

        A timeout from an older generation is already recovered and must not
        begin a sequential recycle after the recovery leader has finished.
        Returns True only when this caller performs a recycle.
        """
        async with self._lock:
            if (
                failed_generation is not None
                and self._generation > failed_generation
            ):
                return False

            if self._heal_in_progress:
                finished = self._heal_finished
            else:
                self._heal_in_progress = True
                self._heal_finished = trio.Event()
                finished = None

        if finished is not None:
            # The caller's failed request is not retried by this invocation;
            # wait until the shared client generation is usable so it cannot
            # launch a sequential duplicate heal once the leader completes.
            await finished.wait()
            return False

        try:
            now = trio.current_time()

            if now - self._pool_timeout_last_warn >= self._warn_interval:
                logger.warning(
                    "HTTP PoolTimeout — shedding stalled streams and recycling VFS client"
                    + (f": {pool_repr}" if pool_repr else "")
                )
                self._pool_timeout_last_warn = now
            else:
                logger.debug(
                    "HTTP PoolTimeout (warn suppressed)"
                    + (f": {pool_repr}" if pool_repr else "")
                )

            if self._shed_callback is not None:
                try:
                    await self._shed_callback()
                except Exception:
                    logger.exception("Stream shed callback failed during VFS pool heal")

            async with self._lock:
                old_gen = self._generation
                old_clients = list(self._clients.values())
                self._retired_generations[old_gen] = old_clients

                self._generation += 1
                self._clients = self._create_generation_clients()
                new_gen = self._generation

                logger.warning(
                    f"VFS HTTP pool recycled (generation={new_gen}, reason=PoolTimeout, "
                    f"max_connections={STREAM_MAX_CONNECTIONS}, "
                    f"admission_total={self._max_total_requests}, "
                    f"admission_body={self._max_body_streams})"
                )

            await self._close_idle_retired_generations()
            return True
        finally:
            async with self._lock:
                self._heal_in_progress = False
                self._heal_finished.set()

    async def teardown(self) -> None:
        """Close all active and retired clients cleanly under Trio."""
        self._closed = True
        clients_to_close: list[httpx.AsyncClient] = []

        async with self._lock:
            clients_to_close.extend(self._clients.values())
            self._clients.clear()
            for retired in self._retired_generations.values():
                clients_to_close.extend(retired)
            self._retired_generations.clear()
            self._active_leases_by_gen.clear()

        for client in clients_to_close:
            try:
                await client.aclose()
            except Exception:
                logger.exception("Failed to aclose VFS HTTP client during teardown")


# ============================================================================
# Legacy module-level globals for non-VFS callers
# ============================================================================

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
