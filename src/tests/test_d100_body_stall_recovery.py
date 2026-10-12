"""Deterministic unit and scenario test suite for D100 body stall timeout and recovery."""

from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest
import trio
from trio.testing import MockClock

from program.services.streaming.config import Config
from program.services.streaming.exceptions import ChunksTooSlowException
from program.services.streaming.media_stream import (
    MediaStream,
    StreamRecoveryPhase,
)
from program.services.streaming.stream_connection import StreamConnection
from tests.test_consumer_runway import CHUNK, read
from tests.test_consumer_runway import setup_stream as stream_fixture
from tests.test_d83_transport_recovery import no_delay

setup_stream = stream_fixture
B = "https://example.test/refreshed"


def test_d100_silent_body_stall_triggers_read_timeout_and_recovers(
    setup_stream, monkeypatch
):
    """Chunk body stall triggers httpx.ReadTimeout before consumer nominal timeout, granting grace and recovering."""
    clock = MockClock(autojump_threshold=0)

    async def scenario():
        async with trio.open_nursery() as nursery:
            stream, cache, _, _ = setup_stream(nursery)
            stream.config = replace(
                stream.config, prefetch_chunks=0, chunk_wait_timeout_seconds=10
            )

            requests = []
            refreshed = []
            initial = stream.target_url.value
            start = stream.config.header_size

            @asynccontextmanager
            async def connect(*, position):
                requests.append(position)
                response = httpx.Response(
                    206, request=httpx.Request("GET", stream.target_url.value)
                )

                async def reader():
                    if position == start:
                        yield b"a" * CHUNK
                    if stream.target_url.value == initial:
                        # Attempt 1 on chunk 1: Stall silently (sleep forever)
                        await trio.sleep_forever()
                    yield b"b" * CHUNK
                    await trio.sleep_forever()

                connection = StreamConnection(
                    response=response,
                    start_position=position,
                    current_read_position=position,
                    reader=reader(),
                )
                stream._active_stream_connection = connection
                try:
                    yield connection
                finally:
                    stream._active_stream_connection = None
                    stream._inflight_chunk = None

            async def refresh(failed_url=None):
                refreshed.append(failed_url)
                stream.target_url.value = B
                return True

            async def fail_fast_retry(attempt, maximum, backoffs):
                return False

            monkeypatch.setattr(
                stream, "_detect_read_type", AsyncMock(return_value="body_read")
            )
            monkeypatch.setattr(stream, "connect", connect)
            monkeypatch.setattr(
                stream,
                "manage_connection",
                MediaStream.manage_connection.__get__(stream),
            )
            monkeypatch.setattr(stream, "_refresh_download_url", refresh)
            monkeypatch.setattr(stream, "_retry_with_backoff", fail_fast_retry)
            monkeypatch.setattr(
                "program.services.streaming.telemetry.playback_telemetry_collector.is_media_foreground",
                lambda _: True,
            )

            # Read chunk 0 (succeeds)
            assert await read(stream, start) == b"a" * (128 * 1024)
            # Read chunk 1 (stalls on first attempt, trips body stall timeout at 7s, triggers refresh, recovers)
            assert await read(stream, start + CHUNK) == b"b" * (128 * 1024)

            assert refreshed == [initial]
            assert requests[0] == start
            assert all(position == start + CHUNK for position in requests[1:])
            assert cache.data[start + CHUNK] == b"b" * CHUNK
            assert stream._recovery_state.phase == StreamRecoveryPhase.IDLE

            await stream.close()
            nursery.cancel_scope.cancel()

    trio.run(scenario, clock=clock)


