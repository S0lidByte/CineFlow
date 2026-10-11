from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from time import monotonic
from typing import Any

import httpx


class _ObservedBody(httpx.AsyncByteStream):
    """Count raw payload before HTTPX buffering; close accounting exactly once."""

    def __init__(
        self, body: httpx.AsyncByteStream, stats: "SessionStatistics", speculative: bool
    ):
        self.body = body
        self.stats = stats
        self.speculative = speculative
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        iterator = self.body.__aiter__()
        while True:
            started = monotonic()
            try:
                part = await anext(iterator)
            except StopAsyncIteration:
                return
            finally:
                # Summed read service time, NOT elapsed wall time or throughput.
                if hasattr(self.stats, "upstream_transfer_seconds"):
                    self.stats.upstream_transfer_seconds += monotonic() - started
                pub = getattr(self.stats, "publish", None)
                if callable(pub):
                    pub()
            if hasattr(self.stats, "upstream_bytes"):
                self.stats.upstream_bytes += len(part)
            if self.speculative and hasattr(self.stats, "speculative_bytes"):
                self.stats.speculative_bytes += len(part)
            pub = getattr(self.stats, "publish", None)
            if callable(pub):
                pub()
            yield part

    async def aclose(self) -> None:
        try:
            await self.body.aclose()
        finally:
            self.finish()

    def finish(self) -> None:
        if not self.closed:
            self.closed = True
            if hasattr(self.stats, "active_body_responses"):
                self.stats.active_body_responses = max(
                    0, self.stats.active_body_responses - 1
                )
            pub = getattr(self.stats, "publish", None)
            if callable(pub):
                pub()


@asynccontextmanager
async def observe_response(
    context: AbstractAsyncContextManager[httpx.Response],
    stats: "SessionStatistics",
    *,
    speculative: bool = False,
) -> AsyncGenerator[httpx.Response]:
    body: _ObservedBody | None = None
    entered = False
    try:
        async with context as response:
            entered = True
            if hasattr(stats, "total_session_connections"):
                stats.total_session_connections += 1
            if hasattr(stats, "active_body_responses"):
                stats.active_body_responses += 1
                stats.peak_body_responses = max(
                    getattr(stats, "peak_body_responses", 0),
                    stats.active_body_responses,
                )
            publish_fn = getattr(stats, "publish", None)
            if callable(publish_fn):
                publish_fn()
            stream_obj = getattr(response, "stream", None)
            if isinstance(stream_obj, httpx.AsyncByteStream):
                body = _ObservedBody(stream_obj, stats, speculative)
                response.stream = body
            elif hasattr(response, "aiter_raw"):
                original_aiter_raw = response.aiter_raw

                def wrapped_aiter_raw(
                    *args: Any, **kwargs: Any
                ) -> AsyncIterator[bytes]:
                    async def generator() -> AsyncIterator[bytes]:
                        async for part in original_aiter_raw(*args, **kwargs):
                            if hasattr(stats, "upstream_bytes"):
                                stats.upstream_bytes += len(part)
                            if speculative and hasattr(stats, "speculative_bytes"):
                                stats.speculative_bytes += len(part)
                            pub = getattr(stats, "publish", None)
                            if callable(pub):
                                pub()
                            yield part

                    return generator()

                response.aiter_raw = wrapped_aiter_raw
            yield response
    finally:
        if entered:
            if body is not None:
                body.finish()
            else:
                if hasattr(stats, "active_body_responses"):
                    stats.active_body_responses = max(
                        0, stats.active_body_responses - 1
                    )
                pub = getattr(stats, "publish", None)
                if callable(pub):
                    pub()


@dataclass
class SessionStatistics:
    """Statistics about the current streaming session."""

    bytes_transferred: int = 0
    upstream_bytes: int = 0
    upstream_transfer_seconds: float = 0.0
    total_session_connections: int = 0
    active_body_responses: int = 0
    peak_body_responses: int = 0
    speculative_bytes: int = 0
    speculative_failures: int = 0
    parallel_prefetch_workers: int = 1
    on_update: Callable[["SessionStatistics"], None] | None = field(
        default=None, repr=False
    )

    def publish(self) -> None:
        if self.on_update is not None:
            self.on_update(self)

    def upstream_snapshot(self) -> dict[str, int | float]:
        return {
            name: getattr(self, name)
            for name in (
                "upstream_bytes",
                "upstream_transfer_seconds",
                "total_session_connections",
                "active_body_responses",
                "peak_body_responses",
                "speculative_bytes",
                "speculative_failures",
                "parallel_prefetch_workers",
            )
        }

    # Incremented only for 'body_read' type reads (sequential HTTP fetches).
    # cache_hit / general_scan / footer_scan do NOT increment this.
    # Used by has_active_streams() to distinguish genuine user playback from
    # Plex intro/credit detection which may do 1–2 non-sequential fetches.
    body_read_count: int = 0
    # Monotonic timestamp of the last body_read. Used by has_active_streams()
    # to filter out idle streams (e.g. completed Plex intro scans or paused players).
    last_body_read_timestamp: float = 0.0
