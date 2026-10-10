"""Deterministic reader-side recovery deadlines using real chunk notifications."""

from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest
import trio
from trio.testing import MockClock

from program.services.streaming.exceptions import ChunksTooSlowException
from program.services.streaming.media_stream import MediaStream, StreamRecoveryPhase
from program.services.streaming.stream_connection import StreamConnection
from tests.test_consumer_runway import CHUNK, read
from tests.test_consumer_runway import setup_stream as stream_fixture
from tests.test_d83_transport_recovery import no_delay

setup_stream = stream_fixture


@pytest.mark.parametrize(
    "case,expected",
    [
        ("idle", 10),
        ("background", 10),
        ("wrong_chunk", 10),
        ("stale_generation", 10),
        ("exhausted", 60),
        ("published", 15),
        ("seek", 11),
        ("close", 11),
    ],
)
def test_reader_recovery_deadlines(setup_stream, monkeypatch, case, expected):
    async def exercise():
        async with trio.open_nursery() as nursery:
            stream, _, _, _ = setup_stream(nursery)
            stream.config = replace(stream.config, chunk_wait_timeout_seconds=10)
            monkeypatch.setattr(
                stream, "_is_foreground_eligible", lambda: case != "background"
            )
            chunk_range = stream.chunker.get_chunk_range(
                position=stream.config.header_size
            )
            generation = stream.adaptive_prefetch.current_generation
            stream._set_recovery_state(
                (
                    StreamRecoveryPhase.IDLE
                    if case == "idle"
                    else StreamRecoveryPhase.TRANSPORT_RETRY
                ),
                generation=generation + (case == "stale_generation"),
                chunk_start=chunk_range.first_chunk.start
                + (CHUNK if case == "wrong_chunk" else 0),
                timeout_bound=60,
            )

            async def producer():
                await trio.sleep(15 if case == "published" else 11)
                if case == "published":
                    for chunk in chunk_range.chunks:
                        await stream._cache_chunk(
                            start=chunk.start, data=b"x" * chunk.size
                        )
                        chunk.emit_cache_signal()
                elif case == "seek":
                    stream.adaptive_prefetch.current_generation += 1
                else:
                    stream.is_killed.value = True
                stream._set_recovery_state(StreamRecoveryPhase.IDLE)

            if case in {"published", "seek", "close"}:
                nursery.start_soon(producer)
            started = trio.current_time()
            if case == "published":
                await stream._wait_until_chunks_ready(chunk_range=chunk_range)
            else:
                with pytest.raises(ChunksTooSlowException):
                    await stream._wait_until_chunks_ready(chunk_range=chunk_range)
            assert trio.current_time() - started == pytest.approx(expected)
            nursery.cancel_scope.cancel()

    trio.run(exercise, clock=MockClock(autojump_threshold=0))


@pytest.mark.parametrize("error_at", [3, 12])
def test_terminal_error_wakes_without_recovery_notification(setup_stream, error_at):
    async def exercise():
        async with trio.open_nursery() as nursery:
            stream, _, _, _ = setup_stream(nursery)
            stream.config = replace(stream.config, chunk_wait_timeout_seconds=10)
            stream.session_statistics.bytes_transferred = 1
            stream.session_statistics.body_read_count = 1
            chunks = stream.chunker.get_chunk_range(position=stream.config.header_size)
            stream._set_recovery_state(
                StreamRecoveryPhase.TRANSPORT_RETRY,
                chunk_start=chunks.first_chunk.start,
                timeout_bound=40,
            )
            failure = httpx.ReadError("terminal producer failure")

            async def fail():
                await trio.sleep(error_at)
                # Real producer error channel; deliberately no recovery event.
                stream._stream_error.value = failure

            nursery.start_soon(fail)
            started = trio.current_time()
            with pytest.raises(httpx.ReadError) as caught:
                await stream._wait_until_chunks_ready(chunk_range=chunks)
            assert caught.value is failure
            assert trio.current_time() - started == error_at
            nursery.cancel_scope.cancel()

    trio.run(exercise, clock=MockClock(autojump_threshold=0))


