import re
import threading
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from functools import cached_property
from http import HTTPStatus
from math import ceil
from time import monotonic
from typing import TYPE_CHECKING, Any, Literal, cast

import httpx
import trio
import trio_util
from kink import di
from loguru import logger
from ordered_set import OrderedSet

from program.contracts.telemetry import redact_text
from program.settings import settings_manager
from program.utils import benchmark
from program.utils.async_client import AsyncClient
from program.utils.proxy_client import ProxyClient

from .chunker import Chunk, Chunker, ChunkRange
from .config import Config
from .exceptions import (
    ByteLengthMismatchException,
    ChunksTooSlowException,
    DebridServiceClosedConnectionException,
    DebridServiceException,
    DebridServiceFairUsageLimitException,
    DebridServiceForbiddenException,
    DebridServiceLinkUnavailable,
    DebridServiceRangeNotSatisfiableException,
    DebridServiceRateLimitedException,
    DebridServiceRefusedRangeRequestException,
    DebridServiceUnableToConnectException,
    EmptyDataException,
    FatalMediaStreamException,
    MediaStreamKilledException,
    RecoverableMediaStreamException,
)
from .file_metadata import FileMetadata
from .http_pool import (
    GenerationLease,
    TrioStreamingHttpPool,
    Workload,
    admit_stream_request,
    heal_on_pool_timeout,
)
from .recent_reads import Read, RecentReads
from .session_statistics import SessionStatistics
from .stream_connection import StreamConnection
from .streaming_constants import PROXY_REQUIRED_PROVIDERS

# Guard against transient short scan reads from unstable debrid/CDN responses.
DISCRETE_SCAN_MAX_INTEGRITY_ATTEMPTS = 3
DISCRETE_SCAN_RETRY_BACKOFF_SECONDS = [0.1, 0.25]


class StreamRecoveryPhase(str, Enum):
    """Phases of transport-level connection and URL recovery."""

    IDLE = "idle"
    TRANSPORT_RETRY = "transport_retry"
    URL_REFRESH = "url_refresh"


@dataclass
class StreamRecoveryState:
    """Snapshot of active transport recovery on a stream."""

    phase: StreamRecoveryPhase = StreamRecoveryPhase.IDLE
    generation: int = 0
    chunk_start: int | None = None
    failed_url: str | None = None
    attempt: int = 0
    start_time: float = 0.0
    timeout_bound: float = 0.0
    recovery_deadline: float | None = None
    recovery_episode: int = 0


class _DeliveryState(str, Enum):
    """State of a transient in-flight delivery entry."""

    PENDING = "pending"  # Chunk is being fetched; waiters are blocked
    READY_TRANSIENT = (
        "ready_transient"  # Payload available in-memory; not yet in persistent cache
    )
    RETIRED = "retired"  # Seek or close invalidated this entry; waiters must re-probe


@dataclass
class _DeliveryEntry:
    """Lossless in-memory delivery slot for a single chunk offset."""

    start: int
    state: _DeliveryState = _DeliveryState.PENDING
    payload: bytes | None = None
    ready: trio.Event = field(default_factory=trio.Event)
    waiter_count: int = 0
    borrower_count: int = 0


class _DeliveryRegistry:
    """
    Per-MediaStream transient delivery registry.

    When Cache.put returns REFUSED_PHYSICAL_PRESSURE the producer publishes
    the already-fetched bytes here so waiting readers can consume them without
    issuing a second HTTP request (one-provider-fetch invariant).

    Thread-safety: all mutations happen inside the Trio event loop; the
    internal dict is protected by a Trio lock so concurrent waiters are safe.
    """

    def __init__(self) -> None:
        self._lock = trio.Lock()
        self._entries: dict[int, _DeliveryEntry] = {}

    async def register_pending(self, start: int) -> _DeliveryEntry:
        """Register a new PENDING slot before the fetch begins, or increment waiters on an existing active slot."""
        async with self._lock:
            existing = self._entries.get(start)
            if existing is not None and existing.state in (
                _DeliveryState.PENDING,
                _DeliveryState.READY_TRANSIENT,
            ):
                existing.waiter_count += 1
                return existing
            entry = _DeliveryEntry(start=start, waiter_count=1)
            self._entries[start] = entry
            return entry

    async def publish(self, start: int, payload: bytes) -> None:
        """Transition a PENDING entry to READY_TRANSIENT and wake waiters."""
        async with self._lock:
            entry = self._entries.get(start)
            if entry is None or entry.state != _DeliveryState.PENDING:
                return
            entry.payload = payload
            entry.state = _DeliveryState.READY_TRANSIENT
            entry.ready.set()

    async def retire(self, start: int) -> None:
        """Retire an entry (seek or close). Waiters will re-probe persistent cache."""
        async with self._lock:
            entry = self._entries.get(start)
            if entry is None:
                return
            entry.state = _DeliveryState.RETIRED
            entry.ready.set()  # Unblock any waiters so they can re-probe

    async def retire_all(self) -> None:
        """Retire all entries (stream close or seek past all pending chunks)."""
        async with self._lock:
            for entry in tuple(self._entries.values()):
                entry.state = _DeliveryState.RETIRED
                entry.ready.set()
            self._entries.clear()

    async def get_ready_payload(self, start: int) -> bytes | None:
        """
        Return payload if READY_TRANSIENT, None if RETIRED or not present.
        Does NOT wait — callers must await entry.ready first.
        """
        async with self._lock:
            entry = self._entries.get(start)
            if entry is None or entry.state != _DeliveryState.READY_TRANSIENT:
                return None
            entry.borrower_count += 1
            return entry.payload

    async def release_borrow(self, start: int) -> None:
        """Decrement waiter/borrower count; remove entry when no more active consumers."""
        async with self._lock:
            entry = self._entries.get(start)
            if entry is None:
                return
            entry.borrower_count = max(0, entry.borrower_count - 1)
            entry.waiter_count = max(0, entry.waiter_count - 1)
            if (
                entry.borrower_count == 0
                and entry.waiter_count == 0
                and entry.state
                in (_DeliveryState.READY_TRANSIENT, _DeliveryState.RETIRED)
            ):
                self._entries.pop(start, None)

    async def release_reservation(self, entry: _DeliveryEntry) -> None:
        """Release only the reservation generation owned by this reader."""
        async with self._lock:
            entry.waiter_count = max(0, entry.waiter_count - 1)
            if entry.waiter_count == 0 and entry.borrower_count == 0:
                entry.state = _DeliveryState.RETIRED
                entry.ready.set()
                if self._entries.get(entry.start) is entry:
                    self._entries.pop(entry.start)

    async def cancel_pending(self, start: int) -> None:
        """Cancel a pending registration if the read was aborted before delivery."""
        async with self._lock:
            entry = self._entries.get(start)
            if entry is None:
                return
            entry.waiter_count = max(0, entry.waiter_count - 1)
            if (
                entry.waiter_count == 0
                and entry.borrower_count == 0
                and entry.state == _DeliveryState.PENDING
            ):
                entry.state = _DeliveryState.RETIRED
                entry.ready.set()
                self._entries.pop(start, None)

    async def wait_for_delivery(
        self, start: int, timeout_seconds: float = 5.0
    ) -> bytes | None:
        """Wait for pending delivery and return the payload if ready, or None on timeout/retire."""
        async with self._lock:
            entry = self._entries.get(start)
        if entry is None:
            return None
        with trio.move_on_after(timeout_seconds):
            await entry.ready.wait()
            return await self.get_ready_payload(start)
        return None

    def has_pending(self, start: int) -> bool:
        """Sync check: is there a PENDING or READY_TRANSIENT entry for this offset?"""
        entry = self._entries.get(start)
        return entry is not None and entry.state != _DeliveryState.RETIRED

    def get_entry(self, start: int) -> _DeliveryEntry | None:
        """Sync access to an entry (for waiting on entry.ready)."""
        return self._entries.get(start)

    def get_pending_starts(self) -> list[int]:
        """Return sorted starts of entries that are actively PENDING or waiting."""
        return sorted(
            start
            for start, entry in self._entries.items()
            if entry.state == _DeliveryState.PENDING and entry.waiter_count > 0
        )


_CONTENT_RANGE_PATTERN = re.compile(
    r"^(?P<unit>[a-zA-Z]+)\s+(?P<start>\d+)-(?P<end>\d+)/(?P<total>\d+|\*)$"
)


@dataclass(frozen=True)
class ParsedContentRange:
    """Parsed RFC 9110 Content-Range header representation."""

    unit: str
    first_byte: int
    last_byte: int
    complete_length: int | None


def parse_content_range(header_val: str | None) -> ParsedContentRange | None:
    """Parse RFC 9110 Content-Range header value.

    Returns ParsedContentRange or None if missing/malformed.
    """
    if not header_val:
        return None
    match = _CONTENT_RANGE_PATTERN.match(header_val.strip())
    if not match:
        return None
    unit = match.group("unit")
    first_byte = int(match.group("start"))
    last_byte = int(match.group("end"))
    total_str = match.group("total")
    complete_length = int(total_str) if total_str != "*" else None
    return ParsedContentRange(
        unit=unit,
        first_byte=first_byte,
        last_byte=last_byte,
        complete_length=complete_length,
    )


def format_range_header(start: int, end: int | None = None) -> str:
    """Format RFC 9110 Range header for byte ranges.

    Explicitly differentiates end=None (open-ended range) from end=0 (e.g. single-byte probe).
    """
    if end is None:
        return f"bytes={start}-"
    return f"bytes={start}-{end}"


def validate_range_response(
    response: httpx.Response,
    requested_start: int,
    requested_end: int | None,
    provider: str,
    file_size: int | None = None,
) -> ParsedContentRange | None:
    """Enforce strict RFC 9110 Range integrity contract on upstream HTTP responses.

    Validates that the returned status and headers guarantee the payload matches the
    requested interval [requested_start, requested_end].

    Raises:
        DebridServiceRefusedRangeRequestException: If the response cannot be safely
            accepted as the payload for the requested range.
    """
    content_encoding = response.headers.get("Content-Encoding")
    if content_encoding is not None and content_encoding.lower().strip() not in (
        "",
        "identity",
    ):
        raise DebridServiceRefusedRangeRequestException(
            provider=provider,
            message=f"Non-identity Content-Encoding not supported for range requests: {content_encoding}",
        )

    content_type = response.headers.get("Content-Type", "")
    if content_type.lower().startswith("multipart/"):
        raise DebridServiceRefusedRangeRequestException(
            provider=provider,
            message=f"Multipart byte range response is not supported: {content_type}",
        )

    if response.status_code == HTTPStatus.OK:
        # HTTP 200 is only valid if the caller requested the full file from offset 0
        if requested_start == 0 and (
            requested_end is None
            or (file_size is not None and requested_end >= file_size - 1)
        ):
            return None

        if requested_start > 0:
            raise DebridServiceRefusedRangeRequestException(
                provider=provider,
                message=(
                    f"Upstream returned HTTP 200 for sub-range request start={requested_start}; "
                    "refusing to commit byte-0 payload to non-zero cache offset"
                ),
            )

        raise DebridServiceRefusedRangeRequestException(
            provider=provider,
            message=(
                f"Upstream returned full HTTP 200 for bounded sub-range request 0-{requested_end}"
            ),
        )

    if response.status_code == HTTPStatus.PARTIAL_CONTENT:
        content_range_hdr = response.headers.get("Content-Range")
        if not content_range_hdr:
            raise DebridServiceRefusedRangeRequestException(
                provider=provider,
                message="HTTP 206 Partial Content response missing Content-Range header",
            )

        parsed = parse_content_range(content_range_hdr)
        if parsed is None or parsed.unit.lower() != "bytes":
            raise DebridServiceRefusedRangeRequestException(
                provider=provider,
                message=f"HTTP 206 invalid or non-bytes Content-Range header: '{content_range_hdr}'",
            )

        if parsed.first_byte != requested_start:
            raise DebridServiceRefusedRangeRequestException(
                provider=provider,
                message=(
                    f"HTTP 206 Content-Range start mismatch: requested {requested_start}, "
                    f"got {parsed.first_byte} ('{content_range_hdr}')"
                ),
            )

        if parsed.last_byte < parsed.first_byte:
            raise DebridServiceRefusedRangeRequestException(
                provider=provider,
                message=f"HTTP 206 inverted Content-Range: {parsed.first_byte}-{parsed.last_byte}",
            )

        if requested_end is not None and parsed.last_byte > requested_end:
            raise DebridServiceRefusedRangeRequestException(
                provider=provider,
                message=(
                    f"HTTP 206 Content-Range end exceeds requested: requested {requested_end}, "
                    f"got {parsed.last_byte} ('{content_range_hdr}')"
                ),
            )

        content_length_hdr = response.headers.get("Content-Length")
        if content_length_hdr is not None:
            try:
                parsed_len = int(content_length_hdr)
                expected_len = parsed.last_byte - parsed.first_byte + 1
                if parsed_len != expected_len:
                    raise DebridServiceRefusedRangeRequestException(
                        provider=provider,
                        message=(
                            f"HTTP 206 Content-Length {parsed_len} contradicts "
                            f"Content-Range length {expected_len}"
                        ),
                    )
            except ValueError:
                raise DebridServiceRefusedRangeRequestException(
                    provider=provider,
                    message=f"HTTP 206 invalid Content-Length header: '{content_length_hdr}'",
                )

        return parsed

    raise DebridServiceRefusedRangeRequestException(
        provider=provider,
        message=f"Unexpected status code for range request: HTTP {response.status_code}",
    )