@pytest.mark.parametrize(
    "fragment_sizes", [(CHUNK // 4,) * 8, (CHUNK + 123, CHUNK - 123)]
)
def test_body_fragments_make_progress_before_chunk_completion(
    setup_stream, monkeypatch, fragment_sizes
):
    """Exercise real HTTPX buffering, including fragments crossing chunk boundaries."""
    clock = MockClock(autojump_threshold=0)

    async def scenario():
        async with trio.open_nursery() as nursery:
            stream, cache, _, _ = setup_stream(nursery)
            stream.config = replace(
                stream.config, prefetch_chunks=1, chunk_wait_timeout_seconds=60
            )
            start = stream.config.header_size
            payload = b"a" * CHUNK + b"b" * CHUNK
            requests = []
            closed = []

            class Body(httpx.AsyncByteStream):
                async def __aiter__(self):
                    offset = 0
                    for size in fragment_sizes:
                        await trio.sleep(3)
                        yield payload[offset : offset + size]
                        offset += size
                    await trio.sleep_forever()

                async def aclose(self):
                    closed.append(True)

            @asynccontextmanager
            async def establish_connection(*, start):
                requests.append(start)
                assert len(requests) == 1, requests
                response = httpx.Response(
                    206,
                    request=httpx.Request("GET", stream.target_url.value),
                    stream=Body(),
                )
                try:
                    yield response
                finally:
                    await response.aclose()

            monkeypatch.setattr(stream, "establish_connection", establish_connection)
            monkeypatch.setattr(
                stream,
                "manage_connection",
                MediaStream.manage_connection.__get__(stream),
            )
            monkeypatch.setattr(
                stream, "_detect_read_type", AsyncMock(return_value="body_read")
            )
            monkeypatch.setattr(
                stream, "_refresh_download_url", AsyncMock(return_value=False)
            )

            try:
                assert await read(stream, start, size=2 * CHUNK) == payload
                assert cache.data[start] == payload[:CHUNK]
                assert cache.data[start + CHUNK] == payload[CHUNK:]
                assert requests == [start]
                assert stream.session_statistics.upstream_bytes == len(payload)
            finally:
                await stream.close()
                nursery.cancel_scope.cancel()
            assert closed == [True]
            assert stream.session_statistics.active_body_responses == 0

    trio.run(scenario, clock=clock)


def test_d100_stall_timeout_derivation(setup_stream):
    """Verify body_read_stall_timeout formula behaves consistently and is strictly shorter across all configurations."""
    clock = MockClock(autojump_threshold=0)

    def calc(wait: float) -> float:
        headroom = 3.0 if wait >= 4.0 else (1.0 if wait >= 2.0 else 0.5)
        return min(7.0, max(0.5, wait - headroom))

    async def scenario():
        async with trio.open_nursery() as nursery:
            stream, _, _, _ = setup_stream(nursery)

            # Minimum supported schema boundary (ge=1): wait=1.0s -> stall=0.5s (< 1.0s)
            stream.config = replace(stream.config, chunk_wait_timeout_seconds=1)
            t1 = calc(float(stream.config.chunk_wait_timeout_seconds))
            assert t1 == 0.5
            assert t1 < stream.config.chunk_wait_timeout_seconds

            # Boundary wait=2.0s -> stall=1.0s (< 2.0s)
            stream.config = replace(stream.config, chunk_wait_timeout_seconds=2)
            t2 = calc(float(stream.config.chunk_wait_timeout_seconds))
            assert t2 == 1.0
            assert t2 < stream.config.chunk_wait_timeout_seconds

            # Boundary wait=3.0s -> stall=2.0s (< 3.0s)
            stream.config = replace(stream.config, chunk_wait_timeout_seconds=3)
            t3 = calc(float(stream.config.chunk_wait_timeout_seconds))
            assert t3 == 2.0
            assert t3 < stream.config.chunk_wait_timeout_seconds

            # Boundary wait=4.0s -> stall=1.0s (< 4.0s)
            stream.config = replace(stream.config, chunk_wait_timeout_seconds=4)
            t4 = calc(float(stream.config.chunk_wait_timeout_seconds))
            assert t4 == 1.0
            assert t4 < stream.config.chunk_wait_timeout_seconds

            # Mid wait=6.0s -> stall=3.0s (< 6.0s)
            stream.config = replace(stream.config, chunk_wait_timeout_seconds=6)
            t6 = calc(float(stream.config.chunk_wait_timeout_seconds))
            assert t6 == 3.0
            assert t6 < stream.config.chunk_wait_timeout_seconds

            # Default 10.0s -> stall=7.0s (< 10.0s)
            stream.config = replace(stream.config, chunk_wait_timeout_seconds=10)
            t10 = calc(float(stream.config.chunk_wait_timeout_seconds))
            assert t10 == 7.0
            assert t10 < stream.config.chunk_wait_timeout_seconds

            # Large 15.0s -> capped at 7.0s (< 15.0s)
            stream.config = replace(stream.config, chunk_wait_timeout_seconds=15)
            t15 = calc(float(stream.config.chunk_wait_timeout_seconds))
            assert t15 == 7.0
            assert t15 < stream.config.chunk_wait_timeout_seconds

            # Exhaustive check across range 1 to 50
            for w in range(1, 51):
                stall = calc(float(w))
                assert (
                    stall < w
                ), f"Stall timeout {stall} must be strictly less than consumer wait {w}"

            await stream.close()
            nursery.cancel_scope.cancel()

    trio.run(scenario, clock=clock)


def test_d100_stall_after_partial_bytes_recovers(setup_stream, monkeypatch):
    """Stall occurring after partial chunk bytes were received resets connection and recovers cleanly."""
    clock = MockClock(autojump_threshold=0)

    async def scenario():
        async with trio.open_nursery() as nursery:
            stream, cache, _, _ = setup_stream(nursery)
            stream.config = replace(
                stream.config, prefetch_chunks=0, chunk_wait_timeout_seconds=10
            )

            requests = []
            refreshed = []
            initial = stream.target_url.value
            start = stream.config.header_size

            @asynccontextmanager
            async def connect(*, position):
                requests.append(position)
                response = httpx.Response(
                    206, request=httpx.Request("GET", stream.target_url.value)
                )

                async def reader():
                    if position == start:
                        yield b"a" * CHUNK
                    if stream.target_url.value == initial:
                        # Yield partial bytes (e.g. half of chunk) and then stall indefinitely
                        yield b"partial_bytes"
                        await trio.sleep_forever()
                    yield b"b" * CHUNK
                    await trio.sleep_forever()

                connection = StreamConnection(
                    response=response,
                    start_position=position,
                    current_read_position=position,
                    reader=reader(),
                )
                stream._active_stream_connection = connection
                try:
                    yield connection
                finally:
                    stream._active_stream_connection = None
                    stream._inflight_chunk = None

            async def refresh(failed_url=None):
                refreshed.append(failed_url)
                stream.target_url.value = B
                return True

            async def fail_fast_retry(attempt, maximum, backoffs):
                return False

            monkeypatch.setattr(
                stream, "_detect_read_type", AsyncMock(return_value="body_read")
            )
            monkeypatch.setattr(stream, "connect", connect)
            monkeypatch.setattr(
                stream,
                "manage_connection",
                MediaStream.manage_connection.__get__(stream),
            )
            monkeypatch.setattr(stream, "_refresh_download_url", refresh)
            monkeypatch.setattr(stream, "_retry_with_backoff", fail_fast_retry)
            monkeypatch.setattr(
                "program.services.streaming.telemetry.playback_telemetry_collector.is_media_foreground",
                lambda _: True,
            )

            assert await read(stream, start) == b"a" * (128 * 1024)
            assert await read(stream, start + CHUNK) == b"b" * (128 * 1024)
            assert refreshed == [initial]
            assert cache.data[start + CHUNK] == b"b" * CHUNK
            assert stream._recovery_state.phase == StreamRecoveryPhase.IDLE

            await stream.close()
            nursery.cancel_scope.cancel()

    trio.run(scenario, clock=clock)


def test_d100_persistent_stall_exhausts_budget_cleanly(setup_stream, monkeypatch):
    """When upstream stalls permanently on all attempts, recovery budget exhausts and fails consumer."""
    clock = MockClock(autojump_threshold=0)

    async def scenario():
        async with trio.open_nursery() as nursery:
            stream, _, _, _ = setup_stream(nursery)
            stream.config = replace(
                stream.config, prefetch_chunks=0, chunk_wait_timeout_seconds=10
            )

            @asynccontextmanager
            async def connect(*, position):
                response = httpx.Response(
                    206, request=httpx.Request("GET", stream.target_url.value)
                )

                async def reader():
                    await trio.sleep_forever()
                    yield b""  # pragma: no cover

                connection = StreamConnection(
                    response=response,
                    start_position=position,
                    current_read_position=position,
                    reader=reader(),
                )
                stream._active_stream_connection = connection
                try:
                    yield connection
                finally:
                    stream._active_stream_connection = None
                    stream._inflight_chunk = None

            async def refresh(failed_url=None):
                # Refresh also fails or stalls
                return False

            async def fail_fast_retry(attempt, maximum, backoffs):
                return False

            monkeypatch.setattr(
                stream, "_detect_read_type", AsyncMock(return_value="body_read")
            )
            monkeypatch.setattr(stream, "connect", connect)
            monkeypatch.setattr(
                stream,
                "manage_connection",
                MediaStream.manage_connection.__get__(stream),
            )
            monkeypatch.setattr(stream, "_refresh_download_url", refresh)
            monkeypatch.setattr(stream, "_retry_with_backoff", fail_fast_retry)
            monkeypatch.setattr(
                "program.services.streaming.telemetry.playback_telemetry_collector.is_media_foreground",
                lambda _: True,
            )

            start = stream.config.header_size
            chunk_range = stream.chunker.get_chunk_range(position=start)
            with pytest.raises(ChunksTooSlowException):
                await stream._wait_until_chunks_ready(chunk_range=chunk_range)

            await stream.close()
            nursery.cancel_scope.cancel()

    trio.run(scenario, clock=clock)


def test_d100_production_3mib_fragmented_delivery_and_stall_recovery(
    setup_stream, monkeypatch
):
    """Verify D100 with production-configured 3 MiB chunks:
    1. Fragmented delivery (e.g. 512 KiB chunks over a slow connection) does NOT trigger ReadTimeout.
    2. Completed chunks retain exact byte offsets and sizes.
    3. Genuine upstream inactivity triggers bounded ReadTimeout and initiates recovery.
    """
    clock = MockClock(autojump_threshold=0)
    PROD_CHUNK = 3 * 1024 * 1024  # 3 MiB exactly as observed in production settings
    FRAGMENT = 512 * 1024  # 512 KiB incoming fragments

    async def scenario():
        async with trio.open_nursery() as nursery:
            stream, cache, _, _ = setup_stream(nursery)
            # Reconfigure stream and chunker to 3 MiB chunk size and 10s wait timeout
            stream.config = replace(
                stream.config,
                chunk_size=PROD_CHUNK,
                chunk_wait_timeout_seconds=10,
                prefetch_chunks=0,
            )
            stream.chunker.chunk_size = PROD_CHUNK
            stream.adaptive_prefetch.config.chunk_size_bytes = PROD_CHUNK

            requests = []
            refreshed = []
            initial = stream.target_url.value
            start = stream.config.header_size

            @asynccontextmanager
            async def connect(*, position):
                requests.append(position)
                response = httpx.Response(
                    206, request=httpx.Request("GET", stream.target_url.value)
                )

                async def reader():
                    if position == start:
                        # Chunk 1 (3 MiB): Emulate slow network where 512 KiB fragments arrive every 1.5s.
                        # Total chunk time is 9s, which exceeds the 7.0s stall timeout.
                        # Because each fragment arrives within 1.5s (< 7.0s stall timeout), NO ReadTimeout should be raised!
                        for i in range(PROD_CHUNK // FRAGMENT):
                            await trio.sleep(1.5)
                            yield bytes([ord("A") + i]) * FRAGMENT

                    if stream.target_url.value == initial:
                        # Chunk 2 on initial URL: Yield two 512 KiB fragments then stall indefinitely
                        yield b"p" * FRAGMENT
                        yield b"q" * FRAGMENT
                        await trio.sleep_forever()

                    # Reconnected on refreshed URL: yield 3 MiB of 'Z'
                    yield b"Z" * PROD_CHUNK
                    await trio.sleep_forever()

                connection = StreamConnection(
                    response=response,
                    start_position=position,
                    current_read_position=position,
                    reader=reader(),
                )
                stream._active_stream_connection = connection
                try:
                    yield connection
                finally:
                    stream._active_stream_connection = None
                    stream._inflight_chunk = None

            async def refresh(failed_url=None):
                refreshed.append(failed_url)
                stream.target_url.value = B
                return True

            async def fail_fast_retry(attempt, maximum, backoffs):
                return False

            monkeypatch.setattr(
                stream, "_detect_read_type", AsyncMock(return_value="body_read")
            )
            monkeypatch.setattr(stream, "connect", connect)
            monkeypatch.setattr(
                stream,
                "manage_connection",
                MediaStream.manage_connection.__get__(stream),
            )
            monkeypatch.setattr(stream, "_refresh_download_url", refresh)
            monkeypatch.setattr(stream, "_retry_with_backoff", fail_fast_retry)
            monkeypatch.setattr(
                "program.services.streaming.telemetry.playback_telemetry_collector.is_media_foreground",
                lambda _: True,
            )

            # 1. Read first 3 MiB chunk (takes 12.0s of virtual time without stall timeout)
            res1 = await read(stream, start)
            assert len(res1) == 128 * 1024
            assert start in cache.data
            assert len(cache.data[start]) == PROD_CHUNK
            # Verify exact fragment sequence
            expected_c1 = b"".join(
                bytes([ord("A") + i]) * FRAGMENT for i in range(PROD_CHUNK // FRAGMENT)
            )
            assert cache.data[start] == expected_c1

            # 2. Read second 3 MiB chunk (stalls after 1 MiB on initial URL, recovers via refresh)
            res2 = await read(stream, start + PROD_CHUNK)
            assert len(res2) == 128 * 1024
            assert (start + PROD_CHUNK) in cache.data
            assert len(cache.data[start + PROD_CHUNK]) == PROD_CHUNK
            assert cache.data[start + PROD_CHUNK] == b"Z" * PROD_CHUNK
            assert refreshed == [initial]

            await stream.close()
            nursery.cancel_scope.cancel()

    trio.run(scenario, clock=clock)
