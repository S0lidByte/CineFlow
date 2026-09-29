"""
D74 Forensic Verification & Regression Test Suite
Deterministic reproduction and verification for:
- Phase 1: Stale is_cached emitter surviving eviction and causing synchronous fallback loop
- Phase 2: 256 MB stream lease lookahead vs 384 MB prefetch window gap
- Phase 3: Chunk.is_cached emitter lifecycle and lack of invalidation on eviction
- Phase 4: Cache.get filesystem probe key alignment for arbitrary VFS offsets
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import trio
from kink import di
from ordered_set import OrderedSet

from program.services.streaming.adaptive_prefetch import (
    AdaptivePrefetchConfig,
    AdaptivePrefetchManager,
)
from program.services.streaming.cache import (
    Cache,
    CacheConfig,
    CacheEntry,
)
from program.services.streaming.chunker import Chunk, ChunkCacheNotifier, Chunker
from program.services.streaming.config import Config
from program.services.streaming.file_metadata import FileMetadata
from program.services.streaming.media_stream import MediaStream
from program.services.streaming.recent_reads import Read, RecentReads


@pytest.fixture(autouse=True)
def _register_streaming_di():
    notifier = ChunkCacheNotifier()
    notifier.emitters.clear()
    di[ChunkCacheNotifier] = notifier
    yield
    if ChunkCacheNotifier in di:
        del di[ChunkCacheNotifier]
    if Cache in di:
        del di[Cache]


def _make_stream(
    tmp_path: Path,
    *,
    file_size: int = 1000 * 1024 * 1024,
    chunk_size: int = 8 * 1024 * 1024,
    cache_max_bytes: int = 500 * 1024 * 1024,
) -> tuple[MediaStream, Cache]:
    cache_dir = tmp_path / "cache"
    cache = Cache(
        CacheConfig(
            cache_dir=cache_dir,
            max_size_bytes=cache_max_bytes,
            metrics_enabled=False,
        )
    )
    di[Cache] = cache

    stream = MediaStream.__new__(MediaStream)
    stream.stream_id = "test_stream_d74"
    stream.config = Config(
        chunk_size=chunk_size,
        activity_timeout_seconds=60,
        chunk_wait_timeout_seconds=5,
        connect_timeout_seconds=5,
        sequential_read_tolerance_blocks=10,
        scan_tolerance_blocks=25,
        prefetch_chunks=48,
    )
    stream.file_metadata = FileMetadata(
        original_filename="Starship.Troopers.1997.mkv",
        file_size=file_size,
        path="/movies/Starship.Troopers.1997.mkv",
    )
    stream.recent_reads = RecentReads()
    footer_size = 1024 * 16
    stream.chunker = Chunker(
        cache_key=stream.file_metadata.original_filename,
        chunk_size=stream.config.chunk_size,
        header_size=stream.config.header_size,
        footer_size=footer_size,
        file_size=file_size,
    )
    stream.adaptive_prefetch = AdaptivePrefetchManager(
        AdaptivePrefetchConfig(
            chunk_size_bytes=chunk_size,
            max_window_chunks=48,
        )
    )
    stream.adaptive_prefetch.set_metadata_bitrate(80_000_000)
    stream.fh = 1
    stream.enable_tracing = False
    return stream, cache


def test_phase3_is_cached_emitter_lifecycle(tmp_path: Path) -> None:
    """PHASE 3 VERIFICATION: Prove whether is_cached emitter is invalidated on eviction.

    Hypothesis: Chunk.emit_cache_signal() latches _emitter.value = True permanently.
    When a chunk is subsequently evicted, chunk.is_cached remains True,
    bypassing Cache.has() checks and advertising stale cached state.
    """
    chunk_size = 8 * 1024 * 1024
    stream, cache = _make_stream(
        tmp_path, chunk_size=chunk_size, cache_max_bytes=16 * 1024 * 1024
    )

    async def _run() -> None:
        chunk = Chunk(
            cache_key=stream.file_metadata.original_filename,
            index=1,
            start=0,
            end=chunk_size - 1,
        )
        chunk_data = b"C" * chunk_size

        # 1. Initially not cached
        assert not chunk.is_cached.value

        # 2. Put chunk into cache and emit signal
        await cache.put(chunk.cache_key, chunk.start, chunk_data)
        chunk.emit_cache_signal()

        # 3. Verified cached
        assert chunk.is_cached.value is True
        assert cache.has(chunk.cache_key, chunk.start, chunk.end) is True

        # 4. Evict chunk under pressure
        await cache._evict_lru(need_bytes=20 * 1024 * 1024)

        # Physical cache confirms chunk is gone
        assert cache.has(chunk.cache_key, chunk.start, chunk.end) is False
        assert cache._key(chunk.cache_key, chunk.start) not in cache._index

        # 5. VERIFY SIGNAL STATE:
        # is_cached correctly reflects eviction (False)
        stale_signal = chunk.is_cached.value
        assert (
            stale_signal is False
        ), "Defect resolved: is_cached must reflect eviction immediately"

    trio.run(_run)


def test_phase2_prefetch_lookahead_gap_eviction(tmp_path: Path) -> None:
    """PHASE 2 VERIFICATION: Prove that chunks in the full 384 MB prefetch horizon
    remain protected under 384 MB lookahead.
    """
    chunk_size = 8 * 1024 * 1024  # 8 MB production chunks
    # 48 chunks = 384 MB prefetch horizon.
    # Set cache capacity to hold 35 chunks (280 MB), so chunks beyond capacity suffer eviction pressure.
    cache_capacity = 35 * chunk_size
    stream, cache = _make_stream(
        tmp_path, chunk_size=chunk_size, cache_max_bytes=cache_capacity
    )

    async def _run() -> None:
        playhead_byte = 0
        lease_lookahead = 384 * 1024 * 1024  # Remediated to 384 MB (48 chunks)
        prefetch_window_chunks = 48  # 384 MB (48 chunks)

        chunks: list[Chunk] = []
        # 1. Prefetch downloads chunks 0 through 47
        for i in range(prefetch_window_chunks):
            chunk = Chunk(
                cache_key=stream.file_metadata.original_filename,
                index=i + 1,
                start=i * chunk_size,
                end=(i + 1) * chunk_size - 1,
            )
            chunks.append(chunk)
            data = f"CHUNK_{i:02d}".encode() * (chunk_size // 8)
            await cache.put(
                chunk.cache_key, chunk.start, data, stream_id=stream.stream_id
            )
            chunk.emit_cache_signal()

        # 2. Player performs read at playhead_byte = 0, which triggers reconcile_stream_playhead()
        cache.reconcile_stream_playhead(
            stream_id=stream.stream_id,
            cache_key=stream.file_metadata.original_filename,
            playhead_byte=playhead_byte,
            lookahead_bytes=lease_lookahead,
        )

        # Inspect protection status:
        # Chunks within [0, 384 MB] (chunks 0..47) should all be protected
        chunk_30 = chunks[30]  # 240 MB
        key_30 = cache._key(chunk_30.cache_key, chunk_30.start)
        assert cache._is_entry_protected(key_30) is True

        chunk_40 = chunks[40]  # 320 MB
        key_40 = cache._key(chunk_40.cache_key, chunk_40.start)
        assert (
            cache._is_entry_protected(key_40) is True
        ), "Chunk at 320 MB MUST be protected under 384 MB lookahead"

        # 3. Simulate cache pressure requiring eviction
        # Total cached bytes is 48 * 8MB = 384 MB > 280 MB budget.
        # Run LRU eviction: all 48 chunks are protected, so eviction is refused
        await cache._evict_lru(need_bytes=0)
        assert cache.eviction_refusals > 0

        # 4. Prove that chunk 40 remains in cache and protected
        assert key_40 in cache._index, "Protected chunk was preserved"
        assert cache.has(chunk_40.cache_key, chunk_40.start, chunk_40.end)

    trio.run(_run)


def test_phase1_deterministic_reproduction_fallback_loop(tmp_path: Path) -> None:
    """PHASE 1 VERIFICATION: Prove that eviction properly invalidates the notifier,
    restoring uncached_chunks and preventing the fallback loop.
    """
    chunk_size = 8 * 1024 * 1024
    stream, cache = _make_stream(
        tmp_path, chunk_size=chunk_size, cache_max_bytes=10 * 1024 * 1024
    )

    async def _run() -> None:
        vfs_read_size = 128 * 1024  # 128 KB VFS block
        chunk_range_init = stream.chunker.get_chunk_range(
            position=10 * chunk_size, size=vfs_read_size
        )
        target_chunk = next(iter(chunk_range_init.chunks))
        target_data = b"TARGET_DATA_8MB!" * (chunk_size // 16)

        # 1. Chunk is downloaded & put in cache
        await cache.put(
            target_chunk.cache_key,
            target_chunk.start,
            target_data,
            stream_id=stream.stream_id,
        )

        # 2. Access is_cached
        assert target_chunk.is_cached.value is True

        # 3. Release lease
        cache.release_lease(
            stream_id=stream.stream_id,
            cache_key=target_chunk.cache_key,
            start=target_chunk.start,
        )
        assert not cache._is_entry_protected(
            cache._key(target_chunk.cache_key, target_chunk.start)
        )

        # 4. Cache pressure evicts target_chunk
        await cache._evict_lru(need_bytes=20 * 1024 * 1024)
        assert (
            cache._key(target_chunk.cache_key, target_chunk.start) not in cache._index
        )

        # 5. Cached state inspected after eviction:
        # Notifier reflects eviction immediately
        notifier = di[ChunkCacheNotifier]
        assert (target_chunk.cache_key, target_chunk.index) in notifier.emitters
        assert (
            notifier.emitters[(target_chunk.cache_key, target_chunk.index)].value
            is False
        )

        # 6. Reader reaches the same chunk on a new read request
        chunk_range = stream.chunker.get_chunk_range(
            position=target_chunk.start, size=vfs_read_size
        )

        # PROOF: uncached_chunks contains the evicted chunk!
        assert (
            len(chunk_range.uncached_chunks) == 1
        ), "Reader correctly identifies chunk as uncached"

    trio.run(_run)


def test_phase4_cache_key_probe_alignment(tmp_path: Path) -> None:
    """PHASE 4 VERIFICATION: Determine whether Cache.get filesystem probe succeeds
    when probed with an arbitrary unaligned VFS start offset.
    """
    chunk_size = 8 * 1024 * 1024
    cache = Cache(
        CacheConfig(
            cache_dir=tmp_path / "cache",
            max_size_bytes=100 * 1024 * 1024,
            metrics_enabled=False,
        )
    )

    async def _run() -> None:
        cache_key = "test_probe.mkv"
        chunk_start = 0
        full_chunk_data = b"X" * chunk_size

        # Write chunk at chunk_start = 0
        await cache.put(cache_key, chunk_start, full_chunk_data)

        # Clear in-memory index to simulate index desync/recovery
        async with cache.locks():
            with cache._thread_lock:
                cache._index.clear()
                cache._by_path.clear()

        # Physical file is still on disk!
        disk_file = cache._file_for(cache._key(cache_key, chunk_start), tier="warm")
        assert disk_file.exists()

        # VFS read requests offset 128 KB (unaligned to chunk start):
        vfs_start = 128 * 1024
        vfs_end = vfs_start + 128 * 1024 - 1

        # Probe fallback in Cache.get() executes:
        result = await cache.get(cache_key, vfs_start, vfs_end)

        assert (
            len(result) == 128 * 1024
        ), "Cache.get probe fallback must find file on disk even when start offset is unaligned"
        assert result == b"X" * (128 * 1024)

    trio.run(_run)
