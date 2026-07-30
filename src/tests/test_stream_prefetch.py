"""Tests for sequential playhead prefetch chunk selection."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from kink import di
from ordered_set import OrderedSet

from program.services.streaming.cache import Cache
from program.services.streaming.chunker import ChunkCacheNotifier, Chunker


@pytest.fixture(autouse=True)
def _register_streaming_di():
    di[ChunkCacheNotifier] = ChunkCacheNotifier()
    cache_mock = MagicMock()
    cache_mock.has.return_value = False
    di[Cache] = cache_mock
    yield
    if ChunkCacheNotifier in di:
        del di[ChunkCacheNotifier]
    if Cache in di:
        del di[Cache]


def _chunker(*, file_size: int = 10 * 1024 * 1024, chunk_size: int = 1024 * 1024) -> Chunker:
    return Chunker(
        cache_key="movie.mkv",
        chunk_size=chunk_size,
        header_size=256 * 1024,
        footer_size=256 * 1024,
        file_size=file_size,
    )


def test_prefetch_returns_next_uncached_chunks() -> None:
    chunker = _chunker()
    after_end = chunker.header_size + chunker.chunk_size - 1
    ahead = chunker.get_prefetch_uncached(after_end=after_end, count=3)

    assert len(ahead) == 3
    assert ahead[0].start == after_end + 1
    assert ahead[0].start >= chunker.header_size
    assert ahead[-1].end < chunker.footer_start


def test_prefetch_disabled_when_count_zero() -> None:
    chunker = _chunker()
    ahead = chunker.get_prefetch_uncached(after_end=chunker.header_size, count=0)
    assert ahead == OrderedSet([])


def test_prefetch_empty_near_footer() -> None:
    chunker = _chunker()
    ahead = chunker.get_prefetch_uncached(
        after_end=chunker.footer_start - 1,
        count=12,
    )
    assert len(ahead) == 0
