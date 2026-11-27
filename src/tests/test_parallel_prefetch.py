"""Focused bounded speculative transfer and producer claim invariants."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import trio
from pydantic import ValidationError

from program.services.streaming.media_stream import MediaStream, _ProducerClaims
from program.services.streaming.session_statistics import SessionStatistics
from program.settings.models import StreamModel


def test_worker_setting_rejects_unsupported_values() -> None:
    assert StreamModel().parallel_prefetch_workers == 1
    for count in (1, 2, 4):
        assert (
            StreamModel(parallel_prefetch_workers=count).parallel_prefetch_workers
            == count
        )
    for count in (0, 3, 8):
        with pytest.raises(ValidationError):
            StreamModel(parallel_prefetch_workers=count)


def test_claims_wake_on_release_and_allow_reacquisition() -> None:
    async def run() -> None:
        claims = _ProducerClaims()
        acquired, first = claims.acquire(42)
        assert acquired
        acquired, other = claims.acquire(42)
        assert not acquired and other is first
        claims.release(42, first)
        await other.wait()
        acquired, replacement = claims.acquire(42)
        assert acquired and replacement is not first
        claims.release(42, first)  # stale release cannot dislodge new owner
        assert claims.acquire(42) == (False, replacement)
        claims.wake_all()
        await replacement.wait()
        assert claims.acquire(42)[0]

    trio.run(run)


def test_speculative_saturation_never_heals_pool() -> None:
    class SaturatedPool:
        def admit(self, kind: str, *, workload: str):
            assert kind == "body" and workload == "background"
            raise httpx.PoolTimeout("saturated")

    async def run() -> None:
        stream = MediaStream.__new__(MediaStream)
        stream._producer_claims = _ProducerClaims()
        stream._http_pool = SaturatedPool()  # type: ignore[assignment]
        stream._delivery_registry = SimpleNamespace(get_pending_starts=lambda: [])  # type: ignore[assignment]
        stream.is_killed = SimpleNamespace(value=False)  # type: ignore[assignment]
        stream.adaptive_prefetch = SimpleNamespace(current_generation=0)  # type: ignore[assignment]
        chunk = SimpleNamespace(
            start=4, end=7, size=4, is_cached=SimpleNamespace(value=False)
        )
        await stream._fetch_speculative_chunk(chunk, 0)  # type: ignore[arg-type]
        assert stream._producer_claims._owners == {}

    trio.run(run)


def test_speculative_invalid_range_releases_claim_without_cache_publish() -> None:
    class Context:
        async def __aenter__(self):
            return self.response

        async def __aexit__(self, *_):
            return False

    class Pool:
        def admit(self, kind, *, workload):
            assert kind == "body" and workload == "background"
            return self.context

        def acquire_lease(self, *, use_proxy):
            return SimpleNamespace(client=self.client)

        release_lease = AsyncMock()

    async def run() -> None:
        stream = MediaStream.__new__(MediaStream)
        stream._producer_claims = _ProducerClaims()
        stream._delivery_registry = SimpleNamespace(get_pending_starts=lambda: [])  # type: ignore[assignment]
        stream.is_killed = SimpleNamespace(value=False)  # type: ignore[assignment]
        stream.adaptive_prefetch = SimpleNamespace(current_generation=0)  # type: ignore[assignment]
        stream._use_proxy_client = False
        stream._speculative_active = 0
        stream._speculative_peak = 0
        stream._speculative_failures = 0
        stream.session_statistics = SessionStatistics()
        stream._cache_chunk = AsyncMock()  # type: ignore[method-assign]
        stream.build_log_message = lambda text: text  # type: ignore[method-assign]
        stream.target_url = SimpleNamespace(value="https://example.test/video")  # type: ignore[assignment]
        stream.file_metadata = SimpleNamespace(file_size=8)  # type: ignore[assignment]
        stream.provider = "test"
        pool = Pool()
        pool.context = Context()
        pool.context.response = None
        pool.client = SimpleNamespace(stream=lambda *args, **kwargs: response_context)
        stream._http_pool = pool  # type: ignore[assignment]
        response_context = Context()
        response_context.response = httpx.Response(
            206,
            headers={"Content-Range": "bytes 0-3/8"},
            request=httpx.Request("GET", "https://example.test/video"),
        )
        chunk = SimpleNamespace(
            start=4, end=7, size=4, is_cached=SimpleNamespace(value=False)
        )
        await stream._fetch_speculative_chunk(chunk, 0)  # type: ignore[arg-type]
        assert stream._speculative_failures == 1
        assert stream._producer_claims._owners == {}
        stream._cache_chunk.assert_not_awaited()
        pool.release_lease.assert_awaited_once()

    trio.run(run)


@pytest.mark.parametrize(
    "payload,content_range,content_length,valid",
    [
        (b"abcd", "bytes 4-7/8", "4", True),
        (b"abc", "bytes 4-7/8", None, False),
        (b"abcde", "bytes 4-7/8", None, False),
        (b"abcd", "bytes 4-7/8", "5", False),
        (b"abcd", "bytes 4-7/9", "4", False),
    ],
)
@pytest.mark.parametrize("trickle", [False, True])
def test_speculative_exact_range_and_payload_integrity(
    payload: bytes,
    content_range: str,
    content_length: str | None,
    valid: bool,
    trickle: bool,
) -> None:
    from contextlib import asynccontextmanager

    from trio.testing import MockClock

    from program.services.streaming.cache import CachePutResult
    from program.services.streaming.session_statistics import SessionStatistics

    class Response:
        headers = {"Content-Range": content_range}
        status_code = 206

        def raise_for_status(self):
            return None

        async def aiter_raw(self):
            if trickle:
                for byte in payload:
                    await trio.sleep(0.6)
                    yield bytes([byte])
                return
            yield payload

    if content_length is not None:
        Response.headers = {**Response.headers, "Content-Length": content_length}

    class Pool:
        def __init__(self):
            self.headers = None
            self.released = 0

        @asynccontextmanager
        async def admit(self, kind, *, workload):
            assert (kind, workload) == ("body", "background")
            yield

        def acquire_lease(self, *, use_proxy):
            assert not use_proxy
            return SimpleNamespace(client=SimpleNamespace(stream=self.stream))

        @asynccontextmanager
        async def stream(self, method, url, *, headers, timeout):
            self.headers = headers
            assert timeout.connect == 17
            assert timeout.read == 30
            yield Response()

        async def release_lease(self, lease):
            self.released += 1

    async def run():
        stream = MediaStream.__new__(MediaStream)
        stream.config = SimpleNamespace(
            chunk_wait_timeout_seconds=1, connect_timeout_seconds=17
        )
        stream._producer_claims = _ProducerClaims()
        stream._delivery_registry = SimpleNamespace(
            get_pending_starts=lambda: [], has_pending=lambda _: False
        )
        stream.is_killed = SimpleNamespace(value=False)
        stream.adaptive_prefetch = SimpleNamespace(
            current_generation=0, record_provider_fetch=MagicMock()
        )
        stream._use_proxy_client = False
        stream._speculative_active = 0
        stream._speculative_peak = 0
        stream._speculative_bytes = 0
        stream._speculative_failures = 0
        stream.session_statistics = SessionStatistics()
        stream._cache_chunk = AsyncMock(return_value=CachePutResult.STORED_HOT)
        stream.build_log_message = lambda text: text
        stream.target_url = SimpleNamespace(value="https://example.test/video")
        stream.file_metadata = SimpleNamespace(file_size=8)
        stream.provider = "test"
        pool = Pool()
        stream._http_pool = pool
        chunk = SimpleNamespace(
            start=4,
            end=7,
            size=4,
            is_cached=SimpleNamespace(value=False),
            emit_cache_signal=MagicMock(),
        )
        await stream._fetch_speculative_chunk(chunk, 0)
        assert pool.headers == {"Accept-Encoding": "identity", "Range": "bytes=4-7"}
        assert pool.released == 1
        assert stream._producer_claims._owners == {}
        assert stream._speculative_active == 0
        assert stream._speculative_failures == (0 if valid and not trickle else 1)
        if valid and not trickle:
            stream._cache_chunk.assert_awaited_once_with(
                start=4, data=b"abcd", admission="prefetch"
            )
            chunk.emit_cache_signal.assert_called_once()
            assert (
                stream._speculative_bytes
                == stream.session_statistics.upstream_bytes
                == 4
            )
        else:
            stream._cache_chunk.assert_not_awaited()
            chunk.emit_cache_signal.assert_not_called()
            # If the response headers failed validation before reading the body,
            # zero wire bytes were transferred. If headers passed but body had invalid length,
            # wire bytes were transferred.
            expected_bytes = (
                len(payload)
                if (content_length is None and content_range == "bytes 4-7/8")
                else 0
            )
            if trickle:
                expected_bytes = int(valid or expected_bytes > 0)
            assert stream.session_statistics.upstream_bytes == expected_bytes
            assert stream.session_statistics.speculative_bytes == expected_bytes
            assert stream._speculative_bytes == 0

    trio.run(run, clock=MockClock(autojump_threshold=0))


def test_parallel_batch_bounds_workers_and_waits_for_cancellation() -> None:
    async def run():
        stream = MediaStream.__new__(MediaStream)
        stream.parallel_prefetch_workers = 2
        stream._speculative_scope = None
        stream._speculative_done = None
        stream.is_killed = SimpleNamespace(value=False)
        stream.adaptive_prefetch = SimpleNamespace(current_generation=0)
        entered = trio.Event()
        released = trio.Event()
        observed = []

        async def fetch(chunk, generation):
            observed.append(chunk)
            entered.set()
            try:
                await trio.sleep_forever()
            finally:
                released.set()

        async def foreground(chunks, *, is_prefetch, prefetch_gen):
            assert is_prefetch and prefetch_gen == 0 and list(chunks) == [0]
            await entered.wait()
            await released.wait()

        async def cancel():
            await entered.wait()
            stream._speculative_scope.cancel()

        stream._fetch_speculative_chunk = fetch
        async with trio.open_nursery() as nursery:
            nursery.start_soon(cancel)
            await stream._parallel_prefetch([0, 1, 2, 3], 0, foreground)
        assert observed == [1]
        assert stream._speculative_done is None
        assert stream._speculative_scope is None

    trio.run(run)


class _TestChunk(SimpleNamespace):
    __hash__ = object.__hash__


class _Harness:
    """Real run/claim/publication code; controlled transport and cache boundaries."""

    def __init__(self, workers=4):
        import trio_util
        from ordered_set import OrderedSet

        from program.services.streaming.media_stream import _DeliveryRegistry
        from program.services.streaming.session_statistics import SessionStatistics

        self.stream = stream = MediaStream.__new__(MediaStream)
        self.chunks = [
            _TestChunk(
                start=i * 4,
                end=i * 4 + 3,
                size=4,
                index=i,
                is_cached=trio_util.AsyncBool(False),
            )
            for i in range(8)
        ]
        for chunk in self.chunks:
            chunk.emit_cache_signal = lambda c=chunk: setattr(
                c.is_cached, "value", True
            )
        self.gates = [trio.Event() for _ in self.chunks]
        self.entered = [trio.Event() for _ in self.chunks]
        self.published = [trio.Event() for _ in self.chunks]
        self.requests = []
        self.active = 0
        self.peak = 0
        self.leases = 0
        self.fail = set()
        self.stored = {}
        self.publication_order = []
        self.admission_gate = None
        self.admission_entered = trio.Event()
        self.background_permits = 0
        self.cache_result = None
        stream.parallel_prefetch_workers = workers
        stream._producer_claims = _ProducerClaims()
        stream._delivery_registry = _DeliveryRegistry()
        stream._speculative_active = stream._speculative_peak = 0
        stream._speculative_bytes = stream._speculative_failures = 0
        stream._speculative_scope = stream._speculative_done = None
        stream._use_proxy_client = False
        stream.is_killed = trio_util.AsyncBool(False)
        stream.is_streaming = trio_util.AsyncBool(False)
        stream.target_url = trio_util.AsyncValue("https://example.test/video")
        stream._stream_error = trio_util.AsyncValue(None)
        stream._active_stream_connection = None
        stream._inflight_chunk = None
        stream._consumer_end = 3
        stream.enable_tracing = False
        stream.config = SimpleNamespace(
            prefetch_chunks=workers,
            chunk_size=4,
            header_size=0,
            chunk_wait_timeout_seconds=10,
        )
        stream.file_metadata = SimpleNamespace(
            file_size=32, original_filename="d107", path="d107"
        )
        stream.stream_id = "d107:1"
        stream.provider = "test"
        stream.session_statistics = SessionStatistics()
        stream.adaptive_prefetch = SimpleNamespace(
            current_generation=0,
            should_abort_prefetch=lambda g: (
                g != stream.adaptive_prefetch.current_generation
            ),
            record_provider_fetch=MagicMock(),
            calculate_window=lambda **_: workers,
            should_prefetch_now=lambda: True,
            get_effective_bitrate=lambda: 1,
        )
        stream._update_runway_observation = lambda _: None
        stream.build_log_message = lambda text: text
        stream.recent_reads = SimpleNamespace(current_read=trio_util.AsyncValue(None))
        stream.chunker = SimpleNamespace(
            get_chunk_range=self.chunk_range,
            get_prefetch_uncached=lambda **_: OrderedSet(
                c for c in self.chunks[1 : workers + 1] if not c.is_cached.value
            ),
        )
        stream._http_pool = self
        stream.manage_connection = self.connection
        stream._cache_chunk = self.cache
        self.chunks[0].emit_cache_signal()

    def chunk_range(self, *, position, size=1):
        from ordered_set import OrderedSet

        chunk = self.chunks[position // 4]
        return SimpleNamespace(
            position=position,
            request_range=(position, position + size - 1),
            first_chunk=chunk,
            chunks=OrderedSet([chunk]),
            uncached_chunks=(
                OrderedSet() if chunk.is_cached.value else OrderedSet([chunk])
            ),
        )

    async def cache(self, *, start, data, admission):
        from program.services.streaming.cache import CachePutResult

        self.stored[start] = data
        self.publication_order.append(start)
        self.published[start // 4].set()
        result = self.cache_result or CachePutResult.STORED_HOT
        if self.cache_result is not None:
            await self.stream._delivery_registry.publish(start, data)
        return result

    def admit(self, kind, *, workload):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def admission():
            assert (kind, workload) == ("body", "background")
            self.admission_entered.set()
            if self.admission_gate is not None:
                await self.admission_gate.wait()
            self.background_permits += 1
            try:
                yield
            finally:
                self.background_permits -= 1

        return admission()

    def acquire_lease(self, *, use_proxy):
        self.leases += 1
        return SimpleNamespace(client=SimpleNamespace(stream=self.response))

    async def release_lease(self, lease):
        await trio.lowlevel.checkpoint()
        self.leases -= 1

    def response(self, method, url, *, headers, timeout):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def response():
            start, end = map(int, headers["Range"].removeprefix("bytes=").split("-"))
            assert end == start + 3
            self.requests.append(("background", start))
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.entered[start // 4].set()

            async def raw():
                await self.gates[start // 4].wait()
                if start in self.fail:
                    raise httpx.ReadError("controlled abandonment")
                yield bytes([start]) * 4

            try:
                yield SimpleNamespace(
                    status_code=206,
                    headers={"Content-Range": f"bytes {start}-{end}/32"},
                    raise_for_status=lambda: None,
                    aiter_raw=raw,
                )
            finally:
                self.active -= 1

        return response()

    def connection(self, *, position):
        from contextlib import asynccontextmanager

        import trio_util

        @asynccontextmanager
        async def connection():
            self.active += 1
            self.peak = max(self.peak, self.active)
            conn = SimpleNamespace(
                current_read_position=position,
                start_position=position,
                seek_required=trio_util.AsyncBool(False),
                seek_range=None,
                response=SimpleNamespace(
                    request=SimpleNamespace(url=self.stream.target_url.value)
                ),
                increment_sequential_chunks=lambda: None,
            )

            def seek(*, chunk_range):
                conn.seek_range = chunk_range
                conn.seek_required.value = True

            async def raw():
                while conn.current_read_position < 32:
                    start = conn.current_read_position
                    self.requests.append(("foreground", start))
                    self.entered[start // 4].set()
                    await self.gates[start // 4].wait()
                    yield bytes([start]) * 4

            conn.seek = seek
            conn.reader = raw()
            self.stream._active_stream_connection = conn
            try:
                yield conn
            finally:
                self.active -= 1

        return connection()

    def start_read(self):
        from ordered_set import OrderedSet

        self.stream.recent_reads.current_read.value = SimpleNamespace(
            read_type="cache_hit",
            chunk_range=SimpleNamespace(
                request_range=(0, 3),
                uncached_chunks=OrderedSet(),
            ),
        )


@pytest.mark.parametrize(
    "fragment_delay,retry_after", [(3.0, 10.2), (8.0, 7.2), (2.0, None)]
)
def test_primary_retries_timed_out_chunk_at_same_offset(fragment_delay, retry_after):
    from contextlib import asynccontextmanager

    from trio.testing import MockClock

    async def run():
        h = _Harness(workers=1)
        attempted = []

        @asynccontextmanager
        async def connect(*, position):
            attempted.append((position, trio.current_time()))
            async with h.connection(position=position) as conn:
                if len(attempted) == 1:

                    async def trickle():
                        for _ in range(4):
                            await trio.sleep(fragment_delay)
                            yield b"x"

                    conn.reader = trickle()
                yield conn

        h.stream.connect = connect
        h.stream.manage_connection = MediaStream.manage_connection.__get__(h.stream)
        h.gates[1].set()
        with trio.fail_after(14):
            async with trio.open_nursery() as nursery:
                nursery.start_soon(h.stream.run, 4)
                h.stream.recent_reads.current_read.value = SimpleNamespace(
                    read_type="body_read", chunk_range=h.chunk_range(position=4)
                )
                await h.published[1].wait()
                if retry_after is None:
                    assert [position for position, _ in attempted] == [4]
                    assert h.stored[4] == b"xxxx"
                else:
                    assert [position for position, _ in attempted] == [4, 4]
                    assert attempted[1][1] - attempted[0][1] == pytest.approx(
                        retry_after
                    )
                    assert h.stored[4] == b"\x04" * 4
                await h.stream.close()

    trio.run(run, clock=MockClock(autojump_threshold=0))


@pytest.mark.parametrize("workers", [1, 2, 4])
def test_run_total_body_bound_out_of_order_and_cleanup(workers):
    async def run():
        h = _Harness(workers)
        h.start_read()
        with trio.fail_after(2):
            async with trio.open_nursery() as nursery:
                nursery.start_soon(h.stream.run, 4)
                for i in range(1, workers + 1):
                    await h.entered[i].wait()
                assert h.active == h.peak == workers
                # Complete independent ranges backwards, while sequential reader is blocked.
                for i in range(workers, 1, -1):
                    h.gates[i].set()
                    await h.published[i].wait()
                assert h.stream._active_stream_connection.current_read_position == 4
                h.gates[1].set()
                await h.published[1].wait()
                await h.stream.close()
            assert h.active == h.leases == 0
            assert h.stream._producer_claims._owners == {}
            assert h.stream._speculative_scope is None
            assert h.publication_order == [i * 4 for i in range(workers, 0, -1)]
            assert len(h.requests) == workers
            assert h.stored == {
                i * 4: bytes([i * 4]) * 4 for i in range(1, workers + 1)
            }

    trio.run(run)


def test_run_opens_parallel_response_while_cached_reader_finishes():
    from trio.testing import wait_all_tasks_blocked

    async def run():
        h = _Harness(workers=2)
        entry = await h.stream._delivery_registry.register_pending(0)
        h.start_read()
        with trio.fail_after(2):
            async with trio.open_nursery() as nursery:
                nursery.start_soon(h.stream.run, 4)
                await h.entered[1].wait()
                await wait_all_tasks_blocked()
                assert h.active == h.peak == 2
                assert ("background", 8) in h.requests
                assert h.stream._delivery_registry.get_pending_starts() == [0]
                h.gates[2].set()
                await h.published[2].wait()
                assert h.stored[8] == bytes([8]) * 4
                await h.stream._delivery_registry.release_reservation(entry)
                await h.stream.close()
            assert h.active == h.leases == 0
            assert not h.stream._producer_claims._owners

    trio.run(run)


@pytest.mark.parametrize("transient", [False, True])
@pytest.mark.parametrize("during_admission", [False, True])
def test_run_resumes_speculative_span_after_primary_serves_demand(
    transient, during_admission
):
    from ordered_set import OrderedSet
    from trio.testing import wait_all_tasks_blocked

    from program.services.streaming.cache import CachePutResult

    async def run():
        h = _Harness(workers=2)
        h.stream.adaptive_prefetch.calculate_window = lambda **_: 6
        h.stream.chunker.get_prefetch_uncached = lambda **_: OrderedSet(
            c for c in h.chunks[1:7] if not c.is_cached.value
        )
        h.start_read()
        with trio.fail_after(2):
            async with trio.open_nursery() as nursery:
                nursery.start_soon(h.stream.run, 4)
                await h.entered[1].wait()
                await h.entered[4].wait()
                if during_admission:
                    h.admission_gate = trio.Event()
                    h.admission_entered = trio.Event()
                else:
                    reservation = await h.stream._delivery_registry.register_pending(4)
                h.gates[4].set()
                await h.published[4].wait()
                if during_admission:
                    await h.admission_entered.wait()
                    reservation = await h.stream._delivery_registry.register_pending(4)
                    h.admission_gate.set()
                await wait_all_tasks_blocked()
                assert not h.entered[5].is_set()
                assert h.active == 1
                assert h.background_permits == 0

                if transient:
                    h.cache_result = CachePutResult.REFUSED_PHYSICAL_PRESSURE
                h.gates[1].set()
                await h.published[1].wait()
                await wait_all_tasks_blocked()
                assert h.entered[2].is_set()  # The primary is still filling its span.
                assert h.entered[5].is_set()  # No new read event should be needed.
                assert h.active == h.peak == 2
                await h.stream._delivery_registry.release_reservation(reservation)
                await h.stream.close()
            assert h.active == h.leases == 0
            assert not h.stream._producer_claims._owners

    trio.run(run)


def test_speculation_yields_if_demand_is_outside_its_primary_span():
    async def run():
        h = _Harness(workers=2)
        demand = await h.stream._delivery_registry.register_pending(28)
        primary_done = trio.Event()
        with trio.fail_after(2):
            await h.stream._fetch_speculative_span(
                h.chunks[4:7], 0, h.chunks[1:4], primary_done
            )
        assert not primary_done.is_set()
        assert not h.requests
        assert h.stream._delivery_registry.get_entry(28) is demand

    trio.run(run)


def test_speculation_does_not_hold_batch_if_primary_leaves_demand_unresolved():
    from trio.testing import wait_all_tasks_blocked

    async def run():
        h = _Harness(workers=2)
        demand = await h.stream._delivery_registry.register_pending(4)
        primary_done = trio.Event()
        with trio.fail_after(2):
            async with trio.open_nursery() as nursery:
                nursery.start_soon(
                    h.stream._fetch_speculative_span,
                    h.chunks[4:7],
                    0,
                    h.chunks[1:4],
                    primary_done,
                )
                await wait_all_tasks_blocked()
                primary_done.set()
        assert not h.requests
        assert h.stream._delivery_registry.get_entry(4) is demand

    trio.run(run)


def test_pending_demand_after_pool_wait_prevents_speculative_open():
    async def run():
        h = _Harness()
        h.admission_gate = trio.Event()
        h.gates[2].set()
        with trio.fail_after(2):
            async with trio.open_nursery() as nursery:
                nursery.start_soon(h.stream._fetch_speculative_chunk, h.chunks[2], 0)
                await h.admission_entered.wait()
                entry = await h.stream._delivery_registry.register_pending(24)
                h.admission_gate.set()
            assert h.requests == []
            assert h.stream._delivery_registry.get_entry(24) is entry
            assert h.stream._producer_claims._owners == {}

    trio.run(run)


@pytest.mark.parametrize("wait_for_pool", [False, True])
def test_cached_foreground_reservation_does_not_suppress_prefetch(wait_for_pool):
    async def run():
        h = _Harness()
        h.gates[2].set()
        if wait_for_pool:
            h.admission_gate = trio.Event()
        with trio.fail_after(2):
            async with trio.open_nursery() as nursery:
                if wait_for_pool:
                    nursery.start_soon(
                        h.stream._fetch_speculative_span, [h.chunks[2]], 0
                    )
                    await h.admission_entered.wait()
                entry = await h.stream._delivery_registry.register_pending(4)
                h.chunks[1].emit_cache_signal()
                if h.admission_gate is not None:
                    h.admission_gate.set()
                else:
                    await h.stream._fetch_speculative_span([h.chunks[2]], 0)

            assert h.stored == {8: bytes([8]) * 4}
            assert h.stream._delivery_registry.get_entry(4) is entry
            assert h.stream._delivery_registry.get_pending_starts() == [4]
            assert h.stream._producer_claims._owners == {}

    trio.run(run)


@pytest.mark.parametrize("cancel", [False, True])
def test_abandonment_wakes_reader_without_retiring_reservation(cancel):
    from trio.testing import wait_all_tasks_blocked

    async def run():
        h = _Harness()
        chunk = h.chunks[2]
        scope = trio.CancelScope()

        async def producer():
            with scope:
                await h.stream._fetch_speculative_chunk(chunk, 0)

        with trio.fail_after(2):
            async with trio.open_nursery() as nursery:
                nursery.start_soon(producer)
                await h.entered[2].wait()
                entry = await h.stream._delivery_registry.register_pending(8)
                nursery.start_soon(h.stream._wait_one_chunk, chunk)
                await wait_all_tasks_blocked()
                if cancel:
                    scope.cancel()
                else:
                    h.fail.add(8)
                    h.gates[2].set()
                await wait_all_tasks_blocked()
                assert h.stream.recent_reads.current_read.value.read_type == "body_read"
                assert h.stream._delivery_registry.get_entry(8) is entry
                assert entry.waiter_count == 1 and not entry.ready.is_set()
                assert h.stream._producer_claims._owners == {}
                assert h.active == h.leases == 0
                chunk.emit_cache_signal()

    trio.run(run)


def test_generation_change_during_response_never_publishes():
    async def run():
        h = _Harness()
        with trio.fail_after(2):
            async with trio.open_nursery() as nursery:
                nursery.start_soon(h.stream._fetch_speculative_chunk, h.chunks[2], 0)
                await h.entered[2].wait()
                h.stream.adaptive_prefetch.current_generation += 1
                h.gates[2].set()
            assert not h.stored
            assert not h.chunks[2].is_cached.value
            assert h.active == h.leases == 0
            assert h.stream._producer_claims._owners == {}

    trio.run(run)


@pytest.mark.parametrize(
    "result_name",
    [
        "STORED_HOT",
        "STORED_WARM",
        "REFUSED_PHYSICAL_PRESSURE",
        "SKIPPED_PREFETCH_PRESSURE",
    ],
)
def test_worker_cache_first_real_publication_and_retained_reader(
    monkeypatch, result_name
):
    from kink import di
    from trio.testing import wait_all_tasks_blocked

    from program.services.streaming.cache import Cache, CachePutResult

    async def run():
        h = _Harness()
        stream = h.stream
        stream._cache_chunk = MediaStream._cache_chunk.__get__(stream)
        put_entered = trio.Event()
        allow_put = trio.Event()
        result = CachePutResult[result_name]

        async def put(**kwargs):
            assert kwargs["admission"] == "prefetch"
            assert kwargs["data"] == bytes([8]) * 4
            put_entered.set()
            await allow_put.wait()
            return result

        fake_cache = SimpleNamespace(put=put)
        original_getitem = type(di).__getitem__
        monkeypatch.setattr(
            type(di),
            "__getitem__",
            lambda self, key: (
                fake_cache if key is Cache else original_getitem(self, key)
            ),
        )
        with trio.fail_after(2):
            async with trio.open_nursery() as nursery:
                nursery.start_soon(stream._fetch_speculative_chunk, h.chunks[2], 0)
                await h.entered[2].wait()
                h.gates[2].set()
                await put_entered.wait()
                entry = await stream._delivery_registry.register_pending(8)
                nursery.start_soon(stream._wait_one_chunk, h.chunks[2])
                await wait_all_tasks_blocked()
                assert not entry.ready.is_set()
                assert not h.chunks[2].is_cached.value
                allow_put.set()
            stored = result_name.startswith("STORED")
            assert h.chunks[2].is_cached.value is stored
            assert entry.waiter_count == 1
            payload = await stream._delivery_registry.get_ready_payload(8)
            assert payload == (None if stored else bytes([8]) * 4)
            assert h.requests == [("background", 8)]
            assert stream._producer_claims._owners == {}

    trio.run(run)


@pytest.mark.parametrize("stop", ["close", "seek", "parent"])
def test_run_cancellation_drains_all_response_contexts(stop):
    from trio.testing import wait_all_tasks_blocked

    async def run():
        h = _Harness(4)
        h.start_read()
        with trio.fail_after(2):
            async with trio.open_nursery() as nursery:
                nursery.start_soon(h.stream.run, 4)
                for i in range(1, 5):
                    await h.entered[i].wait()
                if stop == "close":
                    await h.stream.close()
                elif stop == "seek":
                    h.stream.adaptive_prefetch.current_generation += 1
                    await h.stream._stop_speculative_workers()
                    await wait_all_tasks_blocked()
                    assert h.leases == 0
                    assert not h.stored
                    nursery.cancel_scope.cancel()
                else:
                    nursery.cancel_scope.cancel()
            assert h.active == h.leases == h.stream._speculative_active == 0
            assert not h.stored
            assert h.stream._producer_claims._owners == {}
            assert h.stream._speculative_done is h.stream._speculative_scope is None

    trio.run(run)


@pytest.mark.parametrize("transient", [False, True])
def test_run_foreground_handoff_does_not_refetch_completed_worker(transient):
    from ordered_set import OrderedSet
    from trio.testing import wait_all_tasks_blocked

    from program.services.streaming.cache import CachePutResult

    async def run():
        h = _Harness(2)
        h.start_read()
        with trio.fail_after(2):
            async with trio.open_nursery() as nursery:
                nursery.start_soon(h.stream.run, 4)
                await h.entered[2].wait()
                entry = await h.stream._delivery_registry.register_pending(8)
                # Real run loop receives demand while the speculative owner is active.
                h.stream.recent_reads.current_read.value = SimpleNamespace(
                    read_type="body_read",
                    chunk_range=SimpleNamespace(
                        request_range=(8, 11),
                        uncached_chunks=OrderedSet([h.chunks[2]]),
                    ),
                )
                if transient:
                    h.cache_result = CachePutResult.REFUSED_PHYSICAL_PRESSURE
                h.gates[2].set()
                await h.published[2].wait()
                await wait_all_tasks_blocked()
                h.cache_result = None
                h.gates[1].set()
                await wait_all_tasks_blocked()
                assert h.requests.count(("background", 8)) == 1
                assert ("foreground", 8) not in h.requests
                assert h.stream._delivery_registry.get_entry(8) is entry
                await h.stream.close()

    trio.run(run)


def test_run_abandoned_worker_requeues_and_foreground_delivers():
    from trio.testing import wait_all_tasks_blocked

    async def run():
        h = _Harness(2)
        h.start_read()
        with trio.fail_after(2):
            async with trio.open_nursery() as nursery:
                nursery.start_soon(h.stream.run, 4)
                await h.entered[2].wait()
                entry = await h.stream._delivery_registry.register_pending(8)
                nursery.start_soon(h.stream._wait_one_chunk, h.chunks[2])
                await wait_all_tasks_blocked()
                h.fail.add(8)
                h.gates[2].set()
                await wait_all_tasks_blocked()
                assert h.stream.recent_reads.current_read.value.read_type == "body_read"
                assert h.stream._delivery_registry.get_entry(8) is entry
                h.gates[1].set()
                await h.published[2].wait()
                await wait_all_tasks_blocked()
                assert h.chunks[2].is_cached.value
                assert h.stored[8] == bytes([8]) * 4
                assert h.requests.count(("foreground", 8)) == 1
                assert h.requests.count(("background", 8)) == 1
                assert entry.waiter_count == 1
                await h.stream.close()
            assert h.active == h.leases == 0
            assert h.stream._producer_claims._owners == {}

    trio.run(run)


@pytest.mark.parametrize("workers", [2, 4])
@pytest.mark.parametrize("primary_finishes", [False, True])
def test_stalled_speculation_does_not_block_demand_outside_batch(
    workers, primary_finishes
):
    """Unrelated playback demand must not wait for a speculative HTTP timeout."""
    from ordered_set import OrderedSet
    from trio.testing import MockClock

    async def run():
        h = _Harness(workers)
        h.start_read()
        batch_done = trio.Event()
        demand_start = (workers + 1) * 4

        async def primary(chunks, **kwargs):
            if not primary_finishes:
                await trio.sleep_forever()
            for chunk in chunks:
                chunk.emit_cache_signal()

        async def batch():
            await h.stream._parallel_prefetch(
                OrderedSet(h.chunks[1 : workers + 1]), 0, primary
            )
            batch_done.set()

        async with trio.open_nursery() as nursery:
            nursery.start_soon(batch)
            for index in range(2, workers + 1):
                await h.entered[index].wait()  # These bodies never return bytes.
            await h.stream._delivery_registry.register_pending(demand_start)
            h.stream._consumer_end = demand_start + 3
            h.stream.recent_reads.current_read.value = SimpleNamespace(
                read_type="body_read", chunk_range=h.chunk_range(position=demand_start)
            )
            with trio.move_on_after(1) as deadline:
                await batch_done.wait()
            nursery.cancel_scope.cancel()
        assert not deadline.cancelled_caught
        assert not h.stored  # Cancelled speculative bytes were not published.
        assert h.active == h.leases == h.background_permits == 0
        assert h.stream._producer_claims._owners == {}
        assert h.stream._delivery_registry.has_pending(demand_start)
        assert h.stream._speculative_scope is h.stream._speculative_done is None

    trio.run(run, clock=MockClock(autojump_threshold=0))


def test_foreground_claim_prevents_speculative_duplicate():
    async def run():
        h = _Harness()
        acquired, claim = h.stream._producer_claims.acquire(8)
        assert acquired
        await h.stream._fetch_speculative_chunk(h.chunks[2], 0)
        assert h.requests == []
        assert h.stream._producer_claims.acquire(8) == (False, claim)
        h.stream._producer_claims.release(8, claim)

    trio.run(run)


def test_interleaved_parallel_prefetch_gap_reconnect_reproduction():
    """Verify that contiguous transfer spans eliminate the cached-gap seek.

    With span partitioning, the primary connection is assigned a contiguous transfer
    span (chunks 1 and 2: starts 4 and 8), while the speculative worker is assigned
    chunks 3 and 4 (starts 12 and 16).
    Therefore, the background worker opens chunk 3 (start 12), not chunk 2 (start 8).
    Foreground completes chunks 1 and 2 sequentially without hitting an interleaved
    cached gap, so seek_required remains False.
    """
    from ordered_set import OrderedSet

    async def run():
        h = _Harness(workers=2)
        h.stream.chunker.get_prefetch_uncached = lambda **_: OrderedSet(h.chunks[1:5])
        with trio.fail_after(2):
            async with trio.open_nursery() as nursery:
                nursery.start_soon(h.stream.run, 4)
                h.start_read()
                # Foreground opens chunk 1 (start 4)
                await h.entered[1].wait()
                # Background worker opens chunk 3 (start 12) from its non-overlapping span
                await h.entered[3].wait()

                # Speculative worker completes chunk 3
                h.gates[3].set()
                await h.published[3].wait()

                # Foreground completes chunk 1 (start 4)
                h.gates[1].set()
                await h.published[1].wait()

                # Foreground proceeds directly to chunk 2 (start 8) contiguously
                await h.entered[2].wait()
                h.gates[2].set()
                await h.published[2].wait()

                conn = h.stream._active_stream_connection
                assert conn is not None
                await trio.sleep(0.05)
                # Contiguous span execution: no gap-induced seek
                assert conn.seek_required.value is False
                await h.stream.close()

    trio.run(run)


def test_consecutive_prefetch_windows_boundary_handoff():
    """Verify prefetch window boundary handoff across two consecutive windows.

    In Window 1, chunks 1..4 are partitioned into a contiguous primary span [1, 2]
    and speculative span [3, 4]. Speculative workers fetch chunks 3 and 4 concurrently
    and complete before the primary stream finishes chunk 2.
    The primary stream completes chunks 1 and 2 sequentially without an interleaved gap seek.
    When the consumer advances past Window 1, Window 2 opens, correctly repositioning the
    primary connection across the already-cached speculative chunks without duplicate downloads
    or abandoned producer claims.
    """
    from ordered_set import OrderedSet

    async def run():
        h = _Harness(workers=2)
        h.chunks = [
            _TestChunk(
                start=i * 4,
                end=i * 4 + 3,
                size=4,
                index=i,
                is_cached=h.chunks[0].is_cached.__class__(False),
            )
            for i in range(12)
        ]
        for chunk in h.chunks:
            chunk.emit_cache_signal = lambda c=chunk: setattr(
                c.is_cached, "value", True
            )
        h.gates = [trio.Event() for _ in h.chunks]
        h.entered = [trio.Event() for _ in h.chunks]
        h.published = [trio.Event() for _ in h.chunks]
        h.chunks[0].emit_cache_signal()
        h.stream.adaptive_prefetch.calculate_window = lambda **_: 4

        def get_prefetch_uncached(after_end=0, count=4, **_):
            return OrderedSet(
                c for c in h.chunks if c.start > after_end and not c.is_cached.value
            )[:count]

        h.stream.chunker.get_prefetch_uncached = get_prefetch_uncached

        with trio.fail_after(2):
            async with trio.open_nursery() as nursery:
                nursery.start_soon(h.stream.run, 4)
                h.start_read()

                # --- WINDOW 1 ---
                # Primary span: chunks 1, 2 (starts 4, 8)
                # Speculative span: chunks 3, 4 (starts 12, 16)
                await h.entered[1].wait()
                await h.entered[3].wait()

                # Speculative completes chunks 3 and 4 BEFORE primary reaches boundary
                h.gates[3].set()
                await h.published[3].wait()

                await h.entered[4].wait()
                h.gates[4].set()
                await h.published[4].wait()

                # Primary finishes chunk 1 and proceeds contiguously to chunk 2
                h.gates[1].set()
                await h.published[1].wait()

                await h.entered[2].wait()
                h.gates[2].set()
                await h.published[2].wait()

                # Primary connection completed chunks 1 and 2 contiguously without gap seek!
                conn = h.stream._active_stream_connection
                assert conn is not None
                assert conn.seek_required.value is False

                # Verify foreground and background request distribution
                assert [r for r in h.requests if r[0] == "foreground"] == [
                    ("foreground", 4),
                    ("foreground", 8),
                ]
                assert [r for r in h.requests if r[0] == "background"] == [
                    ("background", 12),
                    ("background", 16),
                ]

                # All 4 chunks of Window 1 are now cached
                for idx in [1, 2, 3, 4]:
                    assert h.chunks[idx].is_cached.value is True

                # Zero abandoned producer claims
                assert len(h.stream._producer_claims._owners) == 0

                # --- WINDOW 2 TRANSITION ---
                # Consumer playhead advances through Window 1 (playhead now at end of chunk 4: byte 19)
                h.stream._consumer_end = 19
                h.stream.recent_reads.current_read.value = SimpleNamespace(
                    read_type="cache_hit",
                    chunk_range=SimpleNamespace(
                        request_range=(16, 19),
                        uncached_chunks=OrderedSet(),
                        position=16,
                    ),
                )
                await trio.sleep(0.02)

                # Primary wire position was at byte 12 (end of chunk 2). Speculative span (12..19)
                # is already cached. The stream repositions across the cached prefetch span
                # to the next uncached chunks without stalls or duplicate downloads.
                assert conn.seek_required.value is True
                assert conn.seek_range.position in (20, 24)

                await h.stream.close()

    trio.run(run)


def test_parallel_prefetch_reopens_at_first_gap_when_a_chunk_becomes_cached():
    """A late cache hit must not move the reconnect past earlier missing bytes."""
    from contextlib import asynccontextmanager

    from ordered_set import OrderedSet

    from program.services.streaming.stream_connection import StreamConnection

    async def run():
        h = _Harness(workers=2)
        h.stream.adaptive_prefetch.calculate_window = lambda **_: 7
        h.stream.chunker.get_prefetch_uncached = lambda **_: OrderedSet(
            chunk for chunk in h.chunks[1:] if not chunk.is_cached.value
        )
        opened = []
        reopened = trio.Event()

        @asynccontextmanager
        async def connection(*, position):
            async with h.connection(position=position) as conn:
                # Use production seek behavior, including its immediate position update.
                conn.seek = StreamConnection.seek.__get__(conn)
                opened.append(position)
                if len(opened) == 2:
                    reopened.set()
                yield conn

        h.stream.manage_connection = connection
        with trio.fail_after(2):
            async with trio.open_nursery() as nursery:
                nursery.start_soon(h.stream.run, 4)
                h.start_read()
                await h.entered[1].wait()
                # Chunk 2 was included in the original primary span. Another
                # handle caches it while chunk 1 is still being transferred.
                h.chunks[2].emit_cache_signal()
                h.gates[1].set()
                await reopened.wait()
                actual = opened[:2]
                if actual == [4, 12]:
                    h.gates[3].set()
                    await h.published[3].wait()
                await h.stream.close()

        assert actual == [4, 12]
        assert h.stored[12] == bytes([12]) * 4
        assert h.stream._producer_claims._owners == {}

    trio.run(run)


def test_interrupted_primary_cache_put_reconnects_instead_of_reusing_misaligned_socket():
    """Cancellation during _cache_chunk after consuming socket bytes must reconnect the primary response."""
    from contextlib import asynccontextmanager

    from ordered_set import OrderedSet
    from trio.testing import wait_all_tasks_blocked

    from program.services.streaming.stream_connection import StreamConnection

    async def run():
        h = _Harness(workers=2)
        h.stream.adaptive_prefetch.calculate_window = lambda **_: 3
        h.stream.chunker.get_prefetch_uncached = lambda **_: OrderedSet(h.chunks[1:4])

        wire_positions: list[int] = []
        cache_entered = trio.Event()
        allow_cache = trio.Event()
        reconnect_opened = trio.Event()

        @asynccontextmanager
        async def connection(*, position):
            conn_idx = len(wire_positions)
            wire_positions.append(position)

            async def raw():
                cursor = position
                while cursor < 32:
                    yield bytes([cursor]) * 4
                    cursor += 4

            import trio_util

            conn = SimpleNamespace(
                current_read_position=position,
                start_position=position,
                seek_required=trio_util.AsyncBool(False),
                seek_range=None,
                response=SimpleNamespace(
                    request=SimpleNamespace(url=h.stream.target_url.value)
                ),
                increment_sequential_chunks=lambda: None,
                reader=raw(),
            )
            conn.seek = StreamConnection.seek.__get__(conn)
            h.stream._active_stream_connection = conn
            if conn_idx == 1:
                reconnect_opened.set()
            yield conn

        async def blocking_cache(*, start, data, admission):
            if start == 4 and not cache_entered.is_set():
                cache_entered.set()
                await allow_cache.wait()
            return await h.cache(start=start, data=data, admission=admission)

        h.stream.manage_connection = connection
        h.stream._cache_chunk = blocking_cache

        with trio.fail_after(2):
            async with trio.open_nursery() as nursery:
                nursery.start_soon(h.stream.run, 4)
                h.start_read()
                # Primary read chunk 1 (offset 4) off the HTTP stream and entered _cache_chunk
                await cache_entered.wait()
                # Unowned demand at offset 20 arrives outside the prefetch batch [4, 8, 12]
                await h.stream._delivery_registry.register_pending(20)
                h.stream.recent_reads.current_read.value = SimpleNamespace(
                    read_type="body_read",
                    chunk_range=h.chunk_range(position=20),
                )
                # Primary must invalidate its consumed HTTP stream and reconnect cleanly at 20
                await reconnect_opened.wait()
                allow_cache.set()
                await wait_all_tasks_blocked()
                await h.published[5].wait()
                await h.stream.close()

        assert wire_positions[0] == 4
        assert wire_positions[1] == 20
        assert h.stored[20] == bytes([20]) * 4
        # Chunk 4 was never overwritten by shifted chunk-8 bytes from the first connection
        assert 4 not in h.stored or h.stored[4] == bytes([4]) * 4

    trio.run(run)


def test_coalesced_speculative_span_streams_multi_chunk_range_and_publishes_incrementally():
    """Real multi-block chunks coalesce up to 4 MiB into one Range GET and publish per chunk."""
    from contextlib import asynccontextmanager

    import trio_util

    from program.services.streaming.cache import CachePutResult

    chunk_size = 1024 * 1024
    total_size = 4 * chunk_size
    chunks = [
        _TestChunk(
            start=i * chunk_size,
            end=(i + 1) * chunk_size - 1,
            size=chunk_size,
            index=i,
            is_cached=trio_util.AsyncBool(False),
        )
        for i in range(4)
    ]
    for c in chunks:
        c.emit_cache_signal = lambda ch=c: setattr(ch.is_cached, "value", True)

    observed_ranges: list[str] = []
    published_starts: list[int] = []
    released_leases = 0

    class Pool:
        @asynccontextmanager
        async def admit(self, kind, *, workload):
            assert (kind, workload) == ("body", "background")
            yield

        def acquire_lease(self, *, use_proxy):
            return SimpleNamespace(client=SimpleNamespace(stream=self.stream))

        @asynccontextmanager
        async def stream(self, method, url, *, headers, timeout):
            observed_ranges.append(headers["Range"])

            async def aiter_raw():
                for idx in range(4):
                    # Deliver in two 512 KiB fragments so incremental slicing is exercised
                    byte_val = bytes([65 + idx])
                    yield byte_val * (chunk_size // 2)
                    yield byte_val * (chunk_size // 2)

            yield SimpleNamespace(
                status_code=206,
                headers={
                    "Content-Range": f"bytes 0-{total_size - 1}/{total_size}",
                    "Content-Length": str(total_size),
                },
                raise_for_status=lambda: None,
                aiter_raw=aiter_raw,
            )

        async def release_lease(self, lease):
            nonlocal released_leases
            released_leases += 1

    async def run():
        stream = MediaStream.__new__(MediaStream)
        stream.config = SimpleNamespace(
            chunk_size=chunk_size,
            block_size=128 * 1024,
            chunk_wait_timeout_seconds=10,
            connect_timeout_seconds=10,
        )
        stream._producer_claims = _ProducerClaims()
        stream._delivery_registry = SimpleNamespace(
            get_pending_starts=lambda: [], has_pending=lambda _: False
        )
        stream.is_killed = trio_util.AsyncBool(False)
        stream.adaptive_prefetch = SimpleNamespace(
            current_generation=0, record_provider_fetch=MagicMock()
        )
        stream._use_proxy_client = False
        stream._speculative_active = 0
        stream._speculative_peak = 0
        stream._speculative_bytes = 0
        stream._speculative_failures = 0
        stream._speculative_inflight_chunks = set()
        stream._batch_spec_starts = {c.start for c in chunks}
        stream.session_statistics = SessionStatistics()
        stream.build_log_message = lambda text: text
        stream.target_url = SimpleNamespace(value="https://example.test/video")
        stream.file_metadata = SimpleNamespace(file_size=total_size)
        stream.provider = "test"
        stream._http_pool = Pool()

        async def fake_cache(*, start, data, admission):
            assert admission == "prefetch"
            published_starts.append(start)
            assert stream._producer_claims.is_claimed(start)
            return CachePutResult.STORED_HOT

        stream._cache_chunk = fake_cache
        await stream._fetch_speculative_span(chunks, 0)

        assert observed_ranges == [f"bytes=0-{total_size - 1}"]
        assert published_starts == [c.start for c in chunks]
        assert all(c.is_cached.value for c in chunks)
        assert stream._speculative_bytes == total_size
        assert stream.session_statistics.upstream_bytes == total_size
        assert stream._producer_claims.active_starts() == set()
        assert released_leases == 1
        assert stream._speculative_active == 0

    trio.run(run)


def test_coalesced_speculative_span_midstream_error_preserves_committed_and_cleans_claims():
    """Mid-span transport error keeps earlier committed chunks and releases all claims/leases."""
    from contextlib import asynccontextmanager

    import trio_util

    from program.services.streaming.cache import CachePutResult

    chunk_size = 1024 * 1024
    total_size = 4 * chunk_size
    chunks = [
        _TestChunk(
            start=i * chunk_size,
            end=(i + 1) * chunk_size - 1,
            size=chunk_size,
            index=i,
            is_cached=trio_util.AsyncBool(False),
        )
        for i in range(4)
    ]
    for c in chunks:
        c.emit_cache_signal = lambda ch=c: setattr(ch.is_cached, "value", True)

    released_leases = 0

    class Pool:
        @asynccontextmanager
        async def admit(self, kind, *, workload):
            yield

        def acquire_lease(self, *, use_proxy):
            return SimpleNamespace(client=SimpleNamespace(stream=self.stream))

        @asynccontextmanager
        async def stream(self, method, url, *, headers, timeout):
            async def aiter_raw():
                yield b"A" * chunk_size
                yield b"B" * (chunk_size // 2)
                raise httpx.ReadError("mid-span disconnect")

            yield SimpleNamespace(
                status_code=206,
                headers={
                    "Content-Range": f"bytes 0-{total_size - 1}/{total_size}",
                    "Content-Length": str(total_size),
                },
                raise_for_status=lambda: None,
                aiter_raw=aiter_raw,
            )

        async def release_lease(self, lease):
            nonlocal released_leases
            released_leases += 1

    async def run():
        stream = MediaStream.__new__(MediaStream)
        stream.config = SimpleNamespace(
            chunk_size=chunk_size,
            block_size=128 * 1024,
            chunk_wait_timeout_seconds=10,
            connect_timeout_seconds=10,
        )
        stream._producer_claims = _ProducerClaims()
        stream._delivery_registry = SimpleNamespace(
            get_pending_starts=lambda: [], has_pending=lambda _: False
        )
        stream.is_killed = trio_util.AsyncBool(False)
        stream.adaptive_prefetch = SimpleNamespace(
            current_generation=0, record_provider_fetch=MagicMock()
        )
        stream._use_proxy_client = False
        stream._speculative_active = 0
        stream._speculative_peak = 0
        stream._speculative_bytes = 0
        stream._speculative_failures = 0
        stream._speculative_inflight_chunks = set()
        stream._batch_spec_starts = {c.start for c in chunks}
        stream.session_statistics = SessionStatistics()
        stream.build_log_message = lambda text: text
        stream.target_url = SimpleNamespace(value="https://example.test/video")
        stream.file_metadata = SimpleNamespace(file_size=total_size)
        stream.provider = "test"
        stream._http_pool = Pool()
        stream._cache_chunk = AsyncMock(return_value=CachePutResult.STORED_HOT)

        await stream._fetch_speculative_span(chunks, 0)

        assert chunks[0].is_cached.value is True
        assert all(not c.is_cached.value for c in chunks[1:])
        assert stream._speculative_failures == 1
        assert stream._producer_claims.active_starts() == set()
        assert released_leases == 1
        assert stream._speculative_active == 0

    trio.run(run)


def test_sequential_playback_into_spec_span_does_not_abort_sibling_workers():
    """Foreground demand arriving on spec_spans[0] must not abort active sibling workers."""
    from trio.testing import wait_all_tasks_blocked

    async def run():
        h = _Harness(workers=4)
        h.stream.adaptive_prefetch.calculate_window = lambda **_: 4
        h.start_read()
        with trio.fail_after(2):
            async with trio.open_nursery() as nursery:
                nursery.start_soon(h.stream.run, 4)
                for idx in range(1, 5):
                    await h.entered[idx].wait()

                # Reader advances straight into chunk 2 (owned by speculative worker 1)
                reservation = await h.stream._delivery_registry.register_pending(8)
                h.stream.recent_reads.current_read.value = SimpleNamespace(
                    read_type="body_read", chunk_range=h.chunk_range(position=8)
                )
                await wait_all_tasks_blocked()

                # Sibling speculative workers 2 and 3 must NOT abort!
                h.gates[3].set()
                h.gates[4].set()
                h.gates[2].set()
                h.gates[1].set()
                for idx in range(1, 5):
                    await h.published[idx].wait()
                await h.stream._delivery_registry.release_reservation(reservation)
                await h.stream.close()

        assert h.requests.count(("background", 8)) == 1
        assert h.requests.count(("background", 12)) == 1
        assert h.requests.count(("background", 16)) == 1
        assert h.requests.count(("foreground", 8)) == 0
        assert h.stored == {i * 4: bytes([i * 4]) * 4 for i in range(1, 5)}

    trio.run(run)


def test_parallel_prefetch_dynamic_tail_work_stealing():
    """Fast speculative worker must steal trailing unclaimed chunks from a slower peer."""
    from collections import deque
    from functools import partial

    from trio.testing import wait_all_tasks_blocked

    async def run():
        h = _Harness(workers=3)
        h.stream._batch_spec_starts = {c.start for c in h.chunks[3:8]}
        primary_done = trio.Event()
        primary_done.set()
        work_queues = [
            deque([h.chunks[3]]),
            deque([h.chunks[4], h.chunks[5], h.chunks[6], h.chunks[7]]),
        ]

        # Allow worker 0's chunk 3 and stolen chunks 6/7, while blocking chunk 4 indefinitely
        h.gates[3].set()
        h.gates[6].set()
        h.gates[7].set()

        with trio.fail_after(2):
            async with trio.open_nursery() as nursery:
                nursery.start_soon(
                    partial(
                        h.stream._fetch_speculative_span,
                        list(work_queues[0]),
                        0,
                        h.chunks[1:3],
                        primary_done,
                        work_queues=work_queues,
                        queue_index=0,
                    )
                )
                nursery.start_soon(
                    partial(
                        h.stream._fetch_speculative_span,
                        list(work_queues[1]),
                        0,
                        h.chunks[1:3],
                        primary_done,
                        work_queues=work_queues,
                        queue_index=1,
                    )
                )
                # Worker 0 must finish chunk 3 and steal chunk 7 (then 6) while worker 1 blocks on 4
                await h.published[3].wait()
                await h.published[7].wait()
                assert not h.published[4].is_set()
                h.gates[4].set()
                h.gates[5].set()
                await wait_all_tasks_blocked()

        for idx in range(3, 8):
            assert h.chunks[idx].is_cached.value
            assert h.requests.count(("background", idx * 4)) == 1

    trio.run(run)


def test_tail_refill_hysteresis_avoids_single_chunk_reconnect():
    """With >50% cached runway ahead, W=4 must skip 1-chunk tail seeks until runway drops below threshold."""
    from ordered_set import OrderedSet

    async def run():
        h = _Harness(workers=4)
        h.chunks = [
            _TestChunk(
                start=i * 4,
                end=i * 4 + 3,
                size=4,
                index=i,
                is_cached=h.chunks[0].is_cached.__class__(i < 17),
            )
            for i in range(28)
        ]
        for c in h.chunks:
            c.emit_cache_signal = lambda ch=c: setattr(ch.is_cached, "value", True)
        h.gates = [trio.Event() for _ in h.chunks]
        h.entered = [trio.Event() for _ in h.chunks]
        h.published = [trio.Event() for _ in h.chunks]
        h.stream.file_metadata = SimpleNamespace(
            file_size=len(h.chunks) * 4, original_filename="d107", path="d107"
        )
        h.stream.adaptive_prefetch.calculate_window = lambda **_: 16

        def get_prefetch_uncached(after_end=0, count=16, **_):
            return OrderedSet(
                c
                for c in h.chunks
                if after_end < c.start <= after_end + count * 4
                and not c.is_cached.value
            )[:count]

        h.stream.chunker.get_prefetch_uncached = get_prefetch_uncached
        h.stream.chunker.footer_start = len(h.chunks) * 4

        with trio.fail_after(2):
            async with trio.open_nursery() as nursery:
                # Primary stopped at offset 16 (chunk 4); speculative cached 5..16 already.
                nursery.start_soon(h.stream.run, 16)
                # Consumer reads chunk 5: 11 cached chunks remain ahead, ahead=[17] (len=1 < 8)
                h.stream._consumer_end = 23
                h.stream.recent_reads.current_read.value = SimpleNamespace(
                    read_type="cache_hit",
                    chunk_range=SimpleNamespace(
                        request_range=(20, 23),
                        uncached_chunks=OrderedSet(),
                        position=20,
                    ),
                )
                await trio.sleep(0.02)
                conn = h.stream._active_stream_connection
                assert conn is not None
                assert conn.seek_required.value is False

                # Consumer advances to chunk 11: only 5 cached chunks left (<8), ahead=[17..24] (len=8>=8)
                h.stream._consumer_end = 47
                h.stream.recent_reads.current_read.value = SimpleNamespace(
                    read_type="cache_hit",
                    chunk_range=SimpleNamespace(
                        request_range=(44, 47),
                        uncached_chunks=OrderedSet(),
                        position=44,
                    ),
                )
                await trio.sleep(0.02)
                assert conn.seek_required.value is True
                assert conn.seek_range.position == 17 * 4
                await h.stream.close()

    trio.run(run)


def test_abandoned_speculative_worker_wakes_unowned_demand_after_primary_done():
    """Abandoned speculative chunk must wake unowned demand barrier once primary finishes."""
    from ordered_set import OrderedSet
    from trio.testing import MockClock

    async def run():
        h = _Harness(workers=3)
        h.start_read()
        batch_done = trio.Event()

        async def primary(chunks, **kwargs):
            for chunk in chunks:
                h.stored[chunk.start] = bytes([chunk.start]) * 4
                chunk.emit_cache_signal()

        async def batch():
            # primary=[chunk 1 (4)], spec=[[chunk 2 (8)], [chunk 3 (12)]]
            await h.stream._parallel_prefetch(OrderedSet(h.chunks[1:4]), 0, primary)
            batch_done.set()

        async with trio.open_nursery() as nursery:
            nursery.start_soon(batch)
            # Worker 1 starts chunk 2 (8), Worker 2 stalls indefinitely on chunk 3 (12)
            await h.entered[2].wait()
            await h.entered[3].wait()

            # Foreground arrives needing chunk 2 (8) right as Worker 1 fails on chunk 2
            await h.stream._delivery_registry.register_pending(8)
            h.stream.recent_reads.current_read.value = SimpleNamespace(
                read_type="body_read", chunk_range=h.chunk_range(position=8)
            )
            h.fail.add(8)
            h.gates[2].set()

            with trio.move_on_after(1) as deadline:
                await batch_done.wait()
            nursery.cancel_scope.cancel()

        assert not deadline.cancelled_caught
        assert h.requests.count(("background", 8)) == 1
        assert h.stream._producer_claims.active_starts() == set()

    trio.run(run, clock=MockClock(autojump_threshold=0))


def test_tail_refill_short_cached_gap_does_not_misaligned_seek():
    """Short <=2 chunk gap already cached by spec workers uses hysteresis instead of misaligned prefetch."""
    from ordered_set import OrderedSet

    async def run():
        h = _Harness(workers=4)
        h.chunks = [
            _TestChunk(
                start=i * 4,
                end=i * 4 + 3,
                size=4,
                index=i,
                is_cached=h.chunks[0].is_cached.__class__(i < 17),
            )
            for i in range(28)
        ]
        for c in h.chunks:
            c.emit_cache_signal = lambda ch=c: setattr(ch.is_cached, "value", True)
        h.gates = [trio.Event() for _ in h.chunks]
        h.entered = [trio.Event() for _ in h.chunks]
        h.published = [trio.Event() for _ in h.chunks]
        h.stream.file_metadata = SimpleNamespace(
            file_size=len(h.chunks) * 4,
            original_filename="d107-short-gap",
            path="d107-short-gap",
        )
        h.stream.adaptive_prefetch.calculate_window = lambda **_: 16

        def get_prefetch_uncached(after_end=0, count=16, **_):
            return OrderedSet(
                c
                for c in h.chunks
                if after_end < c.start <= after_end + count * 4
                and not c.is_cached.value
            )[:count]

        h.stream.chunker.get_prefetch_uncached = get_prefetch_uncached
        h.stream.chunker.footer_start = len(h.chunks) * 4

        prefetch_calls = 0

        async def counting_parallel_prefetch(chunks, generation, process_chunks):
            nonlocal prefetch_calls
            prefetch_calls += 1

        h.stream._parallel_prefetch = counting_parallel_prefetch

        with trio.fail_after(2):
            async with trio.open_nursery() as nursery:
                # Connection stopped at offset 60 (chunk 15); 15 & 16 cached, first=17 at offset 68 (gap=2 chunks)
                nursery.start_soon(h.stream.run, 60)
                # Consumer reads chunk 5 -> 11 cached chunks ahead (>= 8), ahead=[17] (len=1 < 8)
                h.stream._consumer_end = 23
                h.stream.recent_reads.current_read.value = SimpleNamespace(
                    read_type="cache_hit",
                    chunk_range=SimpleNamespace(
                        request_range=(20, 23),
                        uncached_chunks=OrderedSet(),
                        position=20,
                    ),
                )
                await trio.sleep(0.02)
                conn = h.stream._active_stream_connection
                assert conn is not None
                assert conn.seek_required.value is False
                assert prefetch_calls == 0
                await h.stream.close()

    trio.run(run)


def test_large_window_multi_worker_wave_bounds_primary_span():
    """Large high-bitrate prefetch windows must dispatch bounded multi-worker waves rather than starving near-head chunks on one primary connection."""
    from ordered_set import OrderedSet

    async def run():
        chunk_sz = 1024 * 1024
        h = _Harness(workers=4)
        h.stream.config = SimpleNamespace(
            chunk_size=chunk_sz,
            block_size=128 * 1024,
            header_size=256 * 1024,
            activity_timeout_seconds=10,
            chunk_wait_timeout_seconds=10,
            connect_timeout_seconds=10,
            prefetch_chunks=16,
            target_buffer_seconds=15.0,
            min_safe_runway_seconds=4.0,
        )
        # 120 chunks (~120 MiB ahead, typical of 120 Mbps 4K Remux calculate_window)
        h.chunks = [
            _TestChunk(
                start=256 * 1024 + (i - 1) * chunk_sz if i > 0 else 0,
                end=(256 * 1024 + i * chunk_sz - 1) if i > 0 else (256 * 1024 - 1),
                size=chunk_sz if i > 0 else 256 * 1024,
                index=i,
                is_cached=h.chunks[0].is_cached.__class__(i == 0),
            )
            for i in range(121)
        ]
        for c in h.chunks:
            c.emit_cache_signal = lambda ch=c: setattr(ch.is_cached, "value", True)
        h.stream.file_metadata = SimpleNamespace(
            file_size=256 * 1024 + 120 * chunk_sz,
            original_filename="remux-wave",
            path="remux-wave",
        )
        h.stream.adaptive_prefetch.calculate_window = lambda **_: 120
        h.stream.chunker.footer_start = 256 * 1024 + 120 * chunk_sz
        h.stream.chunker.get_prefetch_uncached = lambda after_end=0, count=120, **_: (
            OrderedSet(
                c for c in h.chunks if c.start > after_end and not c.is_cached.value
            )[:count]
        )

        dispatched_batches: list[list[int]] = []

        async def record_parallel_prefetch(chunks, generation, process_chunks):
            dispatched_batches.append([c.index for c in chunks])
            await h.stream.close()

        h.stream._parallel_prefetch = record_parallel_prefetch

        with trio.fail_after(2):
            async with trio.open_nursery() as nursery:
                nursery.start_soon(h.stream.run, 256 * 1024)
                h.stream._consumer_end = 256 * 1024 - 1
                h.stream.recent_reads.current_read.value = SimpleNamespace(
                    read_type="cache_hit",
                    chunk_range=SimpleNamespace(
                        request_range=(0, 256 * 1024 - 1),
                        uncached_chunks=OrderedSet(),
                        position=0,
                    ),
                )

        # W=4 with 1 MiB chunks coalescing up to 4 MiB -> wave_cap = 16 chunks (4 per worker)
        assert len(dispatched_batches) == 1
        assert dispatched_batches[0] == list(range(1, 17))

    trio.run(run)


def test_speculative_span_run_preserves_unattempted_tail_after_cached_break() -> None:
    """Stopping early on a cached/claimed middle chunk must re-queue unattempted suffix chunks."""
    from contextlib import asynccontextmanager

    import trio_util

    from program.services.streaming.cache import CachePutResult

    chunk_size = 1024 * 1024
    total_size = 4 * chunk_size
    chunks = [
        _TestChunk(
            start=i * chunk_size,
            end=(i + 1) * chunk_size - 1,
            size=chunk_size,
            index=i,
            is_cached=trio_util.AsyncBool(i == 1),
        )
        for i in range(4)
    ]
    for c in chunks:
        c.emit_cache_signal = lambda ch=c: setattr(ch.is_cached, "value", True)

    observed_ranges: list[str] = []

    class Pool:
        @asynccontextmanager
        async def admit(self, kind, *, workload):
            assert (kind, workload) == ("body", "background")
            yield

        def acquire_lease(self, *, use_proxy):
            return SimpleNamespace(client=SimpleNamespace(stream=self.stream))

        @asynccontextmanager
        async def stream(self, method, url, *, headers, timeout):
            observed_ranges.append(headers["Range"])
            start_s, end_s = headers["Range"].removeprefix("bytes=").split("-")
            start, end = int(start_s), int(end_s)
            length = end - start + 1

            async def aiter_raw():
                yield bytes([65 + (start // chunk_size)]) * length

            yield SimpleNamespace(
                status_code=206,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{total_size}",
                    "Content-Length": str(length),
                },
                raise_for_status=lambda: None,
                aiter_raw=aiter_raw,
            )

        async def release_lease(self, lease):
            return None

    async def run() -> None:
        stream = MediaStream.__new__(MediaStream)
        stream.config = SimpleNamespace(
            chunk_size=chunk_size,
            block_size=128 * 1024,
            chunk_wait_timeout_seconds=10,
            connect_timeout_seconds=10,
        )
        stream._producer_claims = _ProducerClaims()
        stream._delivery_registry = SimpleNamespace(
            get_pending_starts=lambda: [], has_pending=lambda _: False
        )
        stream.is_killed = trio_util.AsyncBool(False)
        stream.adaptive_prefetch = SimpleNamespace(
            current_generation=0, record_provider_fetch=MagicMock()
        )
        stream._use_proxy_client = False
        stream._speculative_active = 0
        stream._speculative_peak = 0
        stream._speculative_bytes = 0
        stream._speculative_failures = 0
        stream._speculative_inflight_chunks = set()
        stream._batch_spec_starts = {c.start for c in chunks}
        stream.session_statistics = SessionStatistics()
        stream.build_log_message = lambda text: text
        stream.target_url = SimpleNamespace(value="https://example.test/video")
        stream.file_metadata = SimpleNamespace(file_size=total_size)
        stream.provider = "test"
        stream._http_pool = Pool()
        stream._cache_chunk = AsyncMock(return_value=CachePutResult.STORED_HOT)

        await stream._fetch_speculative_span(chunks, 0)

        # C0 fetched singly, C1 skipped (cached), [C2, C3] preserved and fetched as one coalesced span
        assert observed_ranges == [
            f"bytes=0-{chunk_size - 1}",
            f"bytes={2 * chunk_size}-{total_size - 1}",
        ]
        assert all(c.is_cached.value for c in chunks)
        assert stream._producer_claims.active_starts() == set()

    trio.run(run)


def test_speculative_break_on_claimed_head_preserves_other_owner_and_fetches_tail() -> (
    None
):
    """When C0 is already claimed by another producer, C0 stays owned in _batch_spec_starts while [C1..C3] are fetched."""
    from contextlib import asynccontextmanager

    import trio_util

    from program.services.streaming.cache import CachePutResult

    chunk_size = 1024 * 1024
    total_size = 4 * chunk_size
    chunks = [
        _TestChunk(
            start=i * chunk_size,
            end=(i + 1) * chunk_size - 1,
            size=chunk_size,
            index=i,
            is_cached=trio_util.AsyncBool(False),
        )
        for i in range(4)
    ]
    for c in chunks:
        c.emit_cache_signal = lambda ch=c: setattr(ch.is_cached, "value", True)

    observed_ranges: list[str] = []

    class Pool:
        @asynccontextmanager
        async def admit(self, kind, *, workload):
            yield

        def acquire_lease(self, *, use_proxy):
            return SimpleNamespace(client=SimpleNamespace(stream=self.stream))

        @asynccontextmanager
        async def stream(self, method, url, *, headers, timeout):
            observed_ranges.append(headers["Range"])
            start_s, end_s = headers["Range"].removeprefix("bytes=").split("-")
            start, end = int(start_s), int(end_s)
            length = end - start + 1

            async def aiter_raw():
                yield b"X" * length

            yield SimpleNamespace(
                status_code=206,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{total_size}",
                    "Content-Length": str(length),
                },
                raise_for_status=lambda: None,
                aiter_raw=aiter_raw,
            )

        async def release_lease(self, lease):
            return None

    async def run() -> None:
        stream = MediaStream.__new__(MediaStream)
        stream.config = SimpleNamespace(
            chunk_size=chunk_size,
            block_size=128 * 1024,
            chunk_wait_timeout_seconds=10,
            connect_timeout_seconds=10,
        )
        stream._producer_claims = _ProducerClaims()
        acquired, c0_claim = stream._producer_claims.acquire(0)
        assert acquired
        stream._delivery_registry = SimpleNamespace(
            get_pending_starts=lambda: [], has_pending=lambda _: False
        )
        stream.is_killed = trio_util.AsyncBool(False)
        stream.adaptive_prefetch = SimpleNamespace(
            current_generation=0, record_provider_fetch=MagicMock()
        )
        stream._use_proxy_client = False
        stream._speculative_active = 0
        stream._speculative_peak = 0
        stream._speculative_bytes = 0
        stream._speculative_failures = 0
        stream._speculative_inflight_chunks = set()
        stream._batch_spec_starts = {c.start for c in chunks}
        stream.session_statistics = SessionStatistics()
        stream.build_log_message = lambda text: text
        stream.target_url = SimpleNamespace(value="https://example.test/video")
        stream.file_metadata = SimpleNamespace(file_size=total_size)
        stream.provider = "test"
        stream._http_pool = Pool()
        stream._cache_chunk = AsyncMock(return_value=CachePutResult.STORED_HOT)

        await stream._fetch_speculative_span(chunks, 0)

        assert observed_ranges == [f"bytes={chunk_size}-{total_size - 1}"]
        assert 0 in stream._batch_spec_starts
        assert all(c.is_cached.value for c in chunks[1:])
        stream._producer_claims.release(0, c0_claim)
        assert stream._producer_claims.active_starts() == set()

    trio.run(run)


def test_speculative_header_and_trickle_timeouts_fail_fast_below_http_read_timeout() -> (
    None
):
    """Speculative header stall (5s) and coalesced trickle stall (14s) fail fast before 30s STREAM_READ_TIMEOUT."""
    from contextlib import asynccontextmanager

    import trio_util
    from trio.testing import MockClock

    from program.services.streaming.cache import CachePutResult

    chunk_size = 1024 * 1024
    total_size = 4 * chunk_size

    def make_stream(pool):
        chunks = [
            _TestChunk(
                start=i * chunk_size,
                end=(i + 1) * chunk_size - 1,
                size=chunk_size,
                index=i,
                is_cached=trio_util.AsyncBool(False),
            )
            for i in range(4)
        ]
        for c in chunks:
            c.emit_cache_signal = lambda ch=c: setattr(ch.is_cached, "value", True)
        stream = MediaStream.__new__(MediaStream)
        stream.config = SimpleNamespace(
            chunk_size=chunk_size,
            block_size=128 * 1024,
            chunk_wait_timeout_seconds=10,
            connect_timeout_seconds=10,
        )
        stream._producer_claims = _ProducerClaims()
        stream._delivery_registry = SimpleNamespace(
            get_pending_starts=lambda: [], has_pending=lambda _: False
        )
        stream.is_killed = trio_util.AsyncBool(False)
        stream.adaptive_prefetch = SimpleNamespace(
            current_generation=0, record_provider_fetch=MagicMock()
        )
        stream._use_proxy_client = False
        stream._speculative_active = 0
        stream._speculative_peak = 0
        stream._speculative_bytes = 0
        stream._speculative_failures = 0
        stream._speculative_inflight_chunks = set()
        stream.session_statistics = SessionStatistics()
        stream.build_log_message = lambda text: text
        stream.target_url = SimpleNamespace(value="https://example.test/video")
        stream.file_metadata = SimpleNamespace(file_size=total_size)
        stream.provider = "test"
        stream._http_pool = pool
        stream._cache_chunk = AsyncMock(return_value=CachePutResult.STORED_HOT)
        return stream, chunks

    async def run() -> None:
        # 1. Hung headers on both single chunk and coalesced span must abort at 5.0s, not 30s.
        released_leases = 0

        class HungHeaderPool:
            @asynccontextmanager
            async def admit(self, kind, *, workload):
                yield

            def acquire_lease(self, *, use_proxy):
                return SimpleNamespace(client=SimpleNamespace(stream=self.stream))

            @asynccontextmanager
            async def stream(self, method, url, *, headers, timeout):
                assert timeout.read == 30.0
                await trio.sleep_forever()
                yield  # pragma: no cover

            async def release_lease(self, lease):
                nonlocal released_leases
                released_leases += 1

        s_single, chunks_single = make_stream(HungHeaderPool())
        t0 = trio.current_time()
        await s_single._fetch_speculative_chunk(chunks_single[0], 0)
        elapsed_single = trio.current_time() - t0
        assert elapsed_single == pytest.approx(5.0)
        assert s_single._speculative_failures == 1
        assert s_single._producer_claims.active_starts() == set()
        assert s_single._speculative_active == 0
        assert s_single._speculative_inflight_chunks == set()
        assert released_leases == 1

        s_span, chunks_span = make_stream(HungHeaderPool())
        t1 = trio.current_time()
        await s_span._fetch_speculative_span_run(chunks_span, 0)
        elapsed_span = trio.current_time() - t1
        assert elapsed_span == pytest.approx(5.0)
        assert s_span._speculative_failures == 1
        assert s_span._producer_claims.active_starts() == set()
        assert s_span._speculative_active == 0
        assert s_span._speculative_inflight_chunks == set()

        # 2. Slow 6s/chunk trickle across 4 chunks (<7s per chunk) hits 14.0s span ceiling,
        # commits first 2 completed chunks cleanly, and fails cleanly instead of taking 24s+.
        released_trickle = 0

        class SlowTricklePool:
            @asynccontextmanager
            async def admit(self, kind, *, workload):
                yield

            def acquire_lease(self, *, use_proxy):
                return SimpleNamespace(client=SimpleNamespace(stream=self.stream))

            @asynccontextmanager
            async def stream(self, method, url, *, headers, timeout):
                async def aiter_raw():
                    for idx in range(4):
                        await trio.sleep(6.0)
                        yield bytes([65 + idx]) * chunk_size

                yield SimpleNamespace(
                    status_code=206,
                    headers={
                        "Content-Range": f"bytes 0-{total_size - 1}/{total_size}",
                        "Content-Length": str(total_size),
                    },
                    raise_for_status=lambda: None,
                    aiter_raw=aiter_raw,
                )

            async def release_lease(self, lease):
                nonlocal released_trickle
                released_trickle += 1

        s_trickle, chunks_trickle = make_stream(SlowTricklePool())
        t2 = trio.current_time()
        await s_trickle._fetch_speculative_span_run(chunks_trickle, 0)
        elapsed_trickle = trio.current_time() - t2
        assert elapsed_trickle == pytest.approx(14.0)
        assert chunks_trickle[0].is_cached.value is True
        assert chunks_trickle[1].is_cached.value is True
        assert chunks_trickle[2].is_cached.value is False
        assert chunks_trickle[3].is_cached.value is False
        assert s_trickle._speculative_failures == 1
        assert s_trickle._producer_claims.active_starts() == set()
        assert released_trickle == 1

    trio.run(run, clock=MockClock(autojump_threshold=0))


def test_fetch_speculative_span_missing_reservation_waits_without_spin() -> None:
    """Missing _delivery_registry entry on blocking foreground demand must wait on primary instead of CPU-spinning."""

    async def run() -> None:
        h = _Harness(workers=2)
        h.stream._batch_spec_starts = {h.chunks[2].start}
        primary_done = trio.Event()
        # Foreground pending exists, but get_entry intentionally returns None (race / torn-down reservation)
        h.stream._delivery_registry.get_pending_starts = lambda: [h.chunks[1].start]  # type: ignore[method-assign]
        h.stream._delivery_registry.get_entry = lambda _start: None  # type: ignore[method-assign]

        async def complete_primary():
            await trio.sleep(0.05)
            primary_done.set()

        with trio.fail_after(1):
            async with trio.open_nursery() as nursery:
                nursery.start_soon(complete_primary)
                await h.stream._fetch_speculative_span(
                    [h.chunks[2]],
                    0,
                    [h.chunks[1]],
                    primary_done,
                )

    trio.run(run)


def test_pending_backward_demand_triggers_seek_instead_of_skipping() -> None:
    """Backward pending foreground demand in _delivery_registry must trigger seek instead of starving."""
    from contextlib import asynccontextmanager

    from ordered_set import OrderedSet

    from program.services.streaming.stream_connection import StreamConnection

    async def run() -> None:
        h = _Harness(workers=2)
        h.stream.adaptive_prefetch.calculate_window = lambda **_: 1
        # Chunk 1 and Chunk 4 are cached so only Chunk 2 (offset 8) needs provider bytes.
        h.chunks[1].emit_cache_signal()
        h.chunks[4].emit_cache_signal()
        h.gates[2].set()
        h.gates[3].set()

        @asynccontextmanager
        async def connection(*, position):
            async with h.connection(position=position) as conn:
                conn.seek = StreamConnection.seek.__get__(conn)
                yield conn

        h.stream.manage_connection = connection

        # Advance connection to offset 16 (Chunk 4), ahead of pending Chunk 2 (offset 8).
        reservation = await h.stream._delivery_registry.register_pending(8)
        h.stream._consumer_end = 19
        h.stream.recent_reads.current_read.value = SimpleNamespace(
            read_type="cache_hit",
            chunk_range=SimpleNamespace(
                request_range=(16, 19),
                uncached_chunks=OrderedSet(),
                position=16,
            ),
        )

        with trio.fail_after(2):
            async with trio.open_nursery() as nursery:
                nursery.start_soon(h.stream.run, 16)
                await h.published[2].wait()
                await h.stream._delivery_registry.release_reservation(reservation)
                await h.stream.close()

        assert h.chunks[2].is_cached.value is True
        assert ("foreground", 8) in h.requests

    trio.run(run)


def test_speculative_span_slow_cache_commit_does_not_trip_network_stall_deadline() -> (
    None
):
    """Slow local Cache.put admission must not abort an already-downloaded chunk via network chunk_stall_sec."""
    from contextlib import asynccontextmanager

    import trio_util
    from trio.testing import MockClock

    from program.services.streaming.cache import CachePutResult

    chunk_size = 1024 * 1024
    chunks = [
        _TestChunk(
            start=i * chunk_size,
            end=(i + 1) * chunk_size - 1,
            size=chunk_size,
            index=i,
            is_cached=trio_util.AsyncBool(False),
        )
        for i in range(2)
    ]
    for c in chunks:
        c.emit_cache_signal = lambda ch=c: setattr(ch.is_cached, "value", True)

    class Pool:
        @asynccontextmanager
        async def admit(self, kind, *, workload):
            yield

        def acquire_lease(self, *, use_proxy):
            return SimpleNamespace(client=SimpleNamespace(stream=self.stream))

        @asynccontextmanager
        async def stream(self, method, url, *, headers, timeout):
            async def aiter_raw():
                # 4s < 7s chunk_stall_sec, followed by a 3.5s cache commit -> 7.5s > 7s per chunk,
                # yet still inside the 14s hard span budget across both chunks (12.0s total).
                await trio.sleep(4.0)
                yield b"A" * chunk_size
                await trio.sleep(1.0)
                yield b"B" * chunk_size

            yield SimpleNamespace(
                status_code=206,
                headers={
                    "Content-Range": f"bytes 0-{2 * chunk_size - 1}/{2 * chunk_size}",
                    "Content-Length": str(2 * chunk_size),
                },
                raise_for_status=lambda: None,
                aiter_raw=aiter_raw,
            )

        async def release_lease(self, lease):
            return None

    async def slow_cache(*, start, data, admission):
        await trio.sleep(3.5)
        return CachePutResult.STORED_WARM

    async def run() -> None:
        stream = MediaStream.__new__(MediaStream)
        stream.config = SimpleNamespace(
            chunk_size=chunk_size,
            block_size=128 * 1024,
            chunk_wait_timeout_seconds=10,
            connect_timeout_seconds=10,
        )
        stream._producer_claims = _ProducerClaims()
        stream._delivery_registry = SimpleNamespace(
            get_pending_starts=lambda: [], has_pending=lambda _: False
        )
        stream.is_killed = trio_util.AsyncBool(False)
        stream.adaptive_prefetch = SimpleNamespace(
            current_generation=0, record_provider_fetch=MagicMock()
        )
        stream._use_proxy_client = False
        stream._speculative_active = 0
        stream._speculative_peak = 0
        stream._speculative_bytes = 0
        stream._speculative_failures = 0
        stream._speculative_inflight_chunks = set()
        stream.session_statistics = SessionStatistics()
        stream.build_log_message = lambda text: text
        stream.target_url = SimpleNamespace(value="https://example.test/video")
        stream.file_metadata = SimpleNamespace(file_size=2 * chunk_size)
        stream.provider = "test"
        stream._http_pool = Pool()
        stream._cache_chunk = slow_cache

        await stream._fetch_speculative_span_run(chunks, 0)

        assert chunks[0].is_cached.value is True
        assert chunks[1].is_cached.value is True
        assert stream._speculative_failures == 0
        assert stream._producer_claims.active_starts() == set()

    trio.run(run, clock=MockClock(autojump_threshold=0))


def test_speculative_timeout_budgets_preserve_headroom_for_high_wait_timeout() -> None:
    """High chunk_wait_timeout_seconds (e.g. 35s) must leave headroom below foreground timeout."""
    stream = MediaStream.__new__(MediaStream)
    stream.config = SimpleNamespace(chunk_wait_timeout_seconds=10)
    assert stream._speculative_timeout_budgets() == pytest.approx((5.0, 7.0, 14.0))

    stream.config = SimpleNamespace(chunk_wait_timeout_seconds=35)
    header_s, stall_s, span_s = stream._speculative_timeout_budgets()
    assert header_s == pytest.approx(5.0)
    assert stall_s == pytest.approx(7.0)
    assert span_s == pytest.approx(21.0)
    assert span_s < 35.0
