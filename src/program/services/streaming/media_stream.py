import inspect
import re
import threading
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from functools import cached_property, partial
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
from program.utils.stream_http import stream_http_timeout

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
from .session_statistics import SessionStatistics, observe_response
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
    # Monotonic timestamp when this entry was created; used to expire
    # proactively-published transient entries that no reader ever claimed.
    registered_at: float = field(default_factory=monotonic)


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

    # Maximum age (seconds) for a proactively-published READY_TRANSIENT entry
    # that was never claimed by any reader.  After this window the entry is
    # considered stale and will be swept out by _expire_stale_transients().
    _TRANSIENT_TTL_SECONDS: float = 10.0

    async def publish(self, start: int, payload: bytes) -> None:
        """Transition a PENDING entry to READY_TRANSIENT and wake waiters.

        When a PENDING entry exists (a reader registered via register_pending()
        is waiting), publish wakes it immediately.

        When no entry exists the payload is stored as a zero-waiter
        READY_TRANSIENT entry so that readers arriving shortly after the
        producer (e.g. the VFS read that follows a prefetch publish) can still
        find the bytes without issuing a second HTTP request.  The entry is
        self-expiring via _expire_stale_transients() so it cannot accumulate
        indefinitely in the absence of a consumer.

        Crucially, waiter_count is kept at 0 (not 1) for proactively-published
        entries.  Setting it to 1 without a matching release_reservation() /
        release_borrow() call caused the entry to live forever and poisoned
        has_pending() checks in the prefetch admission path, which in turn
        forced every subsequent prefetch to be reclassified as a "demand" fetch
        and broke the prefetch loop — producing the per-second streaming freeze.
        """
        async with self._lock:
            entry = self._entries.get(start)
            if entry is None:
                # No reader is waiting yet.  Publish a zero-waiter transient
                # entry so upcoming readers can grab the payload without a
                # second round-trip.  waiter_count stays 0 so that:
                #   • has_pending() correctly returns False (no one is waiting),
                #     preserving prefetch admission classification.
                #   • The entry is cleaned up by _expire_stale_transients()
                #     if no reader ever arrives within _TRANSIENT_TTL_SECONDS.
                entry = _DeliveryEntry(start=start, waiter_count=0)
                entry.payload = payload
                entry.state = _DeliveryState.READY_TRANSIENT
                entry.ready.set()
                self._entries[start] = entry
                return
            if entry.state != _DeliveryState.PENDING:
                # Already READY_TRANSIENT or RETIRED — nothing to do.
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
        """Sync check: is there a PENDING entry with at least one active waiter for this offset?

        READY_TRANSIENT entries that were proactively published (waiter_count==0)
        are intentionally excluded: they don't represent an in-flight demand
        fetch, so treating them as "pending" would corrupt prefetch admission
        classification (forcing prefetch chunks to be reclassified as demand
        fetches and stalling the prefetch loop).
        """
        entry = self._entries.get(start)
        if entry is None:
            return False
        if entry.state == _DeliveryState.PENDING and entry.waiter_count > 0:
            return True
        # A READY_TRANSIENT entry with actual waiters (reader registered via
        # register_pending before the payload arrived) also counts as pending
        # from the admission perspective.
        if entry.state == _DeliveryState.READY_TRANSIENT and entry.waiter_count > 0:
            return True
        return False

    async def expire_stale_transients(self) -> None:
        """Remove READY_TRANSIENT entries that exceeded the TTL with no active consumers.

        Called periodically from the prefetch loop to prevent zero-waiter
        transient entries (published proactively when no reader was waiting)
        from leaking into the registry indefinitely.
        """
        cutoff = monotonic() - self._TRANSIENT_TTL_SECONDS
        async with self._lock:
            stale = [
                start
                for start, entry in self._entries.items()
                if (
                    entry.state == _DeliveryState.READY_TRANSIENT
                    and entry.waiter_count == 0
                    and entry.borrower_count == 0
                    and entry.registered_at < cutoff
                )
            ]
            for start in stale:
                entry = self._entries.pop(start, None)
                if entry is not None:
                    # Ensure any late-arriving waiter on entry.ready is unblocked.
                    entry.state = _DeliveryState.RETIRED
                    entry.ready.set()

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


class _ProducerClaims:
    """Atomic producer ownership; never owns or changes reader reservations."""

    def __init__(self) -> None:
        self._owners: dict[int, trio.Event] = {}

    def acquire(self, start: int) -> tuple[bool, trio.Event]:
        # No checkpoint between lookup and insertion in the owning Trio loop.
        existing = self._owners.get(start)
        if existing is not None:
            return False, existing
        event = trio.Event()
        self._owners[start] = event
        return True, event

    def release(self, start: int, event: trio.Event) -> None:
        if self._owners.get(start) is event:
            self._owners.pop(start)
            event.set()

    def is_claimed(self, start: int) -> bool:
        """Return True if an active producer currently owns ``start``."""
        return start in self._owners

    def active_starts(self) -> set[int]:
        """Return snapshot of chunk start offsets owned by active producers."""
        return set(self._owners)

    def wake_all(self) -> None:
        for event in self._owners.values():
            event.set()
        self._owners.clear()


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


_STREAM_READ_CHUNK_SIZE = 262_144