type ReadType = Literal[
    "header_scan",
    "footer_scan",
    "general_scan",
    "body_read",
    "footer_read",
    "cache_hit",
    "unknown",
]


def should_emit_hot_stream_trace(counter: int, every: int) -> bool:
    """Whether the Nth hot STREAM event should be logged.

    ``every=1`` logs all events (legacy behavior). ``every=50`` logs the 1st,
    51st, 101st, … event so playback stays diagnosable without drowning logs.
    """

    if every <= 1:
        return True
    if counter < 1:
        return False
    return counter % every == 1


# High-frequency read types that flood logs during sequential playback.
_HOT_STREAM_READ_TYPES: frozenset[str] = frozenset({"cache_hit", "body_read"})

# Scoped lock registry to ensure cross-MediaStream single-flight coordination per media file
_MEDIA_STREAM_REFRESH_LOCKS: dict[str, trio.Lock] = {}
_REFRESH_LOCKS_MUTEX = threading.Lock()


def _get_stream_refresh_lock(original_filename: str) -> trio.Lock:
    """Acquire or create the shared Trio lock for single-flight URL refresh coordination."""
    with _REFRESH_LOCKS_MUTEX:
        lock = _MEDIA_STREAM_REFRESH_LOCKS.get(original_filename)
        if lock is None:
            lock = trio.Lock()
            _MEDIA_STREAM_REFRESH_LOCKS[original_filename] = lock
        return lock


if TYPE_CHECKING:
    from pyfuse3 import FileHandleT
else:
    FileHandleT = Any


