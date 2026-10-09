"""Deterministic production read/run/publication tests; no CDN or disk timing."""

from contextlib import asynccontextmanager

import httpx
import pytest
import trio
from kink import di
from trio.testing import MockClock, wait_all_tasks_blocked

from program.services.streaming import media_stream as module
from program.services.streaming.cache import Cache, CachePutResult
from program.services.streaming.chunker import ChunkCacheNotifier
from program.services.streaming.media_stream import MediaStream
from program.services.streaming.stream_connection import StreamConnection

MIB = 1024 * 1024
CHUNK = 6 * MIB


class MemoryCache:
    usage_percentage = 98.0
    protected_usage_percentage = 0.0

    def __init__(self, processing):
        self.data = {}
        self.processing = processing
        self.puts = []
        self.leases = []
        self.refuse = False

    def has(self, cache_key, start, end):
        return any(
            offset <= start and offset + len(data) > end
            for offset, data in self.data.items()
        )

    async def get(self, cache_key, start, end, **kwargs):
        for offset, data in self.data.items():
            if offset <= start and offset + len(data) > end:
                return data[start - offset : end - offset + 1]
        return b""

    async def put(self, cache_key, start, data, **kwargs):
        await trio.sleep(self.processing)
        self.puts.append((start, kwargs))
        if self.refuse and kwargs["admission"] == "prefetch":
            return CachePutResult.SKIPPED_PREFETCH_PRESSURE
        self.data[start] = data
        return CachePutResult.STORED_HOT

    def reconcile_stream_playhead(self, **kwargs):
        self.leases.append(kwargs)

    def release_stream(self, stream_id):
        pass


@pytest.fixture
def setup_stream(monkeypatch):
    saved = {key: di[key] for key in (Cache, ChunkCacheNotifier) if key in di}
    monkeypatch.setattr(module, "monotonic", trio.current_time)

    def make(nursery, processing=0.0, delays=(1.1,)):
        cache = MemoryCache(processing)
        di[Cache] = cache
        di[ChunkCacheNotifier] = ChunkCacheNotifier()
        stream = MediaStream(
            fh=819,
            file_size=CHUNK * 200,
            path="/runway.mkv",
            original_filename="runway.mkv",
            nursery=nursery,
            provider="realdebrid",
            initial_url="https://example.test/movie",
            bitrate=46_000_000,
        )
        from dataclasses import replace

        stream.config = replace(
            stream.config, chunk_size=CHUNK, chunk_wait_timeout_seconds=120
        )
        stream.chunker.chunk_size = CHUNK
        stream.adaptive_prefetch.config.chunk_size_bytes = CHUNK
        fetched = []
        connections = []
        active = [0]

        @asynccontextmanager
        async def connect(*, position):
            start = stream.chunker.get_chunk_range(position=position).first_chunk.start
            active[0] += 1
            assert active[0] == 1
            response = httpx.Response(
                206, request=httpx.Request("GET", stream.target_url.value)
            )

            async def reader():
                offset = start
                while offset < stream.chunker.footer_start:
                    await trio.sleep(delays[len(fetched) % len(delays)])
                    fetched.append(offset)
                    yield b"x" * CHUNK
                    offset += CHUNK

            connection = StreamConnection(
                response=response,
                start_position=start,
                current_read_position=start,
                reader=reader(),
            )
            stream._active_stream_connection = connection
            connections.append(connection)
            try:
                yield connection
            finally:
                active[0] -= 1
                stream._active_stream_connection = None
                stream._inflight_chunk = None

        monkeypatch.setattr(stream, "manage_connection", connect)
        return stream, cache, fetched, connections

    yield make
    for key in (Cache, ChunkCacheNotifier):
        if key in saved:
            di[key] = saved[key]
        elif key in di:
            del di[key]


async def read(stream, start, size=128 * 1024):
    return await stream.read(
        request_start=start, request_end=start + size - 1, request_size=size
    )


@pytest.mark.parametrize(
    "delay,processing", [(1.0, 1.0), (1.2, 2.0), (4.5, 1.0), (6.5, 2.0)]
)
def test_rolling_actual_producer_is_bounded_and_counts_publication(
    setup_stream, delay, processing
):
    async def exercise():
        async with trio.open_nursery() as nursery:
            stream, cache, fetched, connections = setup_stream(
                nursery, processing, (delay,)
            )
            start = stream.config.header_size
            assert len(await read(stream, start)) == 128 * 1024
            # Consumer cache hits move ahead during the initial fill.
            for index in range(1, 12):
                await trio.sleep(0.15)
                assert len(await read(stream, start + index * 128 * 1024)) == 128 * 1024
            await wait_all_tasks_blocked()
            await trio.sleep(250)
            window = stream.adaptive_prefetch.last_calculated_window
            assert len(fetched) <= window + 2
            assert len(fetched) == len(set(fetched))
            assert len(connections) == 1
            assert stream.adaptive_prefetch.get_fetch_p95() == pytest.approx(
                delay + processing
            )
            assert stream._consumer_end == start + 12 * 128 * 1024 - 1
            assert stream.adaptive_prefetch._cached_runway_bytes > 0
            assert cache.puts[0][1]["admission"] == "demand"
            assert all(
                kwargs["stream_id"] == stream.stream_id for _, kwargs in cache.puts
            )
            before = len(fetched)
            await stream.close()
            await trio.sleep(20)
            assert len(fetched) == before
            nursery.cancel_scope.cancel()

    trio.run(exercise, clock=MockClock(autojump_threshold=0))


