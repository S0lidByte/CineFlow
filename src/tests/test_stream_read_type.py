"""Characterization tests for MediaStream read-type classification."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import trio
from kink import di

from program.services.streaming.cache import Cache
from program.services.streaming.chunker import ChunkCacheNotifier, Chunker
from program.services.streaming.config import Config
from program.services.streaming.file_metadata import FileMetadata
from program.services.streaming.media_stream import MediaStream
from program.services.streaming.recent_reads import Read, RecentReads


@pytest.fixture(autouse=True)
def _register_streaming_di():
    """Chunk construction requires ChunkCacheNotifier in the DI container."""

    di[ChunkCacheNotifier] = ChunkCacheNotifier()
    cache_mock = MagicMock()
    cache_mock.has.return_value = False
    di[Cache] = cache_mock
    yield
    # kink Container supports del, not dict.pop
    if ChunkCacheNotifier in di:
        del di[ChunkCacheNotifier]
    if Cache in di:
        del di[Cache]


def _make_stream(
    *,
    file_size: int = 100 * 1024 * 1024,
    sequential_blocks: int = 10,
    scan_blocks: int = 25,
    cache_hit: bool = False,
) -> MediaStream:
    stream = MediaStream.__new__(MediaStream)
    stream.config = Config(
        chunk_size=1024 * 1024,
        activity_timeout_seconds=60,
        chunk_wait_timeout_seconds=10,
        connect_timeout_seconds=10,
        sequential_read_tolerance_blocks=sequential_blocks,
        scan_tolerance_blocks=scan_blocks,
    )
    stream.file_metadata = FileMetadata(
        original_filename="movie.mkv",
        file_size=file_size,
        path="/movies/movie.mkv",
    )
    stream.recent_reads = RecentReads()
    stream.chunker = Chunker(
        cache_key="movie.mkv",
        chunk_size=stream.config.chunk_size,
        header_size=stream.config.header_size,
        footer_size=stream.footer_size,
        file_size=file_size,
    )
    stream._check_cache = MagicMock(return_value=cache_hit)  # type: ignore[method-assign]
    return stream


def _set_previous_read(
    stream: MediaStream,
    *,
    position: int,
    size: int,
    read_type: str = "body_read",
) -> None:
    chunk_range = stream.chunker.get_chunk_range(position=position, size=size)
    # Explicit timestamp avoids trio.current_time() outside an async context.
    stream.recent_reads.previous_read.value = Read(
        chunk_range=chunk_range,
        read_type=read_type,  # type: ignore[arg-type]
        timestamp=0.0,
    )


@pytest.mark.parametrize(
    ("position", "size", "expected"),
    [
        (0, 64 * 1024, "header_scan"),
        (128 * 1024, 64 * 1024, "header_scan"),
    ],
)
def test_detect_header_scan(position: int, size: int, expected: str) -> None:
    stream = _make_stream()

    async def _run() -> None:
        chunk_range = stream.chunker.get_chunk_range(position=position, size=size)
        assert await stream._detect_read_type(chunk_range=chunk_range) == expected

    trio.run(_run)


def test_detect_cache_hit() -> None:
    stream = _make_stream(cache_hit=True)

    async def _run() -> None:
        chunk_range = stream.chunker.get_chunk_range(position=0, size=64 * 1024)
        assert await stream._detect_read_type(chunk_range=chunk_range) == "cache_hit"

    trio.run(_run)


def test_detect_body_read_sequential() -> None:
    stream = _make_stream()
    _set_previous_read(stream, position=stream.config.header_size, size=128 * 1024)

    async def _run() -> None:
        next_pos = stream.config.header_size + 128 * 1024
        chunk_range = stream.chunker.get_chunk_range(position=next_pos, size=128 * 1024)
        assert await stream._detect_read_type(chunk_range=chunk_range) == "body_read"

    trio.run(_run)


def test_detect_general_scan_large_jump() -> None:
    stream = _make_stream()
    _set_previous_read(stream, position=stream.config.header_size, size=128 * 1024)
    # Classification compares against last_read_end, not the previous start.
    last_end = stream.recent_reads.last_read_end
    assert last_end is not None
    jump_pos = last_end + stream.config.scan_tolerance + 1

    async def _run() -> None:
        chunk_range = stream.chunker.get_chunk_range(
            position=jump_pos,
            size=64 * 1024,  # less than one block
        )
        assert await stream._detect_read_type(chunk_range=chunk_range) == "general_scan"

    trio.run(_run)


def test_detect_general_scan_first_read_past_header() -> None:
    stream = _make_stream()

    async def _run() -> None:
        chunk_range = stream.chunker.get_chunk_range(
            position=stream.config.header_size + 1024,
            size=128 * 1024,
        )
        assert await stream._detect_read_type(chunk_range=chunk_range) == "general_scan"

    trio.run(_run)


def test_detect_footer_scan() -> None:
    stream = _make_stream()
    # Previous read ended near the start; jump into the footer with a large gap.
    _set_previous_read(stream, position=stream.config.header_size, size=128 * 1024)
    footer_pos = stream.file_metadata.file_size - stream.footer_size + 1024

    async def _run() -> None:
        chunk_range = stream.chunker.get_chunk_range(
            position=footer_pos, size=64 * 1024
        )
        assert await stream._detect_read_type(chunk_range=chunk_range) == "footer_scan"

    trio.run(_run)


def test_detect_footer_read_after_near_footer() -> None:
    stream = _make_stream()
    footer_start = stream.file_metadata.file_size - stream.footer_size
    # Prior read already inside footer region so sequential tolerance is satisfied.
    _set_previous_read(stream, position=footer_start, size=64 * 1024)

    async def _run() -> None:
        chunk_range = stream.chunker.get_chunk_range(
            position=footer_start + 64 * 1024,
            size=64 * 1024,
        )
        assert await stream._detect_read_type(chunk_range=chunk_range) == "footer_read"

    trio.run(_run)


def test_config_tolerance_defaults() -> None:
    config = Config(
        chunk_size=1024 * 1024,
        activity_timeout_seconds=60,
        chunk_wait_timeout_seconds=10,
        connect_timeout_seconds=10,
    )
    assert config.sequential_read_tolerance_blocks == 10
    assert config.scan_tolerance_blocks == 25
    assert config.block_size == 128 * 1024
    assert config.sequential_read_tolerance == 10 * 128 * 1024
    assert config.scan_tolerance == 25 * 128 * 1024


def test_read_lifecycle_cache_hit_does_not_start_worker() -> None:
    import trio_util

    stream = _make_stream(cache_hit=True)
    stream.is_streaming = trio_util.AsyncBool(False)
    stream._start_lock = trio.Lock()
    stream.nursery = MagicMock()

    chunk_range = stream.chunker.get_chunk_range(position=0, size=64 * 1024)

    async def _run() -> None:
        async with stream.read_lifecycle(chunk_range=chunk_range) as read_type:
            assert read_type == "cache_hit"
        stream.nursery.start.assert_not_called()

    trio.run(_run)


def test_read_lifecycle_sequential_cache_hit_starts_prefetch_worker() -> None:
    import trio_util

    stream = _make_stream(cache_hit=True)
    stream.is_streaming = trio_util.AsyncBool(False)
    stream._start_lock = trio.Lock()

    mock_nursery = MagicMock()

    async def mock_start(fn, pos):
        pass

    mock_nursery.start = MagicMock(side_effect=mock_start)
    stream.nursery = mock_nursery

    position = stream.config.header_size + 2 * stream.config.chunk_size
    _set_previous_read(
        stream,
        position=position,
        size=128 * 1024,
        read_type="cache_hit",
    )
    next_position = position + 128 * 1024
    chunk_range = stream.chunker.get_chunk_range(
        position=next_position,
        size=128 * 1024,
    )
    expected_start = stream.chunker.get_prefetch_uncached(
        after_end=chunk_range.request_range[1],
        count=stream.config.prefetch_chunks,
    )[0].start

    async def _run() -> None:
        async with stream.read_lifecycle(chunk_range=chunk_range) as read_type:
            assert read_type == "cache_hit"
        mock_nursery.start.assert_called_once_with(stream.run, expected_start)

    trio.run(_run)


def test_read_lifecycle_header_cache_hits_remain_passive() -> None:
    import trio_util

    stream = _make_stream(cache_hit=True)
    stream.is_streaming = trio_util.AsyncBool(False)
    stream._start_lock = trio.Lock()
    stream.nursery = MagicMock()
    _set_previous_read(
        stream,
        position=0,
        size=64 * 1024,
        read_type="cache_hit",
    )
    chunk_range = stream.chunker.get_chunk_range(
        position=64 * 1024,
        size=64 * 1024,
    )

    async def _run() -> None:
        async with stream.read_lifecycle(chunk_range=chunk_range) as read_type:
            assert read_type == "cache_hit"
        stream.nursery.start.assert_not_called()

    trio.run(_run)


def test_read_lifecycle_body_read_starts_worker() -> None:
    import trio_util

    stream = _make_stream(cache_hit=False)
    stream.is_streaming = trio_util.AsyncBool(False)
    stream._start_lock = trio.Lock()

    mock_nursery = MagicMock()

    async def mock_start(fn, pos):
        pass

    mock_nursery.start = MagicMock(side_effect=mock_start)
    stream.nursery = mock_nursery

    _set_previous_read(stream, position=stream.config.header_size, size=128 * 1024)
    next_pos = stream.config.header_size + 128 * 1024
    chunk_range = stream.chunker.get_chunk_range(position=next_pos, size=128 * 1024)

    async def _run() -> None:
        async with stream.read_lifecycle(chunk_range=chunk_range) as read_type:
            assert read_type == "body_read"
        mock_nursery.start.assert_called_once_with(stream.run, next_pos)

    trio.run(_run)