class MediaStream:
    """
    Represents an active streaming session for a file.

    This class manages the streaming of media content, including handling
    connections, fetching data, and managing playback.
    """

    # M-5: Class-level flag set — warnings that should only appear once per
    # process (not once per MediaStream construction during Plex library scans).
    _startup_warnings_emitted: set[str] = set()

    def __init__(
        self,
        *,
        fh: FileHandleT,
        file_size: int,
        path: str,
        original_filename: str,
        nursery: trio.Nursery,
        provider: str,
        initial_url: str,
        bitrate: int | None = None,
        http_pool: TrioStreamingHttpPool | None = None,
        require_mount_http_pool: bool = False,
    ) -> None:
        if require_mount_http_pool and http_pool is None:
            raise RuntimeError(
                "VFS MediaStream requires an injected mount-scoped Trio HTTP pool"
            )

        stream_settings = settings_manager.settings.stream
        fs = settings_manager.settings.filesystem

        self.fh = fh
        self.nursery = nursery
        self.provider = provider
        self._http_pool = http_pool
        self.recent_reads: RecentReads = RecentReads()
        self.is_streaming: trio_util.AsyncBool = trio_util.AsyncBool(False)
        self.is_killed: trio_util.AsyncBool = trio_util.AsyncBool(False)
        self._start_lock = trio.Lock()
        self._stream_error: trio_util.AsyncValue[Exception | None] = (
            trio_util.AsyncValue(None)
        )
        self.enable_tracing = settings_manager.settings.enable_stream_tracing
        self._hot_trace_counter = 0
        # Transient delivery registry: provides in-memory chunk handoff when
        # persistent cache admission is refused (REFUSED_PHYSICAL_PRESSURE).
        # Ensures the one-provider-fetch invariant: bytes fetched by the producer
        # are delivered directly to waiting readers without a second HTTP request.
        self._delivery_registry = _DeliveryRegistry()
        self._consumer_end: int | None = None
        self._inflight_chunk: tuple[int, int] | None = None
        # Per-failed-URL transport recovery budget: allows re-arming after progression on new URL
        self._last_recovered_url: str | None = None
        self._bytes_transferred_at_last_recovery: int = 0
        # Active recovery coordination (D90/D91): signals foreground reader chunk wait
        self._recovery_state: StreamRecoveryState = StreamRecoveryState()
        self._recovery_event: trio.Event = trio.Event()
        self._recovery_episode: int = 0

        # Store initial URL to avoid redundant unrestrict calls
        self.target_url: trio_util.AsyncValue[str] = trio_util.AsyncValue(initial_url)

        self.config = Config(
            chunk_size=stream_settings.chunk_size_mb * 1024 * 1024,
            activity_timeout_seconds=stream_settings.activity_timeout_seconds,
            chunk_wait_timeout_seconds=stream_settings.chunk_wait_timeout_seconds,
            connect_timeout_seconds=stream_settings.connect_timeout_seconds,
            sequential_read_tolerance_blocks=stream_settings.sequential_read_tolerance_blocks,
            scan_tolerance_blocks=stream_settings.scan_tolerance_blocks,
            prefetch_chunks=stream_settings.prefetch_chunks,
        )

        self.session_statistics = SessionStatistics()
        # Monotonic creation time used by is_timed_out to expire scan-only
        # streams (no HTTP transfer) without waiting activity_timeout_seconds.
        # Set inside trio context so trio.current_time() is valid.
        try:
            self._created_at: float = trio.current_time()
        except RuntimeError:
            # Not inside a trio event loop (e.g. tests); use 0 so the property
            # falls back to the activity-based timeout path.
            self._created_at = 0.0

        from .adaptive_prefetch import AdaptivePrefetchConfig, AdaptivePrefetchManager

        configured_prefetch = self.config.prefetch_chunks
        max_chunks = max(48, configured_prefetch) if configured_prefetch > 0 else 48

        self.adaptive_prefetch = AdaptivePrefetchManager(
            config=AdaptivePrefetchConfig(
                chunk_size_bytes=self.config.chunk_size,
                default_fallback_chunks=configured_prefetch,
                max_window_chunks=max_chunks,
                sequential_tolerance_bytes=self.config.sequential_read_tolerance,
            ),
            initial_bitrate=bitrate,
        )

        self.file_metadata = FileMetadata(
            file_size=file_size,
            path=path,
            original_filename=original_filename,
            bitrate=bitrate,
        )

        self.stream_id = f"{self.file_metadata.original_filename}:{self.fh}"

        self.chunker = Chunker(
            cache_key=self.file_metadata.original_filename,
            chunk_size=self.config.chunk_size,
            header_size=self.config.header_size,
            footer_size=self.footer_size,
            file_size=file_size,
        )

        self._trace_stream(
            f"Initialized stream with chunk size {self.config.chunk_size / (1024 * 1024):.2f} MB. "
            f"prefetch_chunks={self.config.prefetch_chunks}, "
            f"read_ahead={self.config.prefetch_chunks * self.config.chunk_size / (1024 * 1024):.0f} MB, "
            f"file_size={self.file_metadata.file_size} bytes",
        )

        # Validate cache size — emit only once so Plex scans don't flood logs.
        min_cache_mb = (self.config.chunk_size * 10) // (1024 * 1024)

        if fs.cache_max_size_mb < min_cache_mb:
            warn_key = f"cache_size:{min_cache_mb}"
            if warn_key not in MediaStream._startup_warnings_emitted:
                MediaStream._startup_warnings_emitted.add(warn_key)
                logger.warning(
                    f"Cache size ({fs.cache_max_size_mb}MB) is too small. "
                    f"Minimum recommended: {min_cache_mb}MB. "
                    "Cache thrashing may occur with concurrent reads, causing poor performance."
                )

        if stream_settings.chunk_size_mb > 8:
            warn_key = f"chunk_size:{stream_settings.chunk_size_mb}"
            if warn_key not in MediaStream._startup_warnings_emitted:
                MediaStream._startup_warnings_emitted.add(warn_key)
                logger.warning(
                    f"stream.chunk_size_mb={stream_settings.chunk_size_mb} is large; "
                    "each concurrent Plex open holds that much RAM while fetching. "
                    "Prefer 4–8 MB for multi-title playback (1–4 MB if cache_dir is "
                    "on /dev/shm or under memory pressure)."
                )

        # Use proxy client if provider requires it (resolved per-request so pool
        # recycle can swap DI singletons without restarting MediaStream).
        self._use_proxy_client = provider in PROXY_REQUIRED_PROVIDERS and bool(
            settings_manager.settings.downloaders.proxy_url
        )
        self._active_stream_connection: StreamConnection | None = None

    def _resolve_async_client(self) -> httpx.AsyncClient:
        """Return the current streaming client (from injected pool or DI fallback)."""

        if self._http_pool is not None:
            return self._http_pool.get_client(use_proxy=self._use_proxy_client)

        if self._use_proxy_client:
            return di[ProxyClient]

        return di[AsyncClient]

    def _update_runway_observation(self, position: int) -> None:
        """Publish contiguous cached runway for the generic mount pressure signal."""
        manager = getattr(self, "adaptive_prefetch", None)
        if manager is None:
            return
        try:
            window = self.chunker.get_chunk_range(
                position=position,
                size=self.config.chunk_size * manager.config.max_window_chunks,
            )
            cached_bytes = 0
            in_flight_bytes = 0
            inflight = getattr(self, "_inflight_chunk", None)
            for chunk in window.chunks:
                remaining = chunk.end - max(position, chunk.start) + 1
                if chunk.is_cached.value:
                    cached_bytes += remaining
                elif inflight == (manager.current_generation, chunk.start):
                    # Only the chunk actually being fetched, never reader reservations.
                    in_flight_bytes += remaining
                    break
                else:
                    break
            manager.record_runway(
                cached_bytes=cached_bytes,
                in_flight_bytes=in_flight_bytes,
            )
        except Exception:
            # Runway telemetry must never affect delivery correctness.
            return

    def _prefetch_window(self) -> int:
        """Keep the existing adaptive resource bounds on every refill turn."""
        from .cache import Cache

        cache = di[Cache] if Cache in di else None

        def pressure(name: str) -> float:
            value = getattr(cache, name, 0.0)
            result = value() if callable(value) else value
            if isinstance(result, (int, float)):
                return float(result)
            return 0.0

        return self.adaptive_prefetch.calculate_window(
            active_leases=getattr(self._http_pool, "active_leases", 0),
            cache_usage_pct=pressure("usage_percentage"),
            cache_protected_pct=pressure("protected_usage_percentage"),
        )

    async def _force_aclose_active_response(self) -> None:
        """Best-effort close of an in-flight httpx response to free pool slots."""

        connection = self._active_stream_connection
        self._active_stream_connection = None

        if connection is None:
            return

        try:
            await connection.response.aclose()
        except Exception:
            logger.debug(
                self.build_log_message("Failed to aclose active stream response")
            )

    def _trace_stream(self, message: str, *, hot: bool = False) -> None:
        """Emit a STREAM log line, optionally sampling high-frequency events."""

        if not self.enable_tracing:
            return
        if hot:
            self._hot_trace_counter += 1
            every = settings_manager.settings.stream_tracing_sample_every
            if not should_emit_hot_stream_trace(self._hot_trace_counter, every):
                return
        logger.log("STREAM", self.build_log_message(message))

    def __repr__(self) -> str:
        return (
            f"<MediaStream[{self.provider}] "
            f"fh={self.fh} "
            f"path={self.file_metadata.path} "
            f"session_statistics={self.session_statistics} "
            f"last_read_timestamp={self.recent_reads.current_read.value.timestamp if self.recent_reads.current_read.value else None} "
            f"is_timed_out={self.is_timed_out} "
            f"is_streaming={self.is_streaming.value} "
            f"file_size={self.file_metadata.file_size} "
            ">"
        )

    @cached_property
    def footer_size(self) -> int:
        """An optimal footer size for scanning based on file size."""

        # Use a percentage-based approach for requesting the footer
        # using the file size to determine an appropriate range.

        min_footer_size = 1024 * 16  # Minimum footer size of 16KB
        max_footer_size = 10 * 1024 * 1024  # Maximum footer size of 10MB
        footer_percentage = 0.002  # 0.2% of file size

        percentage_size = int(self.file_metadata.file_size * footer_percentage)

        raw_footer_size = min(max(percentage_size, min_footer_size), max_footer_size)
        aligned_footer_size = (
            -(raw_footer_size // -self.config.block_size) * self.config.block_size
        )

        return aligned_footer_size

    @property
    def is_timed_out(self) -> bool:
        try:
            current_time = trio.current_time()
        except RuntimeError:
            # This property can be inspected by synchronous diagnostics outside
            # the Trio-owned VFS event loop.
            return False

        if not self.recent_reads.current_read.value:
            # Stream was opened but never triggered an HTTP fetch — it is a
            # Plex scan / intro-detection read that was served entirely from
            # the /dev/shm cache.  Time it out quickly so it does not hold
            # an _active_streams slot and falsely block the Downloader.
            if self._created_at <= 0.0:
                return False  # Safety: no trio context at construction.
            scan_timeout = min(30.0, float(self.config.activity_timeout_seconds))
            return current_time - self._created_at > scan_timeout
        return (
            current_time - self.recent_reads.current_read.value.timestamp
            > self.config.activity_timeout_seconds
        )

    @property
    def created_at(self) -> float:
        """Monotonic creation timestamp in trio time (or 0.0 if construct-time capture failed)."""
        return self._created_at

    @asynccontextmanager
    async def stream_lifecycle(self) -> AsyncGenerator[None]:
        """Context manager for managing stream lifecycle."""

        try:
            self.is_streaming.value = True

            self._trace_stream("Starting stream lifecycle")

            yield
        finally:
            self.is_streaming.value = False

            self._trace_stream("Stream lifecycle ended")

    @staticmethod
    def _response_context(response: httpx.Response) -> str:
        """Return the range headers needed to distinguish EOF from cancellation."""
        content_range = response.headers.get("Content-Range", "missing")
        content_length = response.headers.get("Content-Length", "missing")
        return (
            f"status={response.status_code}, content-range={content_range}, "
            f"content-length={content_length}"
        )

    async def _run_opportunistic_prefetch(
        self,
        chunks: OrderedSet[Chunk],
        process_chunks: Callable[..., Awaitable[None]],
        *,
        label: str,
        prefetch_gen: int | None = None,
    ) -> bool:
        """Run prefetch without poisoning reads when an idle CDN socket is empty."""
        try:
            # Re-evaluate after every publication; never hold a stale batch while
            # foreground cache hits advance the consumer. No extra task/socket.
            manager = getattr(self, "adaptive_prefetch", None)
            consumer_end = getattr(self, "_consumer_end", None)

            # Fallback for bare test doubles or streams without active consumer context:
            # execute the provided chunks directly.
            if manager is None or consumer_end is None:
                await process_chunks(chunks)
                return True

            while chunks and not getattr(self.is_killed, "value", False):
                if prefetch_gen is not None and manager.should_abort_prefetch(
                    prefetch_gen
                ):
                    break
                consumer_end = getattr(self, "_consumer_end", None)
                if consumer_end is None:
                    break
                self._update_runway_observation(consumer_end + 1)
                window = self._prefetch_window()
                if label != "Prefetch gap-fill":
                    chunks = self.chunker.get_prefetch_uncached(
                        after_end=consumer_end,
                        count=window,
                    )
                if not chunks:
                    break
                chunk = chunks[0]
                # Foreground reservations elsewhere always get the next turn.
                if any(
                    start != chunk.start
                    and not self.chunker.get_chunk_range(
                        position=start
                    ).first_chunk.is_cached.value
                    for start in self._delivery_registry.get_pending_starts()
                ):
                    break
                await process_chunks(
                    OrderedSet([chunk]),
                    is_prefetch=True,
                    prefetch_gen=prefetch_gen,
                )
                connection = self._active_stream_connection
                if connection is not None and connection.seek_required.value:
                    break
                if not chunk.is_cached.value:
                    # Refused admission is not runway. Stop rather than spin or
                    # fetch the next chunk across an unfillable hole.
                    break
                if label == "Prefetch gap-fill":
                    chunks.discard(chunk)
        except EmptyDataException as error:
            self._trace_stream(
                f"{label} did not return data ({error}); keeping the stream "
                "available for the next playhead read"
            )
            return False

        return True

    @asynccontextmanager
    async def manage_connection(
        self,
        *,
        position: int,
    ) -> AsyncGenerator[StreamConnection, None]:
        """Context manager to handle connection lifecycle."""

        try:
            try:
                async with self.connect(position=position) as connection:
                    yield connection
            except BaseExceptionGroup as group:
                # Trio's reconnect watcher wraps body failures. Unwrap only a
                # single leaf; mixed failures/cancellation retain their semantics.
                error: BaseException = group
                while isinstance(error, BaseExceptionGroup):
                    children = cast(tuple[BaseException, ...], error.exceptions)
                    if len(children) != 1:
                        break
                    error = children[0]
                if isinstance(
                    error,
                    (
                        httpx.ReadError,
                        httpx.TimeoutException,
                        httpx.RemoteProtocolError,
                    ),
                ):
                    raise error from group
                raise
        except (
            EmptyDataException,
            DebridServiceRateLimitedException,
            DebridServiceRefusedRangeRequestException,
            DebridServiceClosedConnectionException,
            httpx.ReadError,
            httpx.TimeoutException,
            httpx.RemoteProtocolError,
        ) as e:
            logger.exception(
                self.build_log_message(
                    f"{e.__class__.__name__} occurred whilst managing stream connection: {e}"
                )
            )

            raise RecoverableMediaStreamException(e) from e
        except (
            DebridServiceUnableToConnectException,
            DebridServiceForbiddenException,
            DebridServiceRangeNotSatisfiableException,
            DebridServiceFairUsageLimitException,
        ) as e:
            logger.exception(
                self.build_log_message(
                    f"{e.__class__.__name__} occurred whilst managing stream connection: {e}"
                )
            )

            raise FatalMediaStreamException(e) from e

    async def run(
        self,
        position: int,
        *,
        task_status: trio.TaskStatus = trio.TASK_STATUS_IGNORED,
    ) -> None:
        has_started = False

        async with self.stream_lifecycle():
            async with trio_util.move_on_when(lambda: self.is_killed.wait_value(True)):
                attempt_count = 0
                max_attempts = 4
                # Tracks whether the download URL has been refreshed for the
                # current run() invocation; reset once per retry cycle.
                url_refresh_attempted = False
                # Set to True by _process_chunks when it reads 0 bytes, so the
                # outer retry loop can refresh the URL before reconnecting.
                needs_url_refresh = False

                seek_range: ChunkRange | None = None
                run_generation = getattr(
                    getattr(self, "adaptive_prefetch", None), "current_generation", 0
                )

                while True:
                    if not self._transport_generation_live(run_generation):
                        if self.is_killed.value:
                            break
                        latest_read = self.recent_reads.current_read.value
                        if latest_read is not None:
                            seek_range = latest_read.chunk_range
                            position = seek_range.position
                        attempt_count = 0
                    failed_connection: StreamConnection | None = None
                    run_generation = getattr(
                        getattr(self, "adaptive_prefetch", None),
                        "current_generation",
                        0,
                    )
                    try:
                        async with self.manage_connection(
                            position=position
                        ) as connection:
                            failed_connection = connection
                            if not has_started:
                                task_status.started()
                                has_started = True

                            async with trio_util.move_on_when(
                                lambda connection=connection: trio_util.wait_any(
                                    # Reconnect the stream if a seek is requested
                                    lambda: connection.seek_required.wait_value(True),
                                    # Reconnect the stream if the target URL has been updated
                                    # by another request (e.g. a scan that refreshed the URL).
                                    lambda: self.target_url.wait_value(
                                        lambda url: (
                                            url != connection.response.request.url
                                        )
                                    ),
                                )
                            ) as reconnect_scope:

                                async def _process_chunks(
                                    chunks: OrderedSet[Chunk],
                                    *,
                                    is_prefetch: bool = False,
                                    prefetch_gen: int | None = None,
                                ) -> None:
                                    nonlocal needs_url_refresh, position
                                    if len(chunks) == 0:
                                        if self.enable_tracing:
                                            logger.log(
                                                "STREAM",
                                                self.build_log_message(
                                                    "Received no chunks to process; skipping."
                                                ),
                                            )

                                        return

                                    if is_prefetch and prefetch_gen is not None:
                                        if hasattr(
                                            self, "adaptive_prefetch"
                                        ) and self.adaptive_prefetch.should_abort_prefetch(
                                            prefetch_gen
                                        ):
                                            self._trace_stream(
                                                "Prefetch aborted before start: seek detected"
                                            )
                                            return

                                    if self.enable_tracing:
                                        logger.log(
                                            "STREAM",
                                            self.build_log_message(
                                                f"Received chunks to process: {chunks}"
                                            ),
                                        )

                                    chunk_range_label = (
                                        f"{chunks[0].index}"
                                        if len(chunks) == 1
                                        else f"{chunks[0].index}-{chunks[-1].index}"
                                    )

                                    start_read_position = (
                                        connection.current_read_position
                                    )

                                    with benchmark(
                                        log=lambda duration, conn=connection, start=start_read_position: (
                                            logger.log(
                                                "STREAM",
                                                self.build_log_message(
                                                    f"Stream fetch interrupted at byte {start} "
                                                    f"before data transfer in {duration}s "
                                                    "(seek, URL refresh, or file close)."
                                                    if conn.current_read_position
                                                    == start
                                                    and (
                                                        reconnect_scope.cancel_called
                                                        or self.is_killed.value
                                                    )
                                                    else f"Stream fetched {start}-{conn.current_read_position} "
                                                    f"({conn.current_read_position - start} bytes) "
                                                    f"in {duration}s."
                                                ),
                                            )
                                            if self.enable_tracing
                                            else None
                                        )
                                    ):
                                        for chunk in chunks:
                                            if is_prefetch and prefetch_gen is not None:
                                                if hasattr(
                                                    self, "adaptive_prefetch"
                                                ) and self.adaptive_prefetch.should_abort_prefetch(
                                                    prefetch_gen
                                                ):
                                                    self._trace_stream(
                                                        f"Prefetch loop aborted before chunk #{chunk.index}: seek detected"
                                                    )
                                                    return

                                            if (
                                                connection.current_read_position
                                                != chunk.start
                                            ):
                                                # ``get_prefetch_uncached`` may skip a
                                                # chunk that another handle already
                                                # cached. A streaming HTTP response
                                                # cannot jump across that hole: reading
                                                # on would store the skipped bytes under
                                                # the next chunk's key. Reconnect at the
                                                # exact uncached boundary instead.
                                                self._trace_stream(
                                                    "Repositioning stream across cached "
                                                    f"prefetch gap from "
                                                    f"{connection.current_read_position} "
                                                    f"to {chunk.start}"
                                                )
                                                connection.seek(
                                                    chunk_range=self.chunker.get_chunk_range(
                                                        position=chunk.start,
                                                        size=chunk.size,
                                                    )
                                                )
                                                return

                                            chunk_label = f"[{chunk.start}-{chunk.end}]"

                                            with benchmark(
                                                log=lambda duration, c=chunk: (
                                                    logger.log(
                                                        "STREAM",
                                                        self.build_log_message(
                                                            f"Fetching {c} took {duration}s"
                                                        ),
                                                    )
                                                    if self.enable_tracing
                                                    else None
                                                )
                                            ):
                                                fetch_started = monotonic()
                                                ap_mgr = getattr(
                                                    self, "adaptive_prefetch", None
                                                )
                                                fetch_generation = (
                                                    ap_mgr.current_generation
                                                    if ap_mgr is not None
                                                    else 0
                                                )
                                                self._inflight_chunk = (
                                                    fetch_generation,
                                                    chunk.start,
                                                )
                                                chunk_buffer = bytearray()
                                                wait_sec = float(
                                                    self.config.chunk_wait_timeout_seconds
                                                )
                                                headroom = (
                                                    3.0
                                                    if wait_sec >= 4.0
                                                    else (
                                                        1.0 if wait_sec >= 2.0 else 0.5
                                                    )
                                                )
                                                body_read_stall_timeout = min(
                                                    7.0,
                                                    max(0.5, wait_sec - headroom),
                                                )
                                                while len(chunk_buffer) < chunk.size:
                                                    if (
                                                        is_prefetch
                                                        and prefetch_gen is not None
                                                    ):
                                                        if hasattr(
                                                            self,
                                                            "adaptive_prefetch",
                                                        ) and self.adaptive_prefetch.should_abort_prefetch(
                                                            prefetch_gen
                                                        ):
                                                            self._trace_stream(
                                                                f"Prefetch read aborted mid-chunk #{chunk.index}: seek detected"
                                                            )
                                                            if len(chunk_buffer) > 0:
                                                                # Partial bytes consumed from socket; reposition connection before next demand
                                                                connection.seek(
                                                                    chunk_range=self.chunker.get_chunk_range(
                                                                        position=chunk.start,
                                                                        size=chunk.size,
                                                                    )
                                                                )
                                                            return
                                                    try:
                                                        with trio.fail_after(
                                                            body_read_stall_timeout
                                                        ):
                                                            raw_part = await anext(
                                                                connection.reader
                                                            )
                                                    except trio.TooSlowError as err:
                                                        raise httpx.ReadTimeout(
                                                            f"HTTP response-body read stalled for {body_read_stall_timeout:.1f}s"
                                                        ) from err
                                                    except StopAsyncIteration:
                                                        break
                                                    if not raw_part:
                                                        break
                                                    chunk_buffer.extend(raw_part)
                                                data = bytes(chunk_buffer)
                                                if (
                                                    is_prefetch
                                                    and hasattr(
                                                        self, "adaptive_prefetch"
                                                    )
                                                    and self.adaptive_prefetch.should_abort_prefetch(
                                                        fetch_generation
                                                    )
                                                ):
                                                    self._inflight_chunk = None
                                                    connection.seek(
                                                        chunk_range=self.chunker.get_chunk_range(
                                                            position=chunk.start
                                                        )
                                                    )
                                                    return

                                            if not data:
                                                # Signal the outer loop to refresh the URL
                                                # before the next reconnect attempt.
                                                needs_url_refresh = True

                                                raise EmptyDataException(
                                                    range=(chunk.start, chunk.end)
                                                )

                                            expected_chunk_size = chunk.size
                                            if (
                                                chunk.start
                                                < self.file_metadata.file_size
                                            ):
                                                expected_chunk_size = min(
                                                    chunk.size,
                                                    self.file_metadata.file_size
                                                    - chunk.start,
                                                )

                                            if len(data) < expected_chunk_size:
                                                logger.warning(
                                                    self.build_log_message(
                                                        f"Incomplete chunk read for {chunk}: expected {expected_chunk_size} bytes, got {len(data)} bytes; "
                                                        "discarding partial chunk to prevent cache corruption"
                                                    )
                                                )
                                                raise RecoverableMediaStreamException(
                                                    EmptyDataException(
                                                        range=(chunk.start, chunk.end)
                                                    )
                                                )

                                            with benchmark(
                                                log=lambda duration, label=chunk_label, range_label=chunk_range_label: (
                                                    logger.log(
                                                        "STREAM",
                                                        self.build_log_message(
                                                            f"Processing chunk(s) #{range_label} {label} took {duration}s"
                                                        ),
                                                    )
                                                    if self.enable_tracing
                                                    else None
                                                )
                                            ):
                                                connection.increment_sequential_chunks()

                                                await self._cache_chunk(
                                                    start=chunk.start,
                                                    data=data,
                                                    admission=(
                                                        "prefetch"
                                                        if is_prefetch
                                                        and not self._delivery_registry.has_pending(
                                                            chunk.start
                                                        )
                                                        else "demand"
                                                    ),
                                                )
                                                self._inflight_chunk = None
                                                if hasattr(self, "adaptive_prefetch"):
                                                    self.adaptive_prefetch.record_provider_fetch(
                                                        monotonic() - fetch_started
                                                    )

                                                chunk.emit_cache_signal()

                                                connection.current_read_position += len(
                                                    data
                                                )

                                                position = (
                                                    connection.current_read_position
                                                )
                                                if self._consumer_end is not None:
                                                    self._update_runway_observation(
                                                        self._consumer_end + 1
                                                    )

                                                self.session_statistics.bytes_transferred += len(
                                                    data
                                                )

                                if seek_range:
                                    await _process_chunks(seek_range.uncached_chunks)
                                    seek_range = None

                                # Read.current_read is intentionally a latest-value
                                # signal, not a queue. Drain every registered body
                                # reservation first so a rapid subsequent read cannot
                                # erase an earlier producer demand.
                                for (
                                    pending_start
                                ) in self._delivery_registry.get_pending_starts():
                                    pending_range = self.chunker.get_chunk_range(
                                        position=pending_start, size=1
                                    )
                                    pending_chunks = pending_range.uncached_chunks
                                    if not pending_chunks:
                                        continue
                                    first_pending = pending_chunks[0]
                                    if (
                                        connection.current_read_position
                                        < first_pending.start
                                    ):
                                        connection.seek(chunk_range=pending_range)
                                        break
                                    if (
                                        first_pending.start
                                        < connection.current_read_position
                                    ):
                                        continue
                                    await _process_chunks(OrderedSet([first_pending]))

                                async for (
                                    read
                                ) in self.recent_reads.current_read.eventual_values(
                                    lambda v: (
                                        v is not None
                                        and v.read_type in ("body_read", "cache_hit")
                                    )
                                ):
                                    if not read:
                                        raise ValueError(
                                            self.build_log_message("No read available")
                                        )

                                    uncached_chunks = read.chunk_range.uncached_chunks

                                    if len(uncached_chunks) > 0:
                                        if self.enable_tracing:
                                            logger.log(
                                                "STREAM",
                                                self.build_log_message(
                                                    f"Received read event: {read} with uncached_chunks {uncached_chunks}"
                                                ),
                                            )

                                        request_start, _ = (
                                            read.chunk_range.request_range
                                        )

                                        if (
                                            self.config.header_size
                                            < uncached_chunks[0].start
                                            < connection.start_position
                                        ):
                                            # Backward seek detection:
                                            #
                                            # If the requested start is before the start of the stream, we will always need to seek.
                                            # This is because streams can only read forwards, so a new connection must be made.

                                            if self.enable_tracing:
                                                logger.log(
                                                    "STREAM",
                                                    self.build_log_message(
                                                        f"Requested start {request_start} "
                                                        f"is before current read position {connection.current_read_position} "
                                                        f"for {self.file_metadata.path}. "
                                                        f"Seeking to new start position {uncached_chunks[0].start}/{self.file_metadata.file_size}."
                                                    ),
                                                )

                                            connection.seek(
                                                chunk_range=read.chunk_range
                                            )

                                            break

                                        if (
                                            connection.current_read_position
                                            < uncached_chunks[0].start
                                        ):
                                            # Forward seek detection:
                                            #
                                            # If the requested start is after the current read position, we need to seek forward.
                                            # This is because streams cannot skip chunks of data, so a new connection must be made,
                                            # to avoid requesting data that will be discarded and using unnecessary bandwidth.

                                            if self.enable_tracing:
                                                logger.log(
                                                    "STREAM",
                                                    self.build_log_message(
                                                        f"Request chunk start {uncached_chunks[0].start} "
                                                        f"is after current read position {connection.current_read_position} "
                                                        f"for {self.file_metadata.path}. "
                                                        f"Seeking to new start position {uncached_chunks[0].start}/{self.file_metadata.file_size}."
                                                    ),
                                                )

                                            connection.seek(
                                                chunk_range=read.chunk_range
                                            )

                                            break

                                        await _process_chunks(uncached_chunks)

                                    # Drain any other concurrently queued demands that arrived
                                    # while processing this read before waiting for the next
                                    # event, ensuring older or parallel demands are never starved.
                                    for (
                                        pending_start
                                    ) in self._delivery_registry.get_pending_starts():
                                        pending_range = self.chunker.get_chunk_range(
                                            position=pending_start, size=1
                                        )
                                        pending_chunks = pending_range.uncached_chunks
                                        if not pending_chunks:
                                            continue
                                        first_pending = pending_chunks[0]
                                        if (
                                            connection.current_read_position
                                            < first_pending.start
                                        ):
                                            connection.seek(chunk_range=pending_range)
                                            break
                                        if (
                                            first_pending.start
                                            < connection.current_read_position
                                        ):
                                            continue
                                        await _process_chunks(
                                            OrderedSet([first_pending])
                                        )

                                    # Sequential playhead prefetch: fill ahead without
                                    # blocking the current VFS read (already returned /
                                    # waiting independently via is_cached).
                                    consumer_end = getattr(self, "_consumer_end", None)
                                    if consumer_end is None and hasattr(
                                        read, "chunk_range"
                                    ):
                                        consumer_end = getattr(
                                            read.chunk_range,
                                            "request_range",
                                            (None, None),
                                        )[1]
                                    ap_manager = getattr(
                                        self, "adaptive_prefetch", None
                                    )
                                    should_prefetch = (
                                        ap_manager is None
                                        or ap_manager.should_prefetch_now()
                                        or ap_manager.get_effective_bitrate() <= 0
                                    )
                                    if (
                                        self.config.prefetch_chunks > 0
                                        and consumer_end is not None
                                        and should_prefetch
                                    ):
                                        active_leases = 0
                                        pool = getattr(self, "_http_pool", None)
                                        if pool is not None:
                                            try:
                                                active_leases = pool.active_leases
                                            except Exception:
                                                pass

                                        cache_usage = 0.0
                                        cache_protected = 0.0
                                        try:
                                            from .cache import Cache

                                            if Cache in di:
                                                cache_inst: Any = di[Cache]
                                                if hasattr(
                                                    cache_inst, "usage_percentage"
                                                ):
                                                    u_val: Any = (
                                                        cache_inst.usage_percentage
                                                    )
                                                    raw_u: Any = (
                                                        u_val()
                                                        if callable(u_val)
                                                        else u_val
                                                    )
                                                    cache_usage = float(
                                                        cast(float, raw_u)
                                                    )
                                                if hasattr(
                                                    cache_inst,
                                                    "protected_usage_percentage",
                                                ):
                                                    p_val: Any = (
                                                        cache_inst.protected_usage_percentage
                                                    )
                                                    raw_p: Any = (
                                                        p_val()
                                                        if callable(p_val)
                                                        else p_val
                                                    )
                                                    cache_protected = float(
                                                        cast(float, raw_p)
                                                    )
                                        except Exception:
                                            pass

                                        prefetch_window = (
                                            self.adaptive_prefetch.calculate_window(
                                                active_leases=active_leases,
                                                cache_usage_pct=cache_usage,
                                                cache_protected_pct=cache_protected,
                                            )
                                            if hasattr(self, "adaptive_prefetch")
                                            else self.config.prefetch_chunks
                                        )

                                        if prefetch_window > 0:
                                            playhead_end = consumer_end
                                            ahead = self.chunker.get_prefetch_uncached(
                                                after_end=playhead_end,
                                                count=prefetch_window,
                                            )
                                            if ahead:
                                                prefetch_gen = (
                                                    self.adaptive_prefetch.current_generation
                                                    if hasattr(
                                                        self, "adaptive_prefetch"
                                                    )
                                                    else None
                                                )
                                                if self.enable_tracing:
                                                    logger.log(
                                                        "STREAM",
                                                        self.build_log_message(
                                                            f"Prefetching {len(ahead)} chunk(s) (window={prefetch_window}) "
                                                            f"ahead of playhead@{playhead_end}"
                                                        ),
                                                    )
                                                # Prefer contiguous fetch from connection tip;
                                                # seek if gap to first prefetch chunk.
                                                first = ahead[0]
                                                if (
                                                    connection.current_read_position
                                                    < first.start
                                                ):
                                                    if (
                                                        first.start
                                                        - connection.current_read_position
                                                        <= self.config.chunk_size * 2
                                                    ):
                                                        gap_range = self.chunker.get_chunk_range(
                                                            position=connection.current_read_position,
                                                            size=first.start
                                                            - connection.current_read_position,
                                                        )
                                                        if gap_range.uncached_chunks:
                                                            prefetched = await self._run_opportunistic_prefetch(
                                                                gap_range.uncached_chunks,
                                                                _process_chunks,
                                                                label="Prefetch gap-fill",
                                                                prefetch_gen=prefetch_gen,
                                                            )
                                                            if not prefetched:
                                                                needs_url_refresh = (
                                                                    False
                                                                )
                                                                break
                                                    else:
                                                        connection.seek(
                                                            chunk_range=self.chunker.get_chunk_range(
                                                                position=first.start,
                                                                size=first.size,
                                                            )
                                                        )
                                                        break
                                                prefetched = await self._run_opportunistic_prefetch(
                                                    ahead,
                                                    _process_chunks,
                                                    label="Prefetch",
                                                    prefetch_gen=prefetch_gen,
                                                )
                                                if not prefetched:
                                                    needs_url_refresh = False
                                                    break

                            position = connection.current_read_position
                            seek_range = connection.seek_range
                            if reconnect_scope.cancelled_caught:
                                if connection.seek_required.value:
                                    reconnect_reason = "seek requested"
                                elif self.target_url.value != str(
                                    connection.response.request.url
                                ):
                                    reconnect_reason = "download URL updated"
                                else:
                                    reconnect_reason = "connection scope cancelled"
                                self._trace_stream(
                                    f"Restarting stream connection: {reconnect_reason}"
                                )
                    except RecoverableMediaStreamException as e:
                        logger.warning(
                            self.build_log_message(
                                f"Recoverable error from stream: {e.original_exception}. Attempting to reconnect..."
                            )
                        )

                        inflight = getattr(self, "_inflight_chunk", None)
                        chunk_start = inflight[1] if inflight else position
                        self._set_recovery_state(
                            StreamRecoveryPhase.TRANSPORT_RETRY,
                            generation=run_generation,
                            chunk_start=chunk_start,
                            failed_url=(
                                str(failed_connection.response.request.url)
                                if failed_connection
                                else None
                            ),
                            attempt=attempt_count + 1,
                            timeout_bound=10.0,
                        )

                        # If the connection returned 0 bytes (URL expired/exhausted),
                        # refresh the download URL before the next retry so we don't
                        # loop forever on a dead debrid link.
                        if needs_url_refresh and not url_refresh_attempted:
                            url_refresh_attempted = True
                            await self._refresh_download_url()

                        should_retry = await self._retry_with_backoff(
                            attempt_count,
                            max_attempts,
                            [0.2, 0.5, 1.0],
                        )

                        if should_retry:
                            attempt_count += 1
                            # Reset per-cycle flags so a sustained URL expiry can
                            # attempt a fresh refresh on the next retry.
                            url_refresh_attempted = False
                            needs_url_refresh = False

                            continue

                        # Body iterator failures must be retried here, never by
                        # yielding a second response from establish_connection().
                        if (
                            failed_connection is not None
                            and isinstance(
                                e.original_exception,
                                (
                                    httpx.ReadError,
                                    httpx.RemoteProtocolError,
                                    httpx.TimeoutException,
                                ),
                            )
                            and not isinstance(e.original_exception, httpx.PoolTimeout)
                            and await self._recover_transport_url(
                                str(failed_connection.response.request.url),
                                run_generation,
                            )
                        ):
                            attempt_count = 0
                            continue

                        self._set_recovery_state(
                            StreamRecoveryPhase.IDLE, generation=run_generation
                        )

                        # A seek during refresh supersedes this connection's error.
                        # The next turn selects the latest demand, not the failed tip.
                        if not self._transport_generation_live(run_generation):
                            continue

                        # All retries exhausted — reset attempt count so the next
                        # connection attempt (after break/restart) starts fresh.
                        attempt_count = 0
                        if hasattr(self, "_stream_error"):
                            self._stream_error.value = e.original_exception

                        # FIX-03: Signal readiness before breaking so Trio's nursery.start()
                        # does not raise RuntimeError("task exited without calling task_status.started()").
                        if not has_started:
                            task_status.started()
                            has_started = True

                        break
                    except FatalMediaStreamException as e:
                        logger.exception(
                            self.build_log_message(
                                f"Fatal error from stream: {e.original_exception}. Terminating."
                            )
                        )

                        if hasattr(self, "_stream_error"):
                            self._stream_error.value = e.original_exception

                        # FIX-03: Signal readiness so the caller is not left hanging.
                        if not has_started:
                            task_status.started()
                            has_started = True

                        break
                    except Exception as e:
                        # Safely catch any other unexpected exceptions to avoid crashing the FUSE mount
                        logger.exception(
                            self.build_log_message(f"Unexpected error from stream: {e}")
                        )

                        if hasattr(self, "_stream_error"):
                            self._stream_error.value = e

                        # FIX-03: Signal readiness so the caller is not left hanging.
                        if not has_started:
                            task_status.started()
                            has_started = True

                        break

    @asynccontextmanager
    async def connect(self, *, position: int) -> AsyncGenerator[StreamConnection]:
        """Establish a streaming connection starting at the given byte offset, aligned to the closest chunk."""

        chunk_range = self.chunker.get_chunk_range(position=position)

        chunk_aligned_start = (
            chunk_range.uncached_chunks[0].start
            if len(chunk_range.uncached_chunks) > 0
            else max(self.config.header_size, chunk_range.first_chunk.start)
        )

        async with self.establish_connection(start=chunk_aligned_start) as response:
            stream_connection = StreamConnection(
                response=response,
                start_position=chunk_aligned_start,
                current_read_position=chunk_aligned_start,
                reader=response.aiter_raw(chunk_size=self.config.chunk_size),
            )
            self._active_stream_connection = stream_connection

            if self.enable_tracing:
                logger.log(
                    "STREAM",
                    self.build_log_message(
                        f"{response.http_version} stream connection established "
                        f"from byte {chunk_aligned_start} / {self.file_metadata.file_size}; "
                        f"{self._response_context(response)}."
                    ),
                )

            try:
                self._set_recovery_state(StreamRecoveryPhase.IDLE)
                yield stream_connection
            finally:
                self._inflight_chunk = None
                if self._active_stream_connection is stream_connection:
                    self._active_stream_connection = None

    async def close(self) -> None:
        """Immediately terminate the active stream."""

        # Fence even an idle/cache-only stream before any further refill.
        self.is_killed.value = True
        self._inflight_chunk = None
        self._set_recovery_state(StreamRecoveryPhase.IDLE)
        # First wait for the stream to stop, then close the client
        if self.is_streaming.value:
            # FIX-04: Do NOT call clear_emitters() here.
            # Unconditionally clearing emitters when one stream closes wipes the chunk
            # AsyncBool notifiers that a concurrently-running stream for the same file
            # is waiting on, causing its downloader to signal the old emitter while the
            # surviving reader is permanently hung on a new one → ChunksTooSlowException.
            # The ChunkCacheNotifier uses an LRU (maxsize=4096) for automatic memory
            # management — manual eviction on close is unnecessary and dangerous.

            # Wait for the stream loop to close
            try:
                with trio.fail_after(5):
                    self.is_killed.value = True
                    await self.is_streaming.wait_value(False)
            except trio.TooSlowError:
                logger.warning(
                    self.build_log_message("Stream didn't stop within 5 seconds")
                )

        # Always attempt to free the httpx pool slot even if kill timed out.
        await self._force_aclose_active_response()

        # Retire all pending transient delivery entries so any blocked readers
        # are unblocked and can fall through to the HTTP fallback path.
        await self._delivery_registry.retire_all()

        # Free active playback lease reservations for this stream in the VFS cache
        try:
            from .cache import Cache

            if Cache in di and hasattr(self, "stream_id"):
                di[Cache].release_stream(self.stream_id)
        except Exception:
            pass

        if self.enable_tracing:
            logger.log(
                "STREAM",
                self.build_log_message(
                    f"Ended stream for {self.file_metadata.path} fh={self.fh} "
                    f"after transferring {self.session_statistics.bytes_transferred / (1024 * 1024):.2f}MB "
                    f"in {self.session_statistics.total_session_connections} connections."
                ),
            )

    async def scan(self, read_position: int, size: int) -> bytes:
        """Fetch a one-off range of data for scanning purposes.

        Results are cached so repeated Plex intro/credit probes for the same
        byte range are served from /dev/shm instead of making a fresh debrid
        HTTP request on every call.
        """

        data = await self._fetch_discrete_byte_range(
            start=read_position,
            size=size,
        )

        return data[:size]

    async def scan_header(self, read_position: int, size: int) -> bytes:
        """Scans the start of the media file for header data."""

        data = await self._fetch_discrete_byte_range(
            start=self.chunker.header_chunk.start,
            size=self.chunker.header_chunk.size,
        )

        self.chunker.header_chunk.emit_cache_signal()

        return data[read_position : read_position + size]

    async def scan_footer(self, read_position: int, size: int) -> bytes:
        """
        Scans the end of the media file for footer data.

        This "over-fetches" for the individual request,
        but multiple footer requests tend to be made to retrieve more data later,
        so this ends up being more efficient than making multiple small requests.
        """

        footer_chunk = self.chunker.footer_chunk

        data = await self._fetch_discrete_byte_range(
            start=footer_chunk.start,
            size=footer_chunk.size,
        )

        self.chunker.footer_chunk.emit_cache_signal()

        slice_offset = read_position - footer_chunk.start

        return data[slice_offset : slice_offset + size]

    @asynccontextmanager
    async def capture_stream_errors(self) -> AsyncGenerator[None, None]:
        """Context manager to capture and log stream errors."""

        # Handle the read request whilst monitoring for stream kill signals, and errors.
        # This allows us to gracefully handle stream termination and propagate errors,
        # even during the middle of a read operation.
        async with trio_util.move_on_when(
            lambda: trio_util.wait_any(
                lambda: self.is_killed.wait_value(True),
                lambda: self._stream_error.wait_value(lambda v: v is not None),
            )
        ):
            yield

        if self.is_killed.value:
            raise MediaStreamKilledException

        if self._stream_error.value:
            raise self._stream_error.value from None

    @asynccontextmanager
    async def read_lifecycle(
        self, chunk_range: ChunkRange
    ) -> AsyncGenerator[ReadType, None]:
        """Context manager for managing read lifecycle."""

        reservations: list[_DeliveryEntry] = []
        try:
            read_type = await self._detect_read_type(
                chunk_range=chunk_range,
            )

            start_byte, end_byte = chunk_range.request_range
            foreground = (
                start_byte >= self.config.header_size
                and end_byte < self.chunker.footer_start
                and (
                    read_type == "body_read"
                    or (
                        read_type == "cache_hit"
                        and (
                            self._is_sequential_cache_playback(chunk_range)
                            or chunk_range.size > self.config.block_size * 2
                        )
                    )
                )
            )
            if foreground:
                manager = getattr(self, "adaptive_prefetch", None)
                if manager is not None:
                    generation = manager.current_generation
                    manager.record_read(start_byte, end_byte, monotonic())
                    self._consumer_end = end_byte
                    self._update_runway_observation(end_byte + 1)
                    if manager.current_generation != generation:
                        connection = self._active_stream_connection
                        if connection is not None:
                            connection.seek(chunk_range=chunk_range)
                    self._protect_consumer(start_byte)
                else:
                    self._consumer_end = end_byte

            # Start the stream and wait for a connection before progressing with an uncached body read.
            # This MUST be done before assigning a value to current_read,
            # or else the stream will not receive the value.
            start_pos: int | None = None
            if read_type == "body_read":
                if hasattr(self, "adaptive_prefetch"):
                    self.adaptive_prefetch.record_starvation()
                for chunk in chunk_range.chunks:
                    reservations.append(
                        await self._delivery_registry.register_pending(chunk.start)
                    )
                start_pos = chunk_range.position
            elif (
                read_type == "cache_hit"
                and foreground
                and (
                    getattr(self, "adaptive_prefetch", None) is None
                    or self.adaptive_prefetch.should_prefetch_now()
                    or self.adaptive_prefetch.get_effective_bitrate() <= 0
                )
            ):
                _, playhead_end = chunk_range.request_range
                cache_usage = 0.0
                cache_protected = 0.0
                try:
                    from .cache import Cache

                    if Cache in di:
                        cache_inst: Any = di[Cache]
                        if hasattr(cache_inst, "usage_percentage"):
                            u_val: Any = cache_inst.usage_percentage
                            raw_u: Any = u_val() if callable(u_val) else u_val
                            cache_usage = float(cast(float, raw_u))
                        if hasattr(cache_inst, "protected_usage_percentage"):
                            p_val: Any = cache_inst.protected_usage_percentage
                            raw_p: Any = p_val() if callable(p_val) else p_val
                            cache_protected = float(cast(float, raw_p))
                except Exception:
                    pass

                ahead_count = (
                    self.adaptive_prefetch.calculate_window(
                        cache_usage_pct=cache_usage,
                        cache_protected_pct=cache_protected,
                    )
                    if hasattr(self, "adaptive_prefetch")
                    else self.config.prefetch_chunks
                )
                ahead = self.chunker.get_prefetch_uncached(
                    after_end=playhead_end,
                    count=ahead_count,
                )
                if ahead:
                    start_pos = ahead[0].start

            if start_pos is not None and not self.is_streaming.value:
                async with self._start_lock:
                    if not self.is_streaming.value:
                        with trio.fail_after(self.config.connect_timeout_seconds):
                            await self.nursery.start(self.run, start_pos)

            self.recent_reads.current_read.value = Read(
                chunk_range=chunk_range,
                read_type=read_type,
            )

            yield read_type
        finally:
            with trio.CancelScope(shield=True):
                for entry in reservations:
                    await self._delivery_registry.release_reservation(entry)
            self.recent_reads.previous_read.value = self.recent_reads.current_read.value

    def _is_sequential_cache_playback(self, chunk_range: ChunkRange) -> bool:
        """Distinguish sustained playback from isolated cached metadata probes."""
        if self.config.prefetch_chunks <= 0:
            return False

        start, end = chunk_range.request_range
        if start < self.config.header_size or start >= self.chunker.footer_start:
            return False

        previous_end = getattr(self, "_consumer_end", None)
        if previous_end is None:
            previous = self.recent_reads.previous_read.value
            if previous is None or previous.read_type not in _HOT_STREAM_READ_TYPES:
                return False
            previous_start, previous_end = previous.chunk_range.request_range
            if (
                previous_start < self.config.header_size
                or previous_end >= self.chunker.footer_start
            ):
                return False
        return (
            previous_end < end
            and abs(start - previous_end - 1) <= self.config.sequential_read_tolerance
        )

    def _protect_consumer(self, request_start: int) -> None:
        """Metadata probes must not move the foreground lease window."""
        # Protect active playhead lookback and lookahead chunks in VFS cache
        try:
            from .cache import Cache

            if Cache in di and hasattr(self, "stream_id"):
                max_prefetch_chunks = (
                    self.adaptive_prefetch.config.max_window_chunks
                    if hasattr(self, "adaptive_prefetch")
                    else self.config.prefetch_chunks
                )
                dynamic_lookahead = max(
                    (max_prefetch_chunks + 2) * self.chunker.chunk_size,
                    384 * 1024 * 1024,
                )
                di[Cache].reconcile_stream_playhead(
                    stream_id=self.stream_id,
                    cache_key=self.file_metadata.original_filename,
                    playhead_byte=request_start,
                    lookahead_bytes=dynamic_lookahead,
                    header_bytes=self.config.header_size,
                )
        except Exception:
            pass

    async def read(
        self,
        *,
        request_start: int,
        request_end: int,
        request_size: int,
    ) -> bytes:
        """Handles incoming read requests from the VFS."""
        read_range = self.chunker.get_chunk_range(
            position=request_start,
            size=request_size,
        )

        async with self.capture_stream_errors():
            async with self.read_lifecycle(chunk_range=read_range) as read_type:
                self._trace_stream(
                    f"Performing {read_type} for [{request_start}-{request_end}]",
                    hot=read_type in _HOT_STREAM_READ_TYPES,
                )

                match read_type:
                    case "cache_hit":
                        data = await self._read_cached_or_fallback(
                            start=request_start,
                            end=request_end,
                        )
                        try:
                            from program.services.streaming.telemetry import (
                                playback_telemetry_collector,
                            )

                            playback_telemetry_collector.record_stream_read(
                                stream_id=f"{self.file_metadata.path}:{self.fh}",
                                nbytes=len(data),
                                from_cache=True,
                            )
                        except Exception:
                            pass
                        return data
                    case "header_scan":
                        return await self.scan_header(
                            read_position=request_start,
                            size=request_size,
                        )
                    case "footer_scan" | "footer_read":
                        # Note: if the read type is footer_read, the footer cache chunk
                        # has likely expired and the player is nearing EOF.
                        # In this case, we will re-download the entire footer and serve the rest from cache.
                        #
                        # This can happen if the user's cache size is small,
                        # or during heavy scans with lots of competing streams.
                        return await self.scan_footer(
                            read_position=request_start,
                            size=request_size,
                        )
                    case "general_scan":
                        return await self.scan(
                            read_position=request_start,
                            size=request_size,
                        )
                    case "body_read":
                        self.session_statistics.body_read_count += 1
                        self.session_statistics.last_body_read_timestamp = monotonic()
                        data = await self.read_bytes(
                            chunk_range=read_range, delivery_reserved=True
                        )
                        try:
                            from program.services.streaming.telemetry import (
                                playback_telemetry_collector,
                            )

                            playback_telemetry_collector.record_stream_read(
                                stream_id=f"{self.file_metadata.path}:{self.fh}",
                                nbytes=len(data),
                                from_cache=False,
                            )
                        except Exception:
                            pass
                        return data
                    case _:
                        # This should never happen due to prior validation
                        raise RuntimeError("Unknown read type")

    async def read_bytes(
        self,
        chunk_range: ChunkRange,
        *,
        delivery_reserved: bool = False,
    ) -> bytes:
        """Assemble exact per-chunk slices while retaining delivery reservations."""
        start, end = chunk_range.request_range
        reservations: list[_DeliveryEntry] = []
        try:
            if not delivery_reserved:
                for chunk in chunk_range.chunks:
                    reservations.append(
                        await self._delivery_registry.register_pending(chunk.start)
                    )
            await self._wait_until_chunks_ready(chunk_range=chunk_range)
            parts: list[bytes] = []
            for chunk in chunk_range.chunks:
                slice_start = max(start, chunk.start)
                slice_end = min(end, chunk.end)
                if slice_start <= slice_end:
                    parts.append(
                        await self._read_cached_or_fallback(
                            start=slice_start,
                            end=slice_end,
                            chunk_start=chunk.start,
                        )
                    )
            return b"".join(parts)
        finally:
            with trio.CancelScope(shield=True):
                for entry in reservations:
                    await self._delivery_registry.release_reservation(entry)

    async def _read_cached_or_fallback(
        self,
        *,
        start: int,
        end: int,
        chunk_start: int | None = None,
    ) -> bytes:
        """Never expose a transient cache miss as a zero-byte VFS read."""
        cached_data = await self._read_cache(start=start, end=end)

        if len(cached_data) == end - start + 1:
            self._trace_stream(
                f"Found data {start}-{end} ({len(cached_data)} bytes) from cache",
                hot=True,
            )

            return cached_data

        # Check transient delivery registry before issuing a second HTTP request.
        # The producer may have published bytes here when persistent cache admission
        # was refused (REFUSED_PHYSICAL_PRESSURE). This preserves the one-provider-fetch
        # invariant: we never re-fetch bytes that were already downloaded.
        lookup_start = chunk_start if chunk_start is not None else start
        entry = self._delivery_registry.get_entry(lookup_start)
        if entry is None and lookup_start != start:
            entry = self._delivery_registry.get_entry(start)

        if entry is not None:
            if entry.state == _DeliveryState.PENDING:
                # Wait for the producer to publish or retire
                await entry.ready.wait()
            lookup_key = entry.start
            transient_payload = (
                entry.payload if entry.state == _DeliveryState.READY_TRANSIENT else None
            )
            if (
                transient_payload is not None
                and start - lookup_key >= 0
                and end - lookup_key < len(transient_payload)
            ):
                # Slice the exact requested byte range from the chunk payload.
                # start is the requested start offset; lookup_key is the chunk start offset.
                offset_in_payload = max(0, start - lookup_key)
                needed_len = end - start + 1
                result_bytes = transient_payload[
                    offset_in_payload : offset_in_payload + needed_len
                ]
                self._trace_stream(
                    f"Delivered {start}-{end} ({len(result_bytes)} bytes) from transient registry",
                    hot=True,
                )
                return result_bytes

        # Fallback: if cache read returns empty (e.g. transient cache write delay or eviction),
        # fetch the requested range directly from the provider HTTP endpoint rather than failing the VFS read.
        logger.warning(
            self.build_log_message(
                f"Cache miss for {start}-{end} after chunk ready; fetching fallback from HTTP"
            )
        )
        return await self._fetch_discrete_byte_range(
            start=start,
            size=end - start + 1,
            # This is an emergency correctness path after a chunk signalled
            # ready. Caching its small VFS slice creates overlapping entries
            # that can shadow the complete media chunk during resume.
            should_cache=False,
        )

    @asynccontextmanager
    async def establish_connection(
        self,
        start: int,
        *,
        end: int | None = None,
    ) -> AsyncGenerator[httpx.Response]:
        """Establish a streaming connection starting at the given byte offset."""

        if settings_manager.settings.enable_network_tracing:

            async def trace_log(event_name: str, info: Any):
                safe_info = redact_text(str(info))
                logger.log(
                    "NETWORK",
                    self.build_log_message(f"{event_name} - {safe_info}"),
                )

            extensions = {"trace": trace_log}
        else:
            extensions = None

        headers = httpx.Headers(
            {
                "Accept-Encoding": "identity",
                "Connection": "keep-alive",
                "Range": format_range_header(start=start, end=end),
            }
        )

        MAX_DURABLE_REFRESH_ATTEMPTS = 2
        durable_refresh_attempts = 0
        max_transport_attempts = 4
        transport_attempt = 0
        backoffs = [0.2, 0.5, 1.0]
        request_kind = "scan" if end is not None else "body"
        generation = getattr(
            getattr(self, "adaptive_prefetch", None), "current_generation", 0
        )

        def _resolve_stream_workload() -> Workload:
            try:
                from program.services.streaming.telemetry import (
                    playback_telemetry_collector,
                )

                if playback_telemetry_collector.is_media_foreground(
                    self.file_metadata.path
                ):
                    return "foreground"
            except Exception:
                pass
            return "neutral"

        while transport_attempt < max_transport_attempts:
            lease: GenerationLease | None = None
            failed_generation: int | None = None
            yielded = False
            request_url = self.target_url.value
            if request_kind == "body":
                inflight = getattr(self, "_inflight_chunk", None)
                chunk_start = inflight[1] if inflight else start
                remaining_attempts = max_transport_attempts - transport_attempt
                stream_cfg = getattr(self, "config", None)
                connect_timeout = float(
                    getattr(stream_cfg, "connect_timeout_seconds", 5.0)
                )
                remaining_backoffs = sum(
                    backoffs[min(i, len(backoffs) - 1)]
                    for i in range(transport_attempt, max_transport_attempts - 1)
                )
                url_refresh_budget = 10.0
                estimated_bound = (
                    remaining_attempts * connect_timeout
                    + remaining_backoffs
                    + url_refresh_budget
                )
                self._set_recovery_state(
                    StreamRecoveryPhase.TRANSPORT_RETRY,
                    generation=generation,
                    chunk_start=chunk_start,
                    failed_url=request_url,
                    attempt=transport_attempt + 1,
                    timeout_bound=estimated_bound,
                )
            try:
                if not self._transport_generation_live(generation):
                    raise RecoverableMediaStreamException(
                        DebridServiceClosedConnectionException(provider=self.provider)
                    )
                if self._http_pool is not None:
                    failed_generation = self._http_pool.generation
                    try:
                        admit_ctx = self._http_pool.admit(
                            request_kind, workload=_resolve_stream_workload
                        )
                    except TypeError as exc:
                        # Preserve compatibility with test doubles and older injected
                        # pool implementations that predate workload-aware admission.
                        if "workload" not in str(exc):
                            raise
                        admit_ctx = self._http_pool.admit(request_kind)
                else:
                    admit_ctx = admit_stream_request(request_kind)

                async with admit_ctx:
                    if self._http_pool is not None:
                        lease = self._http_pool.acquire_lease(
                            use_proxy=self._use_proxy_client
                        )
                        failed_generation = lease.generation
                        client = lease.client
                    else:
                        client = self._resolve_async_client()

                    request_url = self.target_url.value
                    async with client.stream(
                        method="GET",
                        url=request_url,
                        headers=headers,
                        extensions=extensions,
                    ) as stream:
                        stream.raise_for_status()

                        try:
                            validate_range_response(
                                response=stream,
                                requested_start=start,
                                requested_end=end,
                                provider=self.provider,
                                file_size=self.file_metadata.file_size,
                            )
                        except DebridServiceRefusedRangeRequestException as range_exc:
                            logger.warning(
                                self.build_log_message(
                                    f"Range integrity check failed on attempt {transport_attempt + 1}/{max_transport_attempts}: {range_exc}"
                                )
                            )
                            # Cap range refusal retries to 1 with URL refresh to prevent retry storms
                            if transport_attempt == 0:
                                failed_url = self.target_url.value
                                has_fresh_url = await self._refresh_download_url(
                                    failed_url=failed_url
                                )
                                if has_fresh_url:
                                    logger.warning(
                                        self.build_log_message(
                                            "URL refresh succeeded after range refusal; retrying with fresh URL"
                                        )
                                    )
                                    transport_attempt += 1
                                    continue

                            raise

                        self.session_statistics.total_session_connections += 1

                        yielded = True
                        yield stream

                        return
            except httpx.HTTPStatusError as e:
                if yielded:
                    raise
                status_code = e.response.status_code

                logger.warning(self.build_log_message(f"HTTP error {status_code}: {e}"))

                if status_code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
                    failed_url = self.target_url.value
                    logger.warning(
                        self.build_log_message(
                            f"HTTP {status_code} ({'Unauthorized' if status_code == HTTPStatus.UNAUTHORIZED else 'Forbidden'}) - "
                            f"durable refresh attempt {durable_refresh_attempts + 1}/{MAX_DURABLE_REFRESH_ATTEMPTS}"
                        )
                    )

                    if durable_refresh_attempts < MAX_DURABLE_REFRESH_ATTEMPTS:
                        durable_refresh_attempts += 1
                        has_fresh_url = await self._refresh_download_url(
                            failed_url=failed_url
                        )

                        if has_fresh_url:
                            logger.warning(
                                self.build_log_message(
                                    f"Durable URL refresh succeeded after HTTP {status_code}; reconnecting with new URL"
                                )
                            )
                            continue

                    raise DebridServiceForbiddenException(provider=self.provider) from e
                elif status_code in (
                    HTTPStatus.NOT_FOUND,
                    HTTPStatus.GONE,
                    HTTPStatus.SERVICE_UNAVAILABLE,
                ):
                    # File can't be found at this URL; try refreshing the URL once
                    if transport_attempt == 0:
                        failed_url = self.target_url.value
                        has_fresh_url = await self._refresh_download_url(
                            failed_url=failed_url
                        )

                        if has_fresh_url:
                            logger.warning(
                                self.build_log_message(
                                    f"URL refresh after HTTP {status_code}"
                                )
                            )

                            if await self._retry_with_backoff(
                                transport_attempt,
                                max_transport_attempts,
                                backoffs,
                            ):
                                transport_attempt += 1
                                continue

                    raise DebridServiceUnableToConnectException(
                        provider=self.provider
                    ) from e
                elif status_code == HTTPStatus.RANGE_NOT_SATISFIABLE:
                    # Requested range not satisfiable; handled as EOF
                    raise DebridServiceRangeNotSatisfiableException(
                        provider=self.provider
                    ) from e
                elif status_code == HTTPStatus.TOO_MANY_REQUESTS:
                    # Rate limited - back off exponentially, don't refresh URL
                    logger.warning(
                        self.build_log_message(
                            f"HTTP 429 Rate Limited - attempt {transport_attempt + 1}"
                        )
                    )

                    if await self._retry_with_backoff(
                        transport_attempt,
                        max_transport_attempts,
                        backoffs,
                    ):
                        transport_attempt += 1
                        continue

                    raise DebridServiceRateLimitedException(
                        provider=self.provider
                    ) from e
                else:
                    # Other unexpected status codes
                    logger.warning(
                        self.build_log_message(f"Unexpected HTTP {status_code}")
                    )

                    raise DebridServiceException(
                        "Unexpected error connecting to stream",
                        provider=self.provider,
                    ) from e
            except (
                httpx.ConnectError,
                httpx.InvalidURL,
            ) as e:
                if yielded:
                    raise
                # Preserve the legacy initial-connect/scan ConnectError recovery,
                # but never treat a malformed URL as a refreshable transport fault.
                if (
                    isinstance(e, httpx.ConnectError)
                    and transport_attempt == 0
                    and (
                        end is not None
                        or self.session_statistics.bytes_transferred == 0
                    )
                ):
                    await self._refresh_download_url(failed_url=request_url)
                logger.warning(
                    self.build_log_message(
                        f"Encountered {e.__class__.__name__}: {redact_text(str(e))} "
                        f"(attempt {transport_attempt + 1}/{max_transport_attempts})"
                    )
                )

                if await self._retry_with_backoff(
                    transport_attempt,
                    max_transport_attempts,
                    backoffs,
                ):
                    transport_attempt += 1
                    continue

                if (
                    end is None
                    and isinstance(e, httpx.ConnectError)
                    and await self._recover_transport_url(request_url, generation)
                ):
                    transport_attempt = 0
                    continue

                raise DebridServiceUnableToConnectException(
                    provider=self.provider
                ) from e
            except httpx.PoolTimeout as e:
                if yielded:
                    raise
                # Pool saturation: shed + recycle once, then fail-fast (no backoff storm).
                pool_repr = ""
                try:
                    active_client = (
                        lease.client
                        if lease is not None
                        else self._resolve_async_client()
                    )
                    pool_repr = str(active_client._transport._pool)  # type: ignore[attr-defined]
                except Exception:
                    pool_repr = ""

                logger.warning(
                    self.build_log_message(
                        f"PoolTimeout error (attempt {transport_attempt + 1}/{max_transport_attempts}): "
                        f"{redact_text(str(e))}"
                    ),
                )

                if pool_repr:
                    logger.debug(
                        self.build_log_message(
                            f"All connections are in use: {pool_repr}"
                        )
                    )

                if transport_attempt == 0:
                    if self._http_pool is not None:
                        await self._http_pool.heal_on_pool_timeout(
                            failed_generation=failed_generation,
                            pool_repr=pool_repr,
                        )
                        # A concurrent caller may have completed the shared
                        # recovery while this request still held a lease for
                        # the retired generation. Release it before retrying
                        # so that generation can drain cleanly.
                        if lease is not None:
                            await self._http_pool.release_lease(lease)
                            lease = None
                    else:
                        await heal_on_pool_timeout(pool_repr=pool_repr)
                    transport_attempt += 1
                    continue

                raise DebridServiceClosedConnectionException(
                    provider=self.provider
                ) from e
            except (httpx.RemoteProtocolError, httpx.TimeoutException) as e:
                if yielded:
                    raise
                # This can happen if the server closes the connection prematurely
                logger.warning(
                    self.build_log_message(
                        f"{e.__class__.__name__} error (attempt {transport_attempt + 1}/{max_transport_attempts}): {e}"
                    ),
                )

                if await self._retry_with_backoff(
                    transport_attempt,
                    max_transport_attempts,
                    backoffs,
                ):
                    transport_attempt += 1
                    continue

                if end is None and await self._recover_transport_url(
                    request_url, generation
                ):
                    transport_attempt = 0
                    continue

                raise DebridServiceClosedConnectionException(
                    provider=self.provider
                ) from e
            except (
                DebridServiceLinkUnavailable,
                DebridServiceFairUsageLimitException,
                DebridServiceRefusedRangeRequestException,
                RecoverableMediaStreamException,
            ):
                raise
            except Exception as e:
                if yielded:
                    raise
                logger.exception(
                    self.build_log_message("Unexpected error connecting to stream")
                )

                raise DebridServiceException(
                    "Unexpected error connecting to stream",
                    provider=self.provider,
                ) from e
            finally:
                if request_kind == "body":
                    self._set_recovery_state(
                        StreamRecoveryPhase.IDLE,
                        generation=generation,
                        chunk_start=start,
                    )
                if lease is not None and self._http_pool is not None:
                    await self._http_pool.release_lease(lease)

        raise DebridServiceException(
            "Unexpected error connecting to stream",
            provider=self.provider,
        )

    async def _detect_read_type(
        self,
        *,
        chunk_range: ChunkRange,
    ) -> ReadType:
        start, end = chunk_range.request_range
        size = chunk_range.size

        # First, attempt to detect if the requested range is already cached.
        # This uses a lightweight check, that just checks for existence,
        # rather than reading the actual data.
        is_request_fully_cached = self._check_cache(
            start=chunk_range.first_chunk.start,  # Align to start of chunk for cache check
            end=end,
        )

        if is_request_fully_cached:
            return "cache_hit"

        # A header scan can only satisfy ranges wholly contained in the
        # pre-fetched header chunk. Chunk ranges are inclusive, so the last
        # valid header offset is header_chunk.end (header_size - 1).
        if (
            start <= self.chunker.header_chunk.end
            and end <= self.chunker.header_chunk.end
        ):
            return "header_scan"

        file_size = self.file_metadata.file_size

        if (
            (self.recent_reads.last_read_end or 0)
            < start - self.config.sequential_read_tolerance
        ) and file_size - self.footer_size <= start <= file_size:
            return "footer_scan"

        if (
            # This behaviour is seen during scanning
            # and captures large jumps in read position
            # generally observed when the player is reading the footer
            # for cues or metadata after initial playback start.
            #
            # Scans typically read a single block (128 KB) or two blocks.
            self.recent_reads.last_read_end is not None
            and (
                abs(self.recent_reads.last_read_end - start)
                > self.config.scan_tolerance
            )
            and start != self.config.header_size
            and size <= self.config.block_size * 2
        ) or (
            # This behaviour is seen when seeking.
            # Playback has already begun, so the header has been served
            # for this file, but the scan happens on a new file handle
            # and is the first request to be made.
            start > self.config.header_size
            and self.recent_reads.last_read_end is None
        ):
            return "general_scan"

        if start < self.file_metadata.file_size - self.footer_size:
            return "body_read"

        return "footer_read"

    async def _fetch_discrete_byte_range(
        self,
        start: int,
        size: int,
        should_cache: bool = True,
    ) -> bytes:
        """
        Fetch a discrete range of data outside of the main stream.

        Used for fetching the header, footer, and one-off scans.
        """

        if start < 0:
            raise ValueError("Start must be non-negative")

        if size <= 0:
            raise ValueError("Size must be positive")

        for integrity_attempt in range(1, DISCRETE_SCAN_MAX_INTEGRITY_ATTEMPTS + 1):
            async with self.establish_connection(
                start=start,
                end=start + size - 1,
            ) as response:
                data = await response.aread()

                self.session_statistics.bytes_transferred += len(data)

                if len(data) < size:
                    logger.warning(
                        self.build_log_message(
                            "Short scan read detected; "
                            f"attempt={integrity_attempt}/{DISCRETE_SCAN_MAX_INTEGRITY_ATTEMPTS} "
                            f"expected={size} actual={len(data)} "
                            f"status={response.status_code} "
                            f"content-length={response.headers.get('Content-Length')} "
                            f"content-range={response.headers.get('Content-Range')}"
                        )
                    )

                    if integrity_attempt < DISCRETE_SCAN_MAX_INTEGRITY_ATTEMPTS:
                        has_fresh_url = await self._refresh_download_url()

                        if has_fresh_url:
                            logger.warning(
                                self.build_log_message(
                                    "Refreshed URL after short scan read"
                                )
                            )

                        await trio.sleep(
                            DISCRETE_SCAN_RETRY_BACKOFF_SECONDS[
                                min(
                                    integrity_attempt - 1,
                                    len(DISCRETE_SCAN_RETRY_BACKOFF_SECONDS) - 1,
                                )
                            ]
                        )

                        continue

                verified_data = self._verify_scan_integrity((start, start + size), data)

                if should_cache:
                    await self._cache_chunk(
                        start=start,
                        data=verified_data[:size],
                    )

                return verified_data

        raise RuntimeError(
            "Failed to fetch discrete byte range after integrity retries"
        )

    async def _wait_one_chunk(self, chunk: Chunk) -> None:
        entry = self._delivery_registry.get_entry(chunk.start)
        if entry is None:
            await chunk.is_cached.wait_value(True)
            return
        await trio_util.wait_any(
            lambda: chunk.is_cached.wait_value(True), entry.ready.wait
        )

    async def _wait_all_chunks(self, chunk_range: ChunkRange) -> None:
        async with trio.open_nursery() as nursery:
            for chunk in chunk_range.chunks:
                nursery.start_soon(self._wait_one_chunk, chunk)

    async def _wait_recovery_fence(self, generation: int) -> None:
        # IDLE means the replacement connected, not that its bytes are ready.
        # Keep the original bounded deadline through that handoff.
        while self._transport_generation_live(generation):
            await self._recovery_event.wait()

    def _recovery_grace_budget(self, chunk_range: ChunkRange, generation: int) -> float:
        state = self._recovery_state
        if (
            not self._transport_generation_live(generation)
            or not self._is_foreground_eligible()
            or state.phase == StreamRecoveryPhase.IDLE
            or state.generation != generation
        ):
            return 0.0
        if state.chunk_start is not None and not any(
            chunk.start == state.chunk_start for chunk in chunk_range.uncached_chunks
        ):
            return 0.0

        if state.recovery_deadline is not None:
            try:
                now = trio.current_time()
            except RuntimeError:
                now = monotonic()
            remaining = state.recovery_deadline - now
            return max(0.0, remaining)

        return max(0.0, state.timeout_bound)

    async def _wait_chunk_stage(
        self, chunk_range: ChunkRange, deadline: float, generation: int
    ) -> bool:
        ready = False

        async def wait_ready() -> None:
            nonlocal ready
            await self._wait_all_chunks(chunk_range)
            ready = True

        with trio.move_on_at(deadline):
            await trio_util.wait_any(
                wait_ready,
                lambda: self._stream_error.wait_value(lambda value: value is not None),
                lambda: self.is_killed.wait_value(True),
                lambda: self._wait_recovery_fence(generation),
            )
        error = self._stream_error.value
        if error is not None:
            raise error from None
        return ready

    async def _wait_until_chunks_ready(
        self,
        *,
        chunk_range: ChunkRange,
    ) -> None:
        """Wait for delivery with one bounded, foreground-only recovery grace."""
        started = monotonic()
        generation = self.adaptive_prefetch.current_generation
        nominal_timeout = self.config.chunk_wait_timeout_seconds
        if await self._wait_chunk_stage(
            chunk_range, trio.current_time() + nominal_timeout, generation
        ):
            return
        if not chunk_range.uncached_chunks:
            return
        grace_budget = self._recovery_grace_budget(chunk_range, generation)
        if grace_budget:
            self._trace_stream(
                f"Entering active recovery grace wait ({grace_budget:.1f}s) "
                f"for phase={self._recovery_state.phase.value}"
            )
            if await self._wait_chunk_stage(
                chunk_range, trio.current_time() + grace_budget, generation
            ):
                return
        if chunk_range.uncached_chunks:
            raise ChunksTooSlowException(
                threshold=ceil(monotonic() - started),
                chunks=chunk_range.uncached_chunks,
            )

    def _check_cache(self, *, start: int, end: int) -> bool:
        """Check if the given byte range is fully cached."""

        from .cache import Cache

        return di[Cache].has(
            cache_key=self.file_metadata.original_filename,
            start=start,
            end=end,
        )

    async def _read_cache(
        self,
        *,
        start: int,
        end: int,
    ) -> bytes:
        """Fetch the given byte range from the cache, if it exists."""

        from .cache import Cache

        return await di[Cache].get(
            cache_key=self.file_metadata.original_filename,
            start=start,
            end=end,
            stream_id=getattr(self, "stream_id", None),
        )

    async def _cache_chunk(
        self,
        *,
        start: int,
        data: bytes,
        admission: Literal["demand", "prefetch"] = "demand",
    ) -> Any:
        """Cache the given chunk of data. Returns a CachePutResult enum value."""

        from .cache import Cache, CachePutResult

        result: CachePutResult = await di[Cache].put(
            cache_key=self.file_metadata.original_filename,
            start=start,
            data=data,
            stream_id=getattr(self, "stream_id", None),
            admission=admission,
        )

        if (
            result == CachePutResult.SKIPPED_PREFETCH_PRESSURE
            and self._delivery_registry.has_pending(start)
        ):
            # A foreground reader can arrive while speculative admission awaits
            # the cache shard. Deliver the same bytes; never issue another fetch.
            await self._delivery_registry.publish(start=start, payload=data)

        if result == CachePutResult.REFUSED_PHYSICAL_PRESSURE:
            # Persistent cache cannot admit this chunk. Publish bytes directly
            # into the delivery registry so waiting readers get the data without
            # issuing a second HTTP request (one-provider-fetch invariant).
            await self._delivery_registry.publish(start=start, payload=data)
            logger.debug(
                self.build_log_message(
                    f"Cache admission refused for start={start} ({len(data)} bytes); "
                    "published to transient delivery registry"
                )
            )

        return result

    def _transport_generation_live(self, generation: int) -> bool:
        return not getattr(
            getattr(self, "is_killed", None), "value", False
        ) and generation == getattr(
            getattr(self, "adaptive_prefetch", None), "current_generation", 0
        )

    def _set_recovery_state(
        self,
        phase: StreamRecoveryPhase,
        *,
        generation: int = 0,
        chunk_start: int | None = None,
        failed_url: str | None = None,
        attempt: int = 0,
        timeout_bound: float = 0.0,
        recovery_deadline: float | None = None,
    ) -> None:
        """Update active recovery state and notify waiting readers."""
        try:
            now = trio.current_time()
        except RuntimeError:
            now = monotonic()

        episode = getattr(self, "_recovery_episode", 0)
        previous_state = getattr(self, "_recovery_state", None)
        if phase != StreamRecoveryPhase.IDLE:
            if (
                previous_state is None
                or previous_state.phase == StreamRecoveryPhase.IDLE
            ):
                episode += 1
                self._recovery_episode = episode
        else:
            episode = 0
            self._recovery_episode = 0

        computed_deadline: float | None = None
        if phase != StreamRecoveryPhase.IDLE:
            if recovery_deadline is not None:
                computed_deadline = recovery_deadline
            elif timeout_bound > 0.0:
                computed_deadline = now + timeout_bound

        self._recovery_state = StreamRecoveryState(
            phase=phase,
            generation=generation,
            chunk_start=chunk_start,
            failed_url=failed_url,
            attempt=attempt,
            start_time=now,
            timeout_bound=timeout_bound,
            recovery_deadline=computed_deadline,
            recovery_episode=episode,
        )
        old_event = getattr(self, "_recovery_event", None)
        self._recovery_event = trio.Event()
        if old_event is not None:
            old_event.set()

    def _is_foreground_eligible(self) -> bool:
        """
        Check if stream is eligible for durable transport recovery.
        Qualifies if either Plex telemetry marks it foreground OR the stream
        has established sequential body consumption (body reads occurred,
        bytes transferred > 0, consumer runway active, not an unestablished probe).
        """
        try:
            from program.services.streaming.telemetry import (
                playback_telemetry_collector,
            )

            if playback_telemetry_collector.is_media_foreground(
                self.file_metadata.path
            ):
                return True
        except Exception:
            pass

        # FUSE-only / non-Plex sequential body playback fallback:
        # Requires positive bytes transferred, active body reads or active consumer runway,
        # distinguishing established media playback from discrete header/footer/metadata probes.
        bytes_transferred = getattr(
            getattr(self, "session_statistics", None), "bytes_transferred", 0
        )
        body_read_count = getattr(
            getattr(self, "session_statistics", None), "body_read_count", 0
        )
        consumer_end = getattr(self, "_consumer_end", None)
        return bytes_transferred > 0 and (
            body_read_count > 0 or consumer_end is not None
        )

    async def _recover_transport_url(self, failed_url: str, generation: int) -> bool:
        """
        Durable transport recovery per established foreground handle.
        Enforces a per-failed-URL budget: at most 1 recovery attempt for the same URL.
        Re-arms if new URL successfully made media transfer progression (> 0 new bytes).
        """
        last_recovered = getattr(self, "_last_recovered_url", None)
        bytes_at_recovery = getattr(self, "_bytes_transferred_at_last_recovery", 0)
        current_bytes = getattr(
            getattr(self, "session_statistics", None), "bytes_transferred", 0
        )

        # Disallow recovery if:
        # 1. This exact failed_url was already attempted for recovery, OR
        # 2. We already recovered once, and no new bytes were transferred since that recovery (prevent storming on dead replacement), OR
        # 3. Generation is stale / stream killed, OR
        # 4. Stream has transferred 0 bytes (initial connection failure), OR
        # 5. Stream is not foreground eligible (neither Plex foreground nor established sequential body playback).
        if (
            last_recovered == failed_url
            or (last_recovered is not None and current_bytes <= bytes_at_recovery)
            or not self._transport_generation_live(generation)
            or current_bytes <= 0
            or not self._is_foreground_eligible()
        ):
            return False

        # Set tracking before awaiting the per-file lock; outer run retries cannot reset it.
        self._last_recovered_url = failed_url
        self._bytes_transferred_at_last_recovery = current_bytes

        inflight = getattr(self, "_inflight_chunk", None)
        chunk_start = inflight[1] if inflight else None
        safe_url = redact_text(failed_url)
        logger.info("D83 transport recovery attempted for URL: {}", safe_url)
        self._set_recovery_state(
            StreamRecoveryPhase.URL_REFRESH,
            generation=generation,
            chunk_start=chunk_start,
            failed_url=failed_url,
            attempt=1,
            timeout_bound=20.0,
        )
        try:
            refreshed = await self._refresh_download_url(failed_url=failed_url)
            recovered = (
                self._transport_generation_live(generation)
                and refreshed
                and self.target_url.value != failed_url
            )
            logger.info(
                "D83 transport recovery {}", "ready" if recovered else "unavailable"
            )
            return recovered
        finally:
            self._set_recovery_state(
                StreamRecoveryPhase.IDLE,
                generation=generation,
                chunk_start=chunk_start,
            )

    async def _refresh_download_url(
        self,
        failed_url: str | None = None,
        deadline: float | None = None,
    ) -> bool:
        """
        Refresh download URL by unrestricting from provider.

        Coordinates concurrent refreshes across MediaStream instances using
        a scoped lock per original_filename and checks persisted state before
        calling the provider unrestrict service.

        Args:
            failed_url: The exact URL that failed, used for race comparison.
                If omitted, defaults to self.target_url.value.
            deadline: Optional monotonic timestamp deadline. If omitted, derived
                from self._recovery_state.recovery_deadline or default timeout bound.

        Returns:
            True if successfully refreshed with a distinct URL, False otherwise.
        """
        target_failed_url = (
            failed_url if failed_url is not None else self.target_url.value
        )
        refresh_lock = _get_stream_refresh_lock(self.file_metadata.original_filename)
        generation = getattr(
            getattr(self, "adaptive_prefetch", None), "current_generation", 0
        )

        # Compute effective monotonic deadline
        now_mono = monotonic()
        if deadline is not None:
            effective_deadline = deadline
        else:
            state = getattr(self, "_recovery_state", None)
            if state is not None and state.recovery_deadline is not None:
                effective_deadline = state.recovery_deadline
            else:
                effective_deadline = now_mono + 20.0

        if now_mono >= effective_deadline:
            return False

        async with refresh_lock:
            if (
                not self._transport_generation_live(generation)
                or monotonic() >= effective_deadline
            ):
                return False
            from program.services.filesystem.vfs import VFSDatabase

            vfs_db = di[VFSDatabase]

            def _get_entry_safe(force_resolve: bool) -> Any:
                import inspect

                fn = vfs_db.get_entry_by_original_filename
                target = getattr(fn, "side_effect", None) or fn
                supports_deadline = False
                try:
                    sig = inspect.signature(target)
                    supports_deadline = "operation_deadline" in sig.parameters or any(
                        p.kind == inspect.Parameter.VAR_KEYWORD
                        for p in sig.parameters.values()
                    )
                except Exception:
                    supports_deadline = False

                if supports_deadline:
                    return fn(
                        original_filename=self.file_metadata.original_filename,
                        force_resolve=force_resolve,
                        operation_deadline=effective_deadline,
                    )
                return fn(
                    original_filename=self.file_metadata.original_filename,
                    force_resolve=force_resolve,
                )

            # Step 1: Mandatory DB re-check before provider refresh (force_resolve=False)
            entry_info = await trio.to_thread.run_sync(
                lambda: _get_entry_safe(force_resolve=False)
            )

            if (
                not self._transport_generation_live(generation)
                or monotonic() >= effective_deadline
            ):
                return False
            if entry_info:
                persisted_url = entry_info.url
                if persisted_url and persisted_url != target_failed_url:
                    self._trace_stream(
                        f"Adopting already-refreshed URL from DB for {self.file_metadata.original_filename}"
                    )
                    self.target_url.value = persisted_url
                    return True

            # Step 2: Persisted state still references failed URL or is missing -> canonical provider refresh
            entry_info_refreshed = await trio.to_thread.run_sync(
                lambda: _get_entry_safe(force_resolve=True)
            )

            if (
                not self._transport_generation_live(generation)
                or monotonic() >= effective_deadline
            ):
                return False
            if entry_info_refreshed:
                fresh_url = entry_info_refreshed.url
                if fresh_url and fresh_url != target_failed_url:
                    self._trace_stream(
                        f"Refreshed URL from provider for {self.file_metadata.original_filename}"
                    )
                    self.target_url.value = fresh_url
                    try:
                        from program.services.streaming.telemetry import (
                            playback_telemetry_collector,
                        )

                        playback_telemetry_collector.record_cdn_refresh(
                            stream_id=f"{self.file_metadata.path}:{self.fh}",
                            title=self.file_metadata.path.split("/")[-1],
                            details={
                                "provider": self.provider,
                                "original_filename": self.file_metadata.original_filename,
                            },
                        )
                    except Exception:
                        pass
                    return True

            return False

    async def _retry_with_backoff(
        self,
        attempt: int,
        max_attempts: int,
        backoffs: list[float],
    ) -> bool:
        """
        Common retry logic

        Returns:
            True if should retry, False if max attempts reached
        """
        if attempt < max_attempts - 1:
            await trio.sleep(backoffs[min(attempt, len(backoffs) - 1)])

            return True

        return False

    def _verify_scan_integrity(
        self,
        range: tuple[int, int],
        data: bytes,
    ) -> bytes:
        """
        Verify the integrity of the data read from the stream for scanning purposes.

        Args:
            range: The byte range that was requested
            data: The data read from the stream
        """

        if data == b"":
            raise EmptyDataException(range=range)

        start, end = range
        expected_length = end - start
        actual_length = len(data)

        if actual_length != expected_length:
            raise ByteLengthMismatchException(
                expected_length=expected_length,
                actual_length=actual_length,
                range=range,
            )

        return data

    def build_log_message(self, message: str) -> str:
        fh_repr = getattr(self, "fh", "unknown")
        meta_path = getattr(getattr(self, "file_metadata", None), "path", "unknown")
        file_repr = (
            meta_path.split("/")[-1] if isinstance(meta_path, str) else "unknown"
        )
        return f"{message} [fh: {fh_repr} | file={file_repr}]"