def test_runway_partial_chunks_holes_and_reservations(setup_stream):
    async def exercise():
        async with trio.open_nursery() as nursery:
            stream, cache, _, _ = setup_stream(nursery)
            start = stream.config.header_size
            cache.data[start] = b"x" * CHUNK
            cache.data[start + 2 * CHUNK] = b"x" * CHUNK
            await stream._delivery_registry.register_pending(start + CHUNK)
            stream._update_runway_observation(start + CHUNK // 2)
            assert stream.adaptive_prefetch._cached_runway_bytes == CHUNK // 2
            assert stream.adaptive_prefetch._inflight_runway_bytes == 0
            stream._inflight_chunk = (0, start + CHUNK)
            stream._update_runway_observation(start + CHUNK // 2)
            assert stream.adaptive_prefetch._inflight_runway_bytes == CHUNK
            stream.adaptive_prefetch.current_generation += 1
            stream._update_runway_observation(start + CHUNK // 2)
            assert stream.adaptive_prefetch._inflight_runway_bytes == 0
            nursery.cancel_scope.cancel()

    trio.run(exercise)


def test_cached_scans_do_not_move_consumer_or_protection(setup_stream):
    async def exercise():
        async with trio.open_nursery() as nursery:
            stream, cache, fetched, _ = setup_stream(nursery)
            start = stream.config.header_size
            await read(stream, start)
            generation = stream.adaptive_prefetch.current_generation
            consumer = stream._consumer_end
            leases = len(cache.leases)
            for offset in (0, stream.chunker.footer_start, start + CHUNK * 90):
                chunk = stream.chunker.get_chunk_range(position=offset).first_chunk
                cache.data[chunk.start] = b"x" * chunk.size
                await read(stream, offset, min(128 * 1024, chunk.size))
                assert stream._consumer_end == consumer
                assert stream.adaptive_prefetch.current_generation == generation
                assert len(cache.leases) == leases
            await read(stream, consumer + 1)
            assert stream._consumer_end == consumer + 128 * 1024
            await stream.close()
            assert len(fetched) == len(set(fetched))
            nursery.cancel_scope.cancel()

    trio.run(exercise, clock=MockClock(autojump_threshold=0))


@pytest.mark.parametrize(
    "delays,expect_stalls", [((0.3, 0.3, 0.3, 2.5), False), ((1.5,), True)]
)
def test_paced_concurrent_consumer_at_46mbps(setup_stream, delays, expect_stalls):
    async def exercise():
        async with trio.open_nursery() as nursery:
            stream, cache, fetched, connections = setup_stream(nursery, 0.05, delays)
            start = stream.config.header_size
            await read(stream, start)
            # Explicit player startup buffer, not serial producer/consumer acceptance.
            await trio.sleep(20)
            before = len(fetched)
            block = 128 * 1024
            interval = block * 8 / 46_000_000
            stalls = []
            for index in range(1, 60 * CHUNK // block):
                await trio.sleep(interval)
                pos = start + index * block
                cr = stream.chunker.get_chunk_range(position=pos, size=block)
                was_cached = [
                    stream._check_cache(start=c.start, end=c.end) for c in cr.chunks
                ]
                began = trio.current_time()
                await read(stream, pos)
                wait = trio.current_time() - began
                stalls.append(wait)
                manager = stream.adaptive_prefetch
                assert (
                    manager._cached_runway_bytes
                    <= (manager.last_calculated_window + 1) * CHUNK
                )
            assert len(fetched) > before + 30  # Several cache-hit refill cycles.
            assert len(fetched) == len(set(fetched))
            assert len(connections) == 1
            assert any(wait > 0.05 for wait in stalls) == expect_stalls
            await stream.close()
            closed_count = len(fetched)
            await trio.sleep(20)
            assert len(fetched) == closed_count
            nursery.cancel_scope.cancel()

    trio.run(exercise, clock=MockClock(autojump_threshold=0))


def test_refused_prefetch_does_not_spin(setup_stream):
    async def exercise():
        async with trio.open_nursery() as nursery:
            stream, cache, fetched, _ = setup_stream(nursery)
            cache.refuse = True
            await read(stream, stream.config.header_size)
            await trio.sleep(100)
            assert len(fetched) == 2
            assert cache.puts[-1][1]["admission"] == "prefetch"
            await stream.close()
            nursery.cancel_scope.cancel()

    trio.run(exercise, clock=MockClock(autojump_threshold=0))
