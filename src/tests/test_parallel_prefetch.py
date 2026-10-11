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
def test_speculative_exact_range_and_payload_integrity(
    payload: bytes, content_range: str, content_length: str | None, valid: bool
) -> None:
    from contextlib import asynccontextmanager

    from program.services.streaming.cache import CachePutResult
    from program.services.streaming.session_statistics import SessionStatistics

    class Response:
        headers = {"Content-Range": content_range}
        status_code = 206

        def raise_for_status(self):
            return None

        async def aiter_raw(self):
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
        async def stream(self, method, url, *, headers):
            self.headers = headers
            yield Response()

        async def release_lease(self, lease):
            self.released += 1

    async def run():
        stream = MediaStream.__new__(MediaStream)
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
        assert stream._speculative_failures == (0 if valid else 1)
        if valid:
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
            assert stream.session_statistics.upstream_bytes == expected_bytes
            assert stream.session_statistics.speculative_bytes == expected_bytes
            assert stream._speculative_bytes == 0

    trio.run(run)


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
            should_abort_prefetch=lambda g: g
            != stream.adaptive_prefetch.current_generation,
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
            yield

        return admission()

    def acquire_lease(self, *, use_proxy):
        self.leases += 1
        return SimpleNamespace(client=SimpleNamespace(stream=self.response))

    async def release_lease(self, lease):
        await trio.lowlevel.checkpoint()
        self.leases -= 1

    def response(self, method, url, *, headers):
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