@pytest.mark.parametrize(
    "read_type", ["general_scan", "header_scan", "footer_scan", "footer_read"]
)
def test_discrete_reads_never_enter_reader_grace(setup_stream, monkeypatch, read_type):
    async def exercise():
        async with trio.open_nursery() as nursery:
            stream, _, _, _ = setup_stream(nursery)
            stream.config = replace(stream.config, chunk_wait_timeout_seconds=10)
            stream.session_statistics.bytes_transferred = CHUNK
            stream.session_statistics.body_read_count = 1
            stream._set_recovery_state(
                StreamRecoveryPhase.URL_REFRESH, timeout_bound=40
            )
            monkeypatch.setattr(
                stream, "_detect_read_type", AsyncMock(return_value=read_type)
            )
            waiter = AsyncMock(
                side_effect=AssertionError("scan entered recovery waiter")
            )
            monkeypatch.setattr(stream, "_wait_until_chunks_ready", waiter)
            calls = []

            async def fetch(*, start, size):
                calls.append((start, size))
                await trio.sleep(2)
                return b"s" * size

            monkeypatch.setattr(stream, "_fetch_discrete_byte_range", fetch)
            position = (
                stream.chunker.footer_start
                if read_type.startswith("footer")
                else (
                    0
                    if read_type == "header_scan"
                    else stream.config.header_size + 90 * CHUNK
                )
            )
            started = trio.current_time()
            assert await read(stream, position) == b"s" * (128 * 1024)
            assert trio.current_time() - started == 2
            assert len(calls) == 1
            waiter.assert_not_awaited()
            nursery.cancel_scope.cancel()

    trio.run(exercise, clock=MockClock(autojump_threshold=0))


def test_refresh_crosses_nominal_deadline_and_rearms_after_progress(
    setup_stream, monkeypatch
):
    async def exercise():
        async with trio.open_nursery() as nursery:
            stream, cache, _, _ = setup_stream(nursery)
            stream.config = replace(
                stream.config, prefetch_chunks=0, chunk_wait_timeout_seconds=10
            )
            start = stream.config.header_size
            urls = [
                stream.target_url.value,
                "https://example.test/b",
                "https://example.test/c",
            ]
            requests = []
            refreshes = []
            payloads = {start + i * CHUNK: bytes([97 + i]) * CHUNK for i in range(3)}

            @asynccontextmanager
            async def connect(*, position):
                url = stream.target_url.value
                requests.append((url, position))
                response = httpx.Response(206, request=httpx.Request("GET", url))

                async def reader():
                    good_start = start + urls.index(url) * CHUNK
                    if position == good_start:
                        # The refresh returns IDLE before the demanded bytes arrive.
                        await trio.sleep(1)
                        yield payloads[good_start]
                    yield b"partial-must-not-be-committed"
                    raise httpx.ReadError("broken body")

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
                refreshes.append(failed_url)
                await trio.sleep(12)
                stream.target_url.value = urls[urls.index(failed_url) + 1]
                return True

            monkeypatch.setattr(stream, "connect", connect)
            monkeypatch.setattr(
                stream,
                "manage_connection",
                MediaStream.manage_connection.__get__(stream),
            )
            monkeypatch.setattr(stream, "_refresh_download_url", refresh)
            monkeypatch.setattr(stream, "_retry_with_backoff", no_delay)
            monkeypatch.setattr(
                stream, "_detect_read_type", AsyncMock(return_value="body_read")
            )
            monkeypatch.setattr(stream, "_is_foreground_eligible", lambda: True)
            assert await read(stream, start) == payloads[start][: 128 * 1024]
            for index in (1, 2):
                position = start + index * CHUNK
                before = trio.current_time()
                assert await read(stream, position, CHUNK) == payloads[position]
                assert trio.current_time() - before == pytest.approx(13)
                assert cache.data[position] == payloads[position]
                assert sum(offset == position for offset, _ in cache.puts) == 1
                assert requests.count((urls[index], position)) == 1
                # Same URL cannot re-arm, even after progress.
                assert not await stream._recover_transport_url(urls[index - 1], 0)
            assert refreshes == urls[:2]
            assert requests == [(urls[0], start)] + [(urls[0], start + CHUNK)] * 4 + [
                (urls[1], start + CHUNK)
            ] + [(urls[1], start + 2 * CHUNK)] * 3 + [(urls[2], start + 2 * CHUNK)]
            assert set(cache.data) == set(payloads)
            assert stream._recovery_state.phase == StreamRecoveryPhase.IDLE
            await stream.close()
            assert not stream._delivery_registry._entries
            nursery.cancel_scope.cancel()

    trio.run(exercise, clock=MockClock(autojump_threshold=0))
