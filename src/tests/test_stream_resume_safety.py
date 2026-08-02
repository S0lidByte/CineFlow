"""Resume-path safeguards for opportunistic streaming prefetch."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import trio
from ordered_set import OrderedSet

from program.services.streaming.exceptions import EmptyDataException
from program.services.streaming.media_stream import MediaStream


def _bare_stream() -> MediaStream:
    stream = MediaStream.__new__(MediaStream)
    stream._trace_stream = MagicMock()  # type: ignore[method-assign]
    return stream


def test_empty_prefetch_does_not_fail_active_stream() -> None:
    stream = _bare_stream()
    fetch_chunks = AsyncMock(side_effect=EmptyDataException(range=(10, 19)))

    async def _run() -> bool:
        return await stream._run_opportunistic_prefetch(
            OrderedSet(),
            fetch_chunks,
            label="Prefetch",
        )

    assert trio.run(_run) is False
    fetch_chunks.assert_awaited_once()
    trace = stream._trace_stream.call_args.args[0]
    assert "did not return data" in trace
    assert "next playhead read" in trace


def test_successful_prefetch_reports_success() -> None:
    stream = _bare_stream()
    fetch_chunks = AsyncMock(return_value=None)

    async def _run() -> bool:
        return await stream._run_opportunistic_prefetch(
            OrderedSet(),
            fetch_chunks,
            label="Prefetch",
        )

    assert trio.run(_run) is True
    stream._trace_stream.assert_not_called()


def test_non_empty_prefetch_error_is_not_suppressed() -> None:
    stream = _bare_stream()
    fetch_chunks = AsyncMock(side_effect=RuntimeError("unexpected"))

    async def _run() -> None:
        await stream._run_opportunistic_prefetch(
            OrderedSet(),
            fetch_chunks,
            label="Prefetch",
        )

    with pytest.raises(RuntimeError, match="unexpected"):
        trio.run(_run)


def test_response_context_includes_range_headers() -> None:
    response = SimpleNamespace(
        status_code=206,
        headers=httpx.Headers(
            {
                "Content-Range": "bytes 10-19/100",
                "Content-Length": "10",
            }
        ),
    )

    assert MediaStream._response_context(response) == (
        "status=206, content-range=bytes 10-19/100, content-length=10"
    )


def test_empty_prefetch_closes_exhausted_connection_before_reconnect() -> None:
    class NeverSignal:
        value = False

        async def wait_value(self, *_args: object) -> None:
            await trio.sleep_forever()

    class KillSignal:
        def __init__(self) -> None:
            self.value = False
            self._event = trio.Event()

        async def wait_value(self, expected: bool) -> None:
            if self.value == expected:
                return
            await self._event.wait()

        def kill(self) -> None:
            self.value = True
            self._event.set()

    class EmptyReader:
        def __aiter__(self) -> "EmptyReader":
            return self

        async def __anext__(self) -> bytes:
            raise StopAsyncIteration

    class Chunk:
        start = 0
        end = 9
        size = 10
        index = 0

    class CurrentRead:
        def __init__(self) -> None:
            self.calls = 0

        async def eventual_values(self, _predicate: object):
            self.calls += 1
            if self.calls > 1:
                await trio.sleep_forever()
            yield SimpleNamespace(
                read_type="cache_hit",
                chunk_range=SimpleNamespace(
                    request_range=(0, 9),
                    uncached_chunks=OrderedSet(),
                ),
            )
            await trio.sleep_forever()

    stream = _bare_stream()
    stream.enable_tracing = False
    stream.is_killed = KillSignal()
    stream.target_url = NeverSignal()
    stream.target_url.value = "https://example.test/media"
    stream.config = SimpleNamespace(
        prefetch_chunks=1,
        chunk_size=10,
        header_size=0,
    )
    stream.file_metadata = SimpleNamespace(path="movie.mkv", file_size=100)
    stream.session_statistics = SimpleNamespace(bytes_transferred=0)
    stream.recent_reads = SimpleNamespace(
        current_read=CurrentRead(),
    )
    stream.chunker = SimpleNamespace(
        get_prefetch_uncached=lambda **_kwargs: OrderedSet([Chunk()]),
    )

    connection_closed = False
    connection_count = 0

    @asynccontextmanager
    async def stream_lifecycle():
        yield

    @asynccontextmanager
    async def manage_connection(*, position: int):
        nonlocal connection_closed, connection_count
        assert position == 0
        connection_count += 1
        if connection_count == 2:
            assert connection_closed is True
            stream.is_killed.kill()

        connection = SimpleNamespace(
            current_read_position=0,
            start_position=0,
            seek_range=None,
            seek_required=NeverSignal(),
            response=SimpleNamespace(
                request=SimpleNamespace(url="https://example.test/media")
            ),
            reader=EmptyReader(),
            increment_sequential_chunks=lambda: None,
        )
        try:
            yield connection
        finally:
            connection_closed = True

    stream.stream_lifecycle = stream_lifecycle
    stream.manage_connection = manage_connection

    async def _run() -> None:
        with trio.fail_after(1):
            await stream.run(0)

    trio.run(_run)

    assert connection_count == 2