def _iter_raw_chunks(
    response: Any, chunk_size: int = _STREAM_READ_CHUNK_SIZE
) -> AsyncIterator[bytes]:
    """Iterate response body with a larger buffer when response.aiter_raw supports chunk_size."""
    aiter_raw = response.aiter_raw
    try:
        params = inspect.signature(aiter_raw).parameters
        accepts_chunk_size = "chunk_size" in params or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
    except (TypeError, ValueError):
        accepts_chunk_size = False
    if accepts_chunk_size:
        return aiter_raw(chunk_size=chunk_size)
    return aiter_raw()


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
        self._last_protected_bucket: int | None = None
        self._last_protected_at: float | None = None
        self._inflight_chunk: tuple[int, int] | None = None
        self.parallel_prefetch_workers = stream_settings.parallel_prefetch_workers
        self._producer_claims = _ProducerClaims()
        self._speculative_active = 0
        self._speculative_peak = 0
        self._speculative_bytes = 0
        self._speculative_failures = 0
        self._speculative_scope: trio.CancelScope | None = None
        self._speculative_done: trio.Event | None = None
        self._speculative_inflight_chunks: set[tuple[int, int]] = set()
        self._batch_spec_starts: set[int] | None = None
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
            target_buffer_seconds=stream_settings.target_buffer_seconds,
            min_safe_runway_seconds=stream_settings.min_safe_runway_seconds,
        )

        self.session_statistics = SessionStatistics()
        self.session_statistics.parallel_prefetch_workers = (
            self.parallel_prefetch_workers
        )
        self.session_statistics.on_update = self._publish_telemetry_snapshot
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
        # Allow max_window_chunks to scale based on user-configured target_buffer_seconds
        # and prefetch_chunks so user settings have priority over the historical 48-chunk floor.
        _chunk_bytes = self.config.chunk_size
        _ref_bitrate_bps = (
            120_000_000  # 120 Mbps reference ceiling for 4K UHD Remux / DoVi
        )
        _target_chunks_for_buffer = (
            ceil(
                (_ref_bitrate_bps * self.config.target_buffer_seconds)
                / (8 * _chunk_bytes)
            )
            if _chunk_bytes > 0
            else 48
        )
        max_chunks = max(48, configured_prefetch, _target_chunks_for_buffer)

        effective_initial_bitrate = bitrate
        if effective_initial_bitrate is None and file_size >= 1024 * 1024 * 1024:
            # Derive conservative lower-bound baseline bitrate assuming 2-hour nominal duration (7200s).
            # Clamped between 4 Mbps (standard 1080p) and 120 Mbps (4K UHD Remux).
            derived = int((file_size * 8) / 7200)
            effective_initial_bitrate = max(4_000_000, min(120_000_000, derived))

        self._publish_telemetry_snapshot(self.session_statistics)

        # Enforce robust prefetch floor for smaller/lower-bitrate files (e.g. 16 chunks or configured_prefetch)
        _min_prefetch = min(16, configured_prefetch) if configured_prefetch > 0 else 4
        self.adaptive_prefetch = AdaptivePrefetchManager(
            config=AdaptivePrefetchConfig(
                min_window_chunks=_min_prefetch,
                chunk_size_bytes=self.config.chunk_size,
                default_fallback_chunks=configured_prefetch,
                max_window_chunks=max_chunks,
                sequential_tolerance_bytes=self.config.sequential_read_tolerance,
                target_buffer_seconds=self.config.target_buffer_seconds,
                min_safe_runway_seconds=self.config.min_safe_runway_seconds,
            ),
            initial_bitrate=effective_initial_bitrate,
        )

        self.file_metadata = FileMetadata(
            file_size=file_size,
            path=path,
            original_filename=original_filename,
            bitrate=bitrate,
        )

        self.stream_id = f"{self.file_metadata.path}:{self.fh}"

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
            f"parallel_prefetch_workers={self.parallel_prefetch_workers}, "
            f"chunk_wait_timeout_seconds={self.config.chunk_wait_timeout_seconds}, "
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

    def _invalidate_misaligned_connection(
        self,
        connection: Any,
        *,
        chunk_start: int,
        chunk_size: int,
        prefer_pending_demand: bool = False,
    ) -> None:
        """Force a reconnect after consuming bytes from a response whose read position did not advance."""
        self._inflight_chunk = None
        if getattr(getattr(self, "is_killed", None), "value", False) or not hasattr(
            connection, "seek_required"
        ):
            return
        if connection.seek_required.value:
            return
        seek_target_range = None
        get_chunk_range = getattr(
            getattr(self, "chunker", None), "get_chunk_range", None
        )
        if get_chunk_range is not None:
            if prefer_pending_demand and hasattr(self, "_delivery_registry"):
                pending_starts = self._uncached_foreground_starts()
                if pending_starts:
                    candidate = get_chunk_range(position=pending_starts[0], size=1)
                    if candidate.uncached_chunks:
                        seek_target_range = candidate
            if seek_target_range is None:
                fallback = get_chunk_range(position=chunk_start, size=chunk_size)
                if fallback.uncached_chunks:
                    seek_target_range = fallback
        if seek_target_range is not None:
            connection.seek(chunk_range=seek_target_range)
        if not connection.seek_required.value:
            connection.seek_range = None
            connection.seek_required.value = True

    def _resolve_async_client(self) -> httpx.AsyncClient:
        """Return the current streaming client (from injected pool or DI fallback)."""

        if self._http_pool is not None:
            return self._http_pool.get_client(use_proxy=self._use_proxy_client)

        if self._use_proxy_client:
            return di[ProxyClient]

        return di[AsyncClient]

    def _update_runway_observation(self, position: int) -> None:
        """Publish contiguous cached runway without eagerly materializing all window chunks."""
        manager = getattr(self, "adaptive_prefetch", None)
        if manager is None:
            return
        try:
            file_size = getattr(getattr(self, "file_metadata", None), "file_size", None)
            chunk_size = max(1, int(getattr(self.config, "chunk_size", 1)))
            max_chunks = max(1, int(getattr(manager.config, "max_window_chunks", 48)))
            horizon_end = position + chunk_size * max_chunks - 1
            if isinstance(file_size, int) and file_size > 0:
                horizon_end = min(horizon_end, file_size - 1)
            if position < 0 or position > horizon_end:
                manager.record_runway(cached_bytes=0, in_flight_bytes=0)
                return

            cached_bytes = 0
            in_flight_bytes = 0
            inflight = getattr(self, "_inflight_chunk", None)
            empty_spec: set[tuple[int, int]] = set()
            speculative: set[tuple[int, int]] = getattr(
                self, "_speculative_inflight_chunks", empty_spec
            )
            cur_gen = manager.current_generation
            cur_pos = position
            steps = 0
            while cur_pos <= horizon_end and steps <= max_chunks + 2:
                steps += 1
                single = self.chunker.get_chunk_range(position=cur_pos, size=1)
                chunks = getattr(single, "chunks", None)
                if not chunks:
                    break
                chunk = (
                    single.first_chunk if hasattr(single, "first_chunk") else chunks[0]
                )
                c_start = getattr(chunk, "start", None)
                c_end = getattr(chunk, "end", None)
                if not isinstance(c_start, int) or not isinstance(c_end, int):
                    break
                if c_end < cur_pos or c_start > horizon_end:
                    break
                remaining = c_end - max(position, c_start) + 1
                if chunk.is_cached.value:
                    cached_bytes += remaining
                elif (cur_gen, c_start) in speculative or inflight == (
                    cur_gen,
                    c_start,
                ):
                    # Count all in-flight chunks (primary + speculative workers).
                    # Speculative workers may cover non-contiguous chunks; keep scanning
                    # but stop at the first uncovered gap after both sets.
                    in_flight_bytes += remaining
                else:
                    break
                cur_pos = c_end + 1
            manager.record_runway(
                cached_bytes=cached_bytes,
                in_flight_bytes=in_flight_bytes,
            )
        except Exception:
            # Runway telemetry must never affect delivery correctness.
            return

    def _cache_pressure_percentages(self) -> tuple[float, float]:
        """Return (cache_usage_pct, cache_protected_pct) safely for window sizing."""
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
        return cache_usage, cache_protected

    def _publish_telemetry_snapshot(self, stats: SessionStatistics) -> None:
        try:
            if hasattr(self, "stream_id"):
                from program.services.streaming.telemetry import (
                    playback_telemetry_collector,
                )

                playback_telemetry_collector.record_upstream_statistics(
                    self.stream_id, stats.upstream_snapshot()
                )
        except Exception:
            pass

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
                # Sweep any stale proactive transient entries before re-evaluating
                # the prefetch window; this prevents zero-waiter READY_TRANSIENT
                # entries from poisoning has_pending() and get_prefetch_uncached().
                await self._delivery_registry.expire_stale_transients()
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
        except (EmptyDataException, RecoverableMediaStreamException) as error:
            self._trace_stream(
                f"{label} did not return data ({error}); keeping the stream "
                "available for the next playhead read"
            )
            return False

        return True

    async def _stop_speculative_workers(self) -> None:
        scope = self._speculative_scope
        done = self._speculative_done
        if scope is not None:
            scope.cancel()
        if done is not None:
            await done.wait()

    def _uncached_foreground_starts(self) -> list[int]:
        """Return the reader reservations that still need provider bytes."""
        # A successful cache write wakes the reader before it releases its
        # reservation. That cache-backed tail must not stop the speculative span.
        return [
            start
            for start in self._delivery_registry.get_pending_starts()
            if not self.chunker.get_chunk_range(
                position=start
            ).first_chunk.is_cached.value
        ]

    def _should_defer_speculative_chunk(
        self,
        chunk_start: int,
        *,
        span_starts: set[int] | None = None,
    ) -> bool:
        """Yield only when unowned foreground demand needs priority over this transfer."""
        pending = self._uncached_foreground_starts()
        if not pending:
            return False
        batch_spec_starts = getattr(self, "_batch_spec_starts", None)
        for start in pending:
            if start == chunk_start or (
                span_starts is not None and start in span_starts
            ):
                # Waiting foreground reader needs a chunk owned by this worker/span.
                continue
            if batch_spec_starts is not None and start in batch_spec_starts:
                # Active sibling speculative worker in this batch owns this start.
                continue
            return True
        return False

    def _max_speculative_span_chunks(self) -> int:
        """Return max contiguous chunks a speculative worker coalesces into one GET."""
        cfg = getattr(self, "config", None)
        if cfg is None:
            return 1
        chunk_size = getattr(cfg, "chunk_size", 0)
        block_size = getattr(cfg, "block_size", 128 * 1024)
        if chunk_size < block_size or chunk_size >= 4 * 1024 * 1024:
            return 1
        return max(1, (4 * 1024 * 1024) // chunk_size)

    async def _commit_speculative_chunk(
        self,
        chunk: Chunk,
        data: bytes,
        generation: int,
        *,
        fetch_started: float,
    ) -> None:
        """Publish one verified speculative chunk into cache and telemetry."""
        from .cache import CachePutResult

        result = await self._cache_chunk(
            start=chunk.start,
            data=data,
            admission=(
                "demand"
                if self._delivery_registry.has_pending(chunk.start)
                else "prefetch"
            ),
        )
        if not self._transport_generation_live(generation):
            return
        if result in (
            CachePutResult.STORED_HOT,
            CachePutResult.STORED_WARM,
        ):
            chunk.emit_cache_signal()
        self._speculative_bytes += len(data)
        if hasattr(self, "adaptive_prefetch"):
            self.adaptive_prefetch.record_provider_fetch(monotonic() - fetch_started)

    def _speculative_timeout_budgets(self) -> tuple[float, float, float]:
        """Return bounded (header_budget_s, chunk_stall_s, span_budget_s) for speculative transfers."""
        config = getattr(self, "config", None)
        wait_sec = max(
            0.25,
            float(getattr(config, "chunk_wait_timeout_seconds", 10.0)),
        )
        # Keep speculative stalls faster than the foreground chunk-wait budget so
        # primary recovery retains headroom when chunk_wait_timeout_seconds >= 4s.
        chunk_stall_sec = min(7.0, wait_sec - 3.0) if wait_sec >= 4.0 else wait_sec
        header_budget_sec = min(5.0, chunk_stall_sec) if wait_sec >= 4.0 else wait_sec
        span_budget_sec = (
            max(chunk_stall_sec * 2.0, wait_sec * 0.6)
            if wait_sec >= 15.0
            else max(chunk_stall_sec * 2.0, wait_sec)
        )
        return header_budget_sec, chunk_stall_sec, span_budget_sec

    async def _fetch_speculative_span_run(
        self,
        sub_run: list[Chunk],
        generation: int,
        *,
        unattempted_out: list[Chunk] | None = None,
    ) -> bool:
        """Fetch a bounded contiguous chunk run in one streaming Range GET."""
        if not sub_run or not self._transport_generation_live(generation):
            return False

        claimed: list[tuple[Chunk, trio.Event]] = []
        stop_idx: int | None = None
        include_stop_chunk = False
        for loop_idx, chunk in enumerate(sub_run):
            if chunk.is_cached.value:
                stop_idx = loop_idx
                break
            acquired, claim = self._producer_claims.acquire(chunk.start)
            if not acquired:
                stop_idx = loop_idx
                break
            if chunk.is_cached.value:
                self._producer_claims.release(chunk.start, claim)
                stop_idx = loop_idx
                break
            if claimed and chunk.start != claimed[-1][0].start + claimed[-1][0].size:
                self._producer_claims.release(chunk.start, claim)
                stop_idx = loop_idx
                include_stop_chunk = True
                break
            claimed.append((chunk, claim))

        if stop_idx is not None and unattempted_out is not None:
            tail_start = stop_idx if include_stop_chunk else stop_idx + 1
            unattempted_out.extend(sub_run[tail_start:])

        if not claimed:
            return False

        if len(claimed) == 1:
            only_chunk, only_claim = claimed[0]
            self._producer_claims.release(only_chunk.start, only_claim)
            return await self._fetch_speculative_chunk(only_chunk, generation)

        span_starts = {c.start for c, _ in claimed}
        try:
            if self._should_defer_speculative_chunk(
                claimed[0][0].start, span_starts=span_starts
            ):
                return True
            pool = self._http_pool
            if pool is None:
                return False
            try:
                async with pool.admit("body", workload="background"):
                    if not self._transport_generation_live(generation):
                        return False
                    if self._should_defer_speculative_chunk(
                        claimed[0][0].start, span_starts=span_starts
                    ):
                        return True

                    lease = pool.acquire_lease(use_proxy=self._use_proxy_client)
                    spec_chunks = getattr(self, "_speculative_inflight_chunks", None)
                    span_start = claimed[0][0].start
                    span_end = claimed[-1][0].end
                    expected_total = span_end - span_start + 1
                    try:
                        self._speculative_active += 1
                        self._speculative_peak = max(
                            self._speculative_peak, self._speculative_active
                        )
                        if spec_chunks is not None:
                            for c, _ in claimed:
                                spec_chunks.add((generation, c.start))
                        url = self.target_url.value
                        header_budget_sec, chunk_stall_sec, span_budget_sec = (
                            self._speculative_timeout_budgets()
                        )
                        req_context = lease.client.stream(
                            "GET",
                            url,
                            timeout=self._request_http_timeout(),
                            headers={
                                "Accept-Encoding": "identity",
                                "Range": format_range_header(span_start, span_end),
                            },
                        )
                        span_started = trio.current_time()
                        hard_deadline = span_started + span_budget_sec
                        with trio.fail_at(
                            min(hard_deadline, span_started + header_budget_sec)
                        ) as chunk_scope:
                            async with observe_response(
                                req_context, self.session_statistics, speculative=True
                            ) as response:
                                response.raise_for_status()
                                parsed = validate_range_response(
                                    response,
                                    span_start,
                                    span_end,
                                    self.provider,
                                    self.file_metadata.file_size,
                                )
                                if (
                                    parsed is None
                                    or parsed.last_byte != span_end
                                    or parsed.complete_length
                                    != self.file_metadata.file_size
                                ):
                                    raise ValueError(
                                        "Speculative span Content-Range does not match exact span/file size"
                                    )
                                length = response.headers.get("Content-Length")
                                if length is not None and int(length) != expected_total:
                                    raise ValueError(
                                        "Speculative span Content-Length mismatch"
                                    )

                                buf = bytearray()
                                cursor = 0
                                consumed_total = 0
                                idx = 0
                                chunk_started = monotonic()
                                chunk_scope.deadline = min(
                                    hard_deadline, trio.current_time() + chunk_stall_sec
                                )
                                async for part in _iter_raw_chunks(response):
                                    if not self._transport_generation_live(generation):
                                        return False
                                    buf.extend(part)
                                    if (
                                        len(buf) - cursor
                                        > expected_total - consumed_total
                                    ):
                                        raise ValueError(
                                            "Speculative span response exceeds requested range"
                                        )
                                    while idx < len(claimed):
                                        cur, cur_claim = claimed[idx]
                                        if len(buf) - cursor < cur.size:
                                            break
                                        payload = bytes(buf[cursor : cursor + cur.size])
                                        cursor += cur.size
                                        consumed_total += cur.size
                                        # Suspend the per-chunk network stall timer while publishing
                                        # already-downloaded bytes to cache so disk/demotion contention
                                        # cannot discard a finished chunk (still bounded by hard_deadline).
                                        chunk_scope.deadline = hard_deadline
                                        await self._commit_speculative_chunk(
                                            cur,
                                            payload,
                                            generation,
                                            fetch_started=chunk_started,
                                        )
                                        self._producer_claims.release(
                                            cur.start, cur_claim
                                        )
                                        if spec_chunks is not None:
                                            spec_chunks.discard((generation, cur.start))
                                        idx += 1
                                        chunk_started = monotonic()
                                        chunk_scope.deadline = min(
                                            hard_deadline,
                                            trio.current_time() + chunk_stall_sec,
                                        )
                                    if cursor >= 1024 * 1024:
                                        del buf[:cursor]
                                        cursor = 0
                                if (
                                    idx != len(claimed)
                                    or consumed_total != expected_total
                                ):
                                    raise ValueError(
                                        "Truncated speculative span response"
                                    )
                    finally:
                        self._speculative_active -= 1
                        if spec_chunks is not None:
                            for c, _ in claimed:
                                spec_chunks.discard((generation, c.start))
                        with trio.CancelScope(shield=True):
                            await pool.release_lease(lease)
            except httpx.PoolTimeout:
                return False
        except trio.Cancelled:
            raise
        except Exception as exc:
            self._speculative_failures += 1
            self.session_statistics.speculative_failures += 1
            self.session_statistics.publish()
            logger.debug(
                self.build_log_message(
                    f"Speculative span skipped: {type(exc).__name__}"
                )
            )
        finally:
            for c, claim in claimed:
                self._producer_claims.release(c.start, claim)
        return False

    async def _fetch_speculative_chunk(self, chunk: Chunk, generation: int) -> bool:
        """Fetch one bounded range; return True only if foreground demand deferred it."""
        if not self._transport_generation_live(generation) or chunk.is_cached.value:
            return False
        acquired, claim = self._producer_claims.acquire(chunk.start)
        if not acquired:
            return False
        try:
            if chunk.is_cached.value:
                return False
            if self._should_defer_speculative_chunk(chunk.start):
                return True
            # Reserve a body token before opening the independent HTTP response.
            # Speculative saturation is a soft yield, never a pool heal.
            pool = self._http_pool
            if pool is None:
                return False
            try:
                async with pool.admit("body", workload="background"):
                    if (
                        not self._transport_generation_live(generation)
                        or chunk.is_cached.value
                    ):
                        return False
                    if self._should_defer_speculative_chunk(chunk.start):
                        return True
                    lease = pool.acquire_lease(use_proxy=self._use_proxy_client)
                    try:
                        self._speculative_active += 1
                        self._speculative_peak = max(
                            self._speculative_peak, self._speculative_active
                        )
                        spec_chunks = getattr(
                            self, "_speculative_inflight_chunks", None
                        )
                        if spec_chunks is not None:
                            spec_chunks.add((generation, chunk.start))
                        url = self.target_url.value
                        header_budget_sec, chunk_stall_sec, _ = (
                            self._speculative_timeout_budgets()
                        )
                        req_context = lease.client.stream(
                            "GET",
                            url,
                            timeout=self._request_http_timeout(),
                            headers={
                                "Accept-Encoding": "identity",
                                "Range": format_range_header(chunk.start, chunk.end),
                            },
                        )
                        with trio.fail_at(
                            trio.current_time() + header_budget_sec
                        ) as chunk_scope:
                            async with observe_response(
                                req_context, self.session_statistics, speculative=True
                            ) as response:
                                response.raise_for_status()
                                parsed = validate_range_response(
                                    response,
                                    chunk.start,
                                    chunk.end,
                                    self.provider,
                                    self.file_metadata.file_size,
                                )
                                if (
                                    parsed is None
                                    or parsed.last_byte != chunk.end
                                    or parsed.complete_length
                                    != self.file_metadata.file_size
                                ):
                                    raise ValueError(
                                        "Speculative Content-Range does not match exact chunk/file size"
                                    )
                                length = response.headers.get("Content-Length")
                                if length is not None and int(length) != chunk.size:
                                    raise ValueError(
                                        "Speculative Content-Length mismatch"
                                    )
                                data = bytearray()
                                chunk_scope.deadline = (
                                    trio.current_time() + chunk_stall_sec
                                )
                                async for part in _iter_raw_chunks(response):
                                    if not self._transport_generation_live(generation):
                                        return False
                                    data.extend(part)
                                    if len(data) > chunk.size:
                                        raise ValueError(
                                            "Speculative response exceeds requested chunk"
                                        )
                                if len(data) != chunk.size:
                                    raise ValueError("Truncated speculative response")
                        if not self._transport_generation_live(generation):
                            return False
                        await self._commit_speculative_chunk(
                            chunk,
                            bytes(data),
                            generation,
                            fetch_started=monotonic(),
                        )
                    finally:
                        self._speculative_active -= 1
                        spec_chunks = getattr(
                            self, "_speculative_inflight_chunks", None
                        )
                        if spec_chunks is not None:
                            spec_chunks.discard((generation, chunk.start))
                        with trio.CancelScope(shield=True):
                            await pool.release_lease(lease)
            except httpx.PoolTimeout:
                return False
        except trio.Cancelled:
            raise
        except Exception as exc:
            self._speculative_failures += 1
            self.session_statistics.speculative_failures += 1
            self.session_statistics.publish()
            logger.debug(
                self.build_log_message(
                    f"Speculative fetch skipped: {type(exc).__name__}"
                )
            )
        finally:
            self._producer_claims.release(chunk.start, claim)
        return False

    async def _fetch_speculative_span(
        self,
        span: list[Chunk],
        generation: int,
        primary_span: list[Chunk] | None = None,
        primary_done: trio.Event | None = None,
        *,
        work_queues: list[deque[Chunk]] | None = None,
        queue_index: int | None = None,
        on_ownership_change: Callable[[], None] | None = None,
    ) -> None:
        """Yield to unowned foreground work, coalesce contiguous runs, and steal stalled tails."""
        primary: dict[int, Chunk] | None = (
            {(c if isinstance(c, int) else c.start): c for c in primary_span}
            if primary_span is not None
            else None
        )
        my_queue: deque[Chunk] = (
            work_queues[queue_index]
            if (work_queues is not None and queue_index is not None)
            else deque(span)
        )
        max_run = self._max_speculative_span_chunks()

        def _is_cached(item: Any) -> bool:
            cached = getattr(item, "is_cached", None)
            return bool(getattr(cached, "value", False))

        def _item_start(item: Chunk | int) -> int:
            return item if isinstance(item, int) else item.start

        def _pop_next_run() -> list[Any]:
            run: list[Any] = []
            while my_queue and len(run) < max_run:
                head = my_queue[0]
                if _is_cached(head):
                    my_queue.popleft()
                    continue
                if run and (
                    not hasattr(head, "start")
                    or not hasattr(run[-1], "start")
                    or not hasattr(run[-1], "size")
                    or head.start != run[-1].start + run[-1].size
                ):
                    break
                run.append(my_queue.popleft())
            if run or work_queues is None:
                return run

            # Own queue drained: steal contiguous unclaimed chunks from the tail
            # of the longest peer queue so straggling workers never stall the batch.
            steal_source = max(
                (q for q in work_queues if q is not my_queue and len(q) > 0),
                key=len,
                default=None,
            )
            if steal_source is None:
                return []
            steal_cap = min(max_run, max(1, len(steal_source) // 2))
            stolen: deque[Any] = deque()
            while steal_source and len(stolen) < steal_cap:
                tail = steal_source[-1]
                if _is_cached(tail):
                    steal_source.pop()
                    continue
                tail_start = _item_start(tail)
                claims = getattr(self, "_producer_claims", None)
                if claims is not None and claims.is_claimed(tail_start):
                    break
                if stolen and (
                    not hasattr(tail, "start")
                    or not hasattr(tail, "size")
                    or not hasattr(stolen[0], "start")
                    or tail.start + tail.size != stolen[0].start
                ):
                    break
                stolen.appendleft(steal_source.pop())
            return list(stolen)

        batch_spec = getattr(self, "_batch_spec_starts", None)
        try:
            while self._transport_generation_live(generation):
                sub_run = _pop_next_run()
                if not sub_run:
                    return
                sub_starts = {_item_start(c) for c in sub_run}
                while self._transport_generation_live(generation):
                    pending = (
                        self._uncached_foreground_starts()
                        if hasattr(self, "_delivery_registry")
                        else []
                    )
                    blocking_pending = [
                        s
                        for s in pending
                        if s not in sub_starts
                        and (batch_spec is None or s not in batch_spec)
                    ]
                    if not blocking_pending:
                        if len(sub_run) == 1:
                            deferred = await self._fetch_speculative_chunk(
                                sub_run[0], generation
                            )
                        else:
                            unattempted_tail: list[Chunk] = []
                            deferred = await self._fetch_speculative_span_run(
                                sub_run,
                                generation,
                                unattempted_out=unattempted_tail,
                            )
                            if unattempted_tail:
                                attempted_end = len(sub_run) - len(unattempted_tail)
                                for tail_chunk in reversed(unattempted_tail):
                                    my_queue.appendleft(tail_chunk)
                                sub_run = sub_run[:attempted_end]
                                sub_starts = {_item_start(c) for c in sub_run}
                        if not deferred:
                            discarded = False
                            claims = getattr(self, "_producer_claims", None)
                            if batch_spec is not None:
                                for c in sub_run:
                                    if not _is_cached(c):
                                        start_val = _item_start(c)
                                        if start_val in batch_spec and (
                                            claims is None
                                            or not claims.is_claimed(start_val)
                                        ):
                                            batch_spec.discard(start_val)
                                            discarded = True
                            if discarded and on_ownership_change is not None:
                                on_ownership_change()
                            break
                        pending = (
                            self._uncached_foreground_starts()
                            if hasattr(self, "_delivery_registry")
                            else []
                        )
                        blocking_pending = [
                            s
                            for s in pending
                            if s not in sub_starts
                            and (batch_spec is None or s not in batch_spec)
                        ]
                        if not blocking_pending:
                            continue

                    if (
                        primary is None
                        or primary_done is None
                        or primary_done.is_set()
                        or any(start not in primary for start in blocking_pending)
                    ):
                        for c in reversed(sub_run):
                            my_queue.appendleft(c)
                        return
                    start = blocking_pending[0]
                    reservation = self._delivery_registry.get_entry(start)
                    primary_chunk = primary[start]
                    if reservation is not None:
                        if hasattr(primary_chunk, "is_cached"):
                            await trio_util.wait_any(
                                lambda: primary_chunk.is_cached.wait_value(True),
                                reservation.ready.wait,
                                primary_done.wait,
                            )
                        else:
                            await trio_util.wait_any(
                                reservation.ready.wait,
                                primary_done.wait,
                            )
                    elif hasattr(primary_chunk, "is_cached"):
                        await trio_util.wait_any(
                            lambda: primary_chunk.is_cached.wait_value(True),
                            primary_done.wait,
                        )
                    else:
                        await primary_done.wait()
                else:
                    return
        finally:
            discarded_leftover = False
            if batch_spec is not None:
                claims = getattr(self, "_producer_claims", None)
                while my_queue:
                    leftover = my_queue.popleft()
                    start_val = _item_start(leftover)
                    if start_val in batch_spec and (
                        claims is None or not claims.is_claimed(start_val)
                    ):
                        batch_spec.discard(start_val)
                        discarded_leftover = True
            if discarded_leftover and on_ownership_change is not None:
                on_ownership_change()

    def _partition_prefetch_spans(
        self, chunks: OrderedSet[Chunk] | list[Chunk], parallel_workers: int
    ) -> tuple[list[Chunk], list[list[Chunk]]]:
        """Partition prefetch chunks into non-overlapping contiguous transfer spans.

        The primary socket is assigned the first contiguous transfer span so that
        the active HTTP response can stream sequentially without encountering
        isolated cached holes. Speculative workers are assigned disjoint contiguous spans
        beyond the primary span.
        """
        chunks_list = list(chunks)
        if not chunks_list or parallel_workers <= 1:
            return chunks_list, []

        if not hasattr(chunks_list[0], "start") or not hasattr(chunks_list[0], "size"):
            primary_len = 1
            primary_span = chunks_list[:primary_len]
            remaining = chunks_list[primary_len:]
            num_spec = parallel_workers - 1
            spec_spans: list[list[Chunk]] = []
            if remaining and num_spec > 0:
                for item in remaining[:num_spec]:
                    spec_spans.append([item])
            return primary_span, spec_spans

        contiguous_run = [chunks_list[0]]
        expected_start = chunks_list[0].start + chunks_list[0].size
        idx = 1
        while idx < len(chunks_list) and chunks_list[idx].start == expected_start:
            contiguous_run.append(chunks_list[idx])
            expected_start = chunks_list[idx].start + chunks_list[idx].size
            idx += 1

        total_len = len(chunks_list)
        k = parallel_workers
        primary_len = min(len(contiguous_run), max(1, (total_len + k - 1) // k))
        primary_span = chunks_list[:primary_len]
        remaining = chunks_list[primary_len:]

        num_spec = k - 1
        speculative_spans: list[list[Chunk]] = []
        if remaining and num_spec > 0:
            spec_len = max(1, (len(remaining) + num_spec - 1) // num_spec)
            for i in range(num_spec):
                span = remaining[i * spec_len : (i + 1) * spec_len]
                if span:
                    speculative_spans.append(span)

        return primary_span, speculative_spans

    async def _parallel_prefetch(
        self,
        chunks: OrderedSet[Chunk],
        generation: int,
        process_chunks: Callable[..., Awaitable[None]],
    ) -> None:
        """Bounded generation-owned batch with coordinated non-overlapping transfer spans."""
        if self._speculative_done is not None:
            await self._speculative_done.wait()
        done = trio.Event()
        self._speculative_done = done
        try:
            primary_span, spec_spans = self._partition_prefetch_spans(
                chunks, self.parallel_prefetch_workers
            )
            primary_done = trio.Event()
            ownership_changed = trio.Event()

            def _notify_ownership_changed() -> None:
                nonlocal ownership_changed
                ownership_changed.set()
                ownership_changed = trio.Event()

            batch_starts: set[int] = {
                chunk if isinstance(chunk, int) else chunk.start for chunk in chunks
            }
            spec_starts: set[int] = {
                chunk if isinstance(chunk, int) else chunk.start
                for span in spec_spans
                for chunk in span
            }
            self._batch_spec_starts = spec_starts
            work_queues = [deque(span) for span in spec_spans]

            def _has_unowned_demand() -> bool:
                recent_reads = getattr(self, "recent_reads", None)
                if recent_reads is None or recent_reads.current_read.value is None:
                    return False
                claims = getattr(self, "_producer_claims", None)
                primary_finished = primary_done.is_set()
                for start in self._uncached_foreground_starts():
                    if start not in batch_starts:
                        return True
                    if (
                        primary_finished
                        and start not in spec_starts
                        and (claims is None or not claims.is_claimed(start))
                    ):
                        return True
                return False

            async def wait_for_unowned_demand() -> None:
                recent_reads = getattr(self, "recent_reads", None)
                if recent_reads is None:
                    await trio.sleep_forever()
                else:
                    while not _has_unowned_demand():
                        cur_event = ownership_changed
                        await trio_util.wait_any(
                            lambda: recent_reads.current_read.wait_value(
                                lambda _v: _has_unowned_demand()
                            ),
                            cur_event.wait,
                        )

            # The nursery is a batch barrier. Demand outside active batch owners
            # has no producer here, so waiting for an unrelated speculative body
            # would stall playback until that body's timeout. Drain its owners
            # first, then let the main loop dispatch the queued foreground read.
            async with trio_util.move_on_when(wait_for_unowned_demand) as scope:
                self._speculative_scope = scope
                async with trio.open_nursery() as nursery:
                    for q_idx, span in enumerate(spec_spans):
                        nursery.start_soon(
                            partial(
                                self._fetch_speculative_span,
                                span,
                                generation,
                                primary_span,
                                primary_done,
                                work_queues=work_queues,
                                queue_index=q_idx,
                                on_ownership_change=_notify_ownership_changed,
                            )
                        )
                    try:
                        await process_chunks(
                            OrderedSet(primary_span),
                            is_prefetch=True,
                            prefetch_gen=generation,
                        )
                    finally:
                        primary_done.set()
                        _notify_ownership_changed()
                    if not self._transport_generation_live(generation):
                        nursery.cancel_scope.cancel()
        finally:
            self._batch_spec_starts = None
            self._speculative_scope = None
            self._speculative_done = None
            done.set()

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
                        EmptyDataException,
                        RecoverableMediaStreamException,
                    ),
                ):
                    raise error from group
                raise
        except (
            EmptyDataException,
            RecoverableMediaStreamException,
            DebridServiceRateLimitedException,
            DebridServiceRefusedRangeRequestException,
            DebridServiceClosedConnectionException,
            httpx.ReadError,
            httpx.TimeoutException,
            httpx.RemoteProtocolError,
        ) as e:
            if isinstance(e, RecoverableMediaStreamException):
                raise
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
                            position = getattr(
                                seek_range, "position", seek_range.request_range[0]
                            )
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
                                    owned: bool = False,
                                ) -> None:
                                    nonlocal needs_url_refresh, position
                                    if (
                                        not owned
                                        and getattr(
                                            self, "parallel_prefetch_workers", 1
                                        )
                                        > 1
                                    ):
                                        for claimed_chunk in chunks:
                                            while not claimed_chunk.is_cached.value:
                                                # A nested read may have requested a reconnect.
                                                # Preserve its first missing offset until the
                                                # connection watcher actually replaces the body.
                                                if connection.seek_required.value:
                                                    return
                                                delivery = (
                                                    self._delivery_registry.get_entry(
                                                        claimed_chunk.start
                                                    )
                                                )
                                                if (
                                                    delivery is not None
                                                    and delivery.state
                                                    == _DeliveryState.READY_TRANSIENT
                                                ):
                                                    break
                                                acquired, claim = (
                                                    self._producer_claims.acquire(
                                                        claimed_chunk.start
                                                    )
                                                )
                                                if not acquired:
                                                    if is_prefetch:
                                                        break
                                                    await claim.wait()
                                                    continue
                                                try:
                                                    await _process_chunks(
                                                        OrderedSet([claimed_chunk]),
                                                        is_prefetch=is_prefetch,
                                                        prefetch_gen=prefetch_gen,
                                                        owned=True,
                                                    )
                                                finally:
                                                    self._producer_claims.release(
                                                        claimed_chunk.start, claim
                                                    )
                                                break
                                        return
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
                                            stream_consumed = False
                                            try:
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
                                                            1.0
                                                            if wait_sec >= 2.0
                                                            else 0.5
                                                        )
                                                    )
                                                    body_read_stall_timeout = min(
                                                        7.0,
                                                        max(0.5, wait_sec - headroom),
                                                    )
                                                    # A trickle can keep resetting the idle timer
                                                    # without delivering a usable chunk. One body
                                                    # attempt must also respect the chunk wait budget.
                                                    chunk_deadline = (
                                                        trio.current_time() + wait_sec
                                                    )
                                                    while (
                                                        len(chunk_buffer) < chunk.size
                                                    ):
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
                                                                if (
                                                                    len(chunk_buffer)
                                                                    > 0
                                                                    or stream_consumed
                                                                ):
                                                                    self._invalidate_misaligned_connection(
                                                                        connection,
                                                                        chunk_start=chunk.start,
                                                                        chunk_size=chunk.size,
                                                                    )
                                                                return
                                                        raw_part = getattr(
                                                            connection,
                                                            "pending_body",
                                                            b"",
                                                        )
                                                        if not raw_part:
                                                            # Empty fragments are not byte progress.
                                                            idle_deadline = (
                                                                trio.current_time()
                                                                + body_read_stall_timeout
                                                            )
                                                            deadline = min(
                                                                chunk_deadline,
                                                                idle_deadline,
                                                            )
                                                            try:
                                                                with trio.fail_at(
                                                                    deadline
                                                                ):
                                                                    while not raw_part:
                                                                        raw_part = await anext(
                                                                            connection.reader
                                                                        )
                                                            except (
                                                                trio.TooSlowError
                                                            ) as err:
                                                                if (
                                                                    deadline
                                                                    == chunk_deadline
                                                                ):
                                                                    raise httpx.ReadTimeout(
                                                                        f"HTTP response-body chunk did not complete within {wait_sec:.1f}s"
                                                                    ) from err
                                                                raise httpx.ReadTimeout(
                                                                    f"HTTP response-body read stalled for {body_read_stall_timeout:.1f}s"
                                                                ) from err
                                                            except StopAsyncIteration:
                                                                break
                                                        remaining = chunk.size - len(
                                                            chunk_buffer
                                                        )
                                                        stream_consumed = True
                                                        chunk_buffer.extend(
                                                            raw_part[:remaining]
                                                        )
                                                        connection.pending_body = (
                                                            raw_part[remaining:]
                                                        )
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
                                                        self._invalidate_misaligned_connection(
                                                            connection,
                                                            chunk_start=chunk.start,
                                                            chunk_size=chunk.size,
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
                                                            range=(
                                                                chunk.start,
                                                                chunk.end,
                                                            )
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

                                                    cache_result = await self._cache_chunk(
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
                                                    if hasattr(
                                                        self, "adaptive_prefetch"
                                                    ):
                                                        self.adaptive_prefetch.record_provider_fetch(
                                                            monotonic() - fetch_started
                                                        )

                                                    from .cache import CachePutResult

                                                    if cache_result in (
                                                        CachePutResult.STORED_HOT,
                                                        CachePutResult.STORED_WARM,
                                                    ):
                                                        chunk.emit_cache_signal()

                                                    connection.current_read_position += len(
                                                        data
                                                    )
                                                    stream_consumed = False

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
                                                    self.session_statistics.publish()
                                            except trio.Cancelled:
                                                if stream_consumed or getattr(
                                                    getattr(
                                                        connection, "response", None
                                                    ),
                                                    "is_closed",
                                                    False,
                                                ):
                                                    self._invalidate_misaligned_connection(
                                                        connection,
                                                        chunk_start=chunk.start,
                                                        chunk_size=chunk.size,
                                                        prefer_pending_demand=True,
                                                    )
                                                raise

                                if seek_range:
                                    await _process_chunks(seek_range.uncached_chunks)
                                    seek_range = None

                                async def _drain_pending_demands() -> bool:
                                    """Seek to or serve any pending foreground chunk before blocking/prefetching."""
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
                                            != first_pending.start
                                        ):
                                            connection.seek(chunk_range=pending_range)
                                            self._protect_consumer(first_pending.start)
                                            return True
                                        await _process_chunks(
                                            OrderedSet([first_pending])
                                        )
                                        if connection.seek_required.value:
                                            return True
                                    return False

                                # Read.current_read is intentionally a latest-value
                                # signal, not a queue. Drain every registered body
                                # reservation first so a rapid subsequent read cannot
                                # erase an earlier producer demand.
                                if not await _drain_pending_demands():
                                    async for (
                                        read
                                    ) in self.recent_reads.current_read.eventual_values(
                                        lambda v: (
                                            v is not None
                                            and v.read_type
                                            in ("body_read", "cache_hit")
                                        )
                                    ):
                                        if not read:
                                            raise ValueError(
                                                self.build_log_message(
                                                    "No read available"
                                                )
                                            )

                                        uncached_chunks = (
                                            read.chunk_range.uncached_chunks
                                        )

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
                                                < connection.current_read_position
                                            ):
                                                # Backward seek detection:
                                                #
                                                # If the requested start is before the current wire position, we will always need to seek.
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
                                                self._protect_consumer(
                                                    read.chunk_range.position
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
                                                self._protect_consumer(
                                                    read.chunk_range.position
                                                )

                                                break

                                            await _process_chunks(uncached_chunks)
                                            if connection.seek_required.value:
                                                break

                                        # Drain any other concurrently queued demands that arrived
                                        # while processing this read before waiting for the next
                                        # event, ensuring older or parallel demands are never starved.
                                        if await _drain_pending_demands():
                                            break

                                        # Sequential playhead prefetch: fill ahead without
                                        # blocking the current VFS read (already returned /
                                        # waiting independently via is_cached).
                                        consumer_end = getattr(
                                            self, "_consumer_end", None
                                        )
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
                                            or (
                                                ap_manager.should_refill_prefetch()
                                                if hasattr(
                                                    ap_manager,
                                                    "should_refill_prefetch",
                                                )
                                                else ap_manager.should_prefetch_now()
                                            )
                                            or ap_manager.get_effective_bitrate() <= 0
                                            or (
                                                hasattr(read, "chunk_range")
                                                and self._is_sequential_cache_playback(
                                                    read.chunk_range
                                                )
                                            )
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

                                            cache_usage, cache_protected = (
                                                self._cache_pressure_percentages()
                                            )

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
                                                ahead = (
                                                    self.chunker.get_prefetch_uncached(
                                                        after_end=playhead_end,
                                                        count=prefetch_window,
                                                    )
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
                                                    parallel_workers = getattr(
                                                        self,
                                                        "parallel_prefetch_workers",
                                                        1,
                                                    )
                                                    wave_cap: int | None = None
                                                    if (
                                                        parallel_workers > 1
                                                        and prefetch_gen is not None
                                                        and self.config.chunk_size
                                                        >= getattr(
                                                            self.config,
                                                            "block_size",
                                                            128 * 1024,
                                                        )
                                                    ):
                                                        wave_cap = max(
                                                            parallel_workers * 2,
                                                            parallel_workers
                                                            * self._max_speculative_span_chunks(),
                                                        )
                                                    batch_ahead = (
                                                        OrderedSet(
                                                            list(ahead)[:wave_cap]
                                                        )
                                                        if wave_cap is not None
                                                        and len(ahead) > wave_cap
                                                        else ahead
                                                    )
                                                    # Prefer contiguous fetch from connection tip;
                                                    # seek if gap to first prefetch chunk.
                                                    first = batch_ahead[0]
                                                    if (
                                                        connection.current_read_position
                                                        < first.start
                                                    ):
                                                        gap_bytes = (
                                                            first.start
                                                            - connection.current_read_position
                                                        )
                                                        gap_range = (
                                                            self.chunker.get_chunk_range(
                                                                position=connection.current_read_position,
                                                                size=gap_bytes,
                                                            )
                                                            if gap_bytes
                                                            <= self.config.chunk_size
                                                            * 2
                                                            else None
                                                        )
                                                        if (
                                                            gap_range is not None
                                                            and gap_range.uncached_chunks
                                                        ):
                                                            prefetched = await self._run_opportunistic_prefetch(
                                                                gap_range.uncached_chunks,
                                                                _process_chunks,
                                                                label="Prefetch gap-fill",
                                                                prefetch_gen=prefetch_gen,
                                                            )
                                                            if (
                                                                not prefetched
                                                                or connection.seek_required.value
                                                            ):
                                                                break
                                                        else:
                                                            # Avoid tearing down the primary HTTP stream
                                                            # just to fetch a 1-2 chunk tail edge when
                                                            # speculative workers already filled runway ahead.
                                                            footer_limit = getattr(
                                                                self.chunker,
                                                                "footer_start",
                                                                self.file_metadata.file_size,
                                                            )
                                                            at_eof_horizon = (
                                                                playhead_end
                                                                + (prefetch_window + 1)
                                                                * self.config.chunk_size
                                                                >= footer_limit
                                                            )
                                                            effective_batch = (
                                                                min(
                                                                    prefetch_window,
                                                                    wave_cap,
                                                                )
                                                                if wave_cap is not None
                                                                else prefetch_window
                                                            )
                                                            min_batch = (
                                                                min(
                                                                    effective_batch,
                                                                    max(
                                                                        parallel_workers,
                                                                        effective_batch
                                                                        // 2,
                                                                    ),
                                                                )
                                                                if wave_cap is not None
                                                                else min(
                                                                    prefetch_window,
                                                                    max(
                                                                        parallel_workers
                                                                        * 2,
                                                                        prefetch_window
                                                                        // 2,
                                                                    ),
                                                                )
                                                            )
                                                            cached_runway_chunks = max(
                                                                0,
                                                                (
                                                                    first.start
                                                                    - (playhead_end + 1)
                                                                )
                                                                // max(
                                                                    1,
                                                                    self.config.chunk_size,
                                                                ),
                                                            )
                                                            if (
                                                                parallel_workers > 1
                                                                and not at_eof_horizon
                                                                and cached_runway_chunks
                                                                >= min_batch
                                                                and len(batch_ahead)
                                                                < min_batch
                                                            ):
                                                                continue

                                                            connection.seek(
                                                                chunk_range=self.chunker.get_chunk_range(
                                                                    position=first.start,
                                                                    size=first.size,
                                                                )
                                                            )
                                                            break
                                                    if (
                                                        parallel_workers > 1
                                                        and prefetch_gen is not None
                                                    ):
                                                        await self._parallel_prefetch(
                                                            batch_ahead,
                                                            prefetch_gen,
                                                            _process_chunks,
                                                        )
                                                        if (
                                                            connection.seek_required.value
                                                        ):
                                                            break
                                                    else:
                                                        prefetched = await self._run_opportunistic_prefetch(
                                                            ahead,
                                                            _process_chunks,
                                                            label="Prefetch",
                                                            prefetch_gen=prefetch_gen,
                                                        )
                                                        if not prefetched:
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

        conn_ctx = self.establish_connection(start=chunk_aligned_start)
        async with observe_response(conn_ctx, self.session_statistics) as response:
            stream_connection = StreamConnection(
                response=response,
                start_position=chunk_aligned_start,
                current_read_position=chunk_aligned_start,
                reader=_iter_raw_chunks(response),
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
                spec_chunks = getattr(self, "_speculative_inflight_chunks", None)
                if spec_chunks is not None:
                    spec_chunks.clear()
                if self._active_stream_connection is stream_connection:
                    self._active_stream_connection = None

    async def close(self) -> None:
        """Immediately terminate the active stream."""

        # Fence even an idle/cache-only stream before any further refill.
        self.is_killed.value = True
        self._inflight_chunk = None
        spec_chunks = getattr(self, "_speculative_inflight_chunks", None)
        if spec_chunks is not None:
            spec_chunks.clear()
        self._producer_claims.wake_all()
        await self._stop_speculative_workers()
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

        if not self.is_streaming.value and self.config.prefetch_chunks > 0:
            async with self._start_lock:
                if not self.is_streaming.value:
                    try:
                        self.nursery.start_soon(self.run, self.config.header_size)
                    except Exception:
                        pass

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
                    if manager.current_generation != generation:
                        consumer_end = end_byte
                        self._consumer_end = consumer_end
                        self._update_runway_observation(consumer_end + 1)
                        await self._stop_speculative_workers()
                        connection = self._active_stream_connection
                        if connection is not None and read_type == "body_read":
                            connection.seek(chunk_range=chunk_range)
                    else:
                        consumer_end = max(
                            getattr(self, "_consumer_end", 0) or 0, end_byte
                        )
                        self._consumer_end = consumer_end
                        self._update_runway_observation(consumer_end + 1)
                    self._protect_consumer(start_byte)
                else:
                    self._consumer_end = max(
                        getattr(self, "_consumer_end", 0) or 0, end_byte
                    )
                    self._protect_consumer(start_byte)

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
                and not self.is_streaming.value
                and (
                    getattr(self, "adaptive_prefetch", None) is None
                    or (
                        self.adaptive_prefetch.should_refill_prefetch()
                        if hasattr(
                            self.adaptive_prefetch,
                            "should_refill_prefetch",
                        )
                        else self.adaptive_prefetch.should_prefetch_now()
                    )
                    or self.adaptive_prefetch.get_effective_bitrate() <= 0
                    or self._is_sequential_cache_playback(chunk_range)
                )
            ):
                _, playhead_end = chunk_range.request_range
                cache_usage, cache_protected = self._cache_pressure_percentages()

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

        start, _ = chunk_range.request_range
        if start < self.config.header_size or start >= self.chunker.footer_start:
            return False

        interleave_tolerance = max(
            self.config.sequential_read_tolerance,
            16 * 1024 * 1024,
        )

        previous_end = getattr(self, "_consumer_end", None)
        if previous_end is not None:
            # Active playback horizon relative to current consumer playhead:
            # Accepts interleaved audio/subtitles behind playhead and video strides ahead
            horizon_min = max(
                self.config.header_size, previous_end - interleave_tolerance
            )
            horizon_max = previous_end + interleave_tolerance * 2
            if horizon_min <= start <= horizon_max:
                return True

        # Check proximity to the immediate previous read
        previous = self.recent_reads.previous_read.value
        if previous is not None and previous.read_type in _HOT_STREAM_READ_TYPES:
            prev_start, prev_end = previous.chunk_range.request_range
            if (
                prev_start >= self.config.header_size
                and prev_end < self.chunker.footer_start
                and abs(start - prev_end - 1) <= interleave_tolerance
            ):
                return True

        return False

    def _protect_consumer(self, request_start: int) -> None:
        """Metadata probes must not move the foreground lease window."""
        # Protect active playhead lookback and lookahead chunks in VFS cache.
        # Throttle reconciliation to chunk-boundary transitions or >= 5s intervals
        # so 128 KiB FUSE slice reads inside the same chunk do not repeatedly
        # recompute SHA-1 keys and rebuild hundreds of StreamLease entries under _thread_lock.
        try:
            from .cache import Cache

            if Cache in di and hasattr(self, "stream_id"):
                chunk_size = getattr(getattr(self, "chunker", None), "chunk_size", 0)
                bucket = (
                    request_start // chunk_size
                    if isinstance(chunk_size, int) and chunk_size > 0
                    else None
                )
                now = monotonic()
                last_bucket = getattr(self, "_last_protected_bucket", None)
                last_at = getattr(self, "_last_protected_at", None)
                if (
                    bucket is not None
                    and last_bucket == bucket
                    and last_at is not None
                    and (now - last_at) < 5.0
                ):
                    return
                self._last_protected_bucket = bucket
                self._last_protected_at = now
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

    def _request_http_timeout(self) -> httpx.Timeout:
        """Apply this handle's connection budget without mutating pooled clients."""
        config = getattr(self, "config", None)
        return stream_http_timeout(
            connect_timeout=getattr(config, "connect_timeout_seconds", None)
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
                if request_kind == "body" and self._is_foreground_eligible():
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
                        timeout=self._request_http_timeout(),
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

        session_stats = getattr(self, "session_statistics", None)
        body_read_count = (
            getattr(session_stats, "body_read_count", 0)
            if session_stats is not None
            else 0
        )
        is_streaming_obj = getattr(self, "is_streaming", None)
        is_streaming_active = (
            getattr(is_streaming_obj, "value", False)
            if is_streaming_obj is not None
            else False
        )

        if (
            body_read_count == 0
            and not is_streaming_active
            and (
                (
                    self.recent_reads.last_read_end is not None
                    and (
                        abs(self.recent_reads.last_read_end - start)
                        > self.config.scan_tolerance
                    )
                    and start != self.config.header_size
                    and size <= self.config.block_size * 2
                )
                or (
                    start > self.config.header_size
                    and self.recent_reads.last_read_end is None
                )
            )
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
            conn_ctx = self.establish_connection(
                start=start,
                end=start + size - 1,
            )
            async with observe_response(conn_ctx, self.session_statistics) as response:
                data = await response.aread()

                self.session_statistics.bytes_transferred += len(data)
                publish_fn = getattr(self.session_statistics, "publish", None)
                if callable(publish_fn):
                    publish_fn()

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
        while not chunk.is_cached.value:
            entry = self._delivery_registry.get_entry(chunk.start)
            if entry is not None and entry.state == _DeliveryState.READY_TRANSIENT:
                return
            if entry is not None and entry.state == _DeliveryState.RETIRED:
                return
            producer_claims = getattr(self, "_producer_claims", None)
            claim = (
                producer_claims._owners.get(chunk.start)
                if producer_claims is not None
                else None
            )
            if claim is not None:
                await trio_util.wait_any(
                    lambda: chunk.is_cached.wait_value(True),
                    claim.wait,
                    *([entry.ready.wait] if entry is not None else []),
                )
                if not chunk.is_cached.value and (
                    entry is None or entry.state == _DeliveryState.PENDING
                ):
                    # Ownership was abandoned; retain reader reservation and let
                    # foreground claim the chunk rather than treating it as delivered.
                    if (
                        producer_claims is not None
                        and producer_claims._owners.get(chunk.start) is None
                    ):
                        self.recent_reads.current_read.value = Read(
                            chunk_range=self.chunker.get_chunk_range(
                                position=chunk.start, size=1
                            ),
                            read_type="body_read",
                        )
                    await trio.lowlevel.checkpoint()
                continue
            if entry is None:
                await chunk.is_cached.wait_value(True)
            else:
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

        if result in (
            CachePutResult.SKIPPED_PREFETCH_PRESSURE,
            CachePutResult.REFUSED_PHYSICAL_PRESSURE,
        ):
            # Persistent cache cannot admit this chunk. Publish bytes directly
            # into the delivery registry so waiting or upcoming readers get the
            # data without issuing a second HTTP request (one-provider-fetch invariant).
            await self._delivery_registry.publish(start=start, payload=data)
            logger.debug(
                self.build_log_message(
                    f"Cache admission refused/skipped for start={start} ({len(data)} bytes); "
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
                # Trio deadlines have a different epoch from provider monotonic time.
                remaining = state.recovery_deadline - trio.current_time()
                effective_deadline = now_mono + max(0.0, remaining)
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
