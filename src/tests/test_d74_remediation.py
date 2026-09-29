from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import trio
from kink import di

from program.services.streaming.cache import Cache, CacheConfig
from program.services.streaming.chunker import Chunk, ChunkCacheNotifier, Chunker
from program.services.streaming.config import Config
from program.services.streaming.file_metadata import FileMetadata
from program.services.streaming.media_stream import MediaStream


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

    file_metadata = MagicMock()
    file_metadata.path = "Starship.Troopers.1997.mkv"
    file_metadata.original_filename = "Starship.Troopers.1997.mkv"
    file_metadata.file_size = 62 * 1024 * 1024 * 1024

    stream = MediaStream.__new__(MediaStream)
    stream.stream_id = "test_d74_stream"
    stream.file_metadata = file_metadata
    stream.config = Config(
        chunk_size=chunk_size,
        activity_timeout_seconds=60,
        prefetch_chunks=48,
        chunk_wait_timeout_seconds=2,
        connect_timeout_seconds=2,
        sequential_read_tolerance_blocks=10,
        scan_tolerance_blocks=25,
    )
    stream.chunker = Chunker(
        cache_key=stream.file_metadata.original_filename,
        chunk_size=stream.config.chunk_size,
        header_size=stream.config.header_size,
        footer_size=1024 * 16,
        file_size=62 * 1024 * 1024 * 1024,
    )
    return stream, cache


def test_defect_a_dynamic_lookahead_protects_full_prefetch_horizon(
    tmp_path: Path,
) -> None:
    """DEFECT A REMEDIATION: Prove chunks across the full 384 MB prefetch horizon

    remain protected by the sliding stream lease and resist cache eviction.
    """
    chunk_size = 8 * 1024 * 1024  # 8 MB
    # Capacity for 35 chunks (280 MB), so 48 chunks (384 MB) exceed capacity
    cache_capacity = 35 * chunk_size
    stream, cache = _make_stream(
        tmp_path, chunk_size=chunk_size, cache_max_bytes=cache_capacity
    )

    async def _run() -> None:
        playhead_byte = 0
        prefetch_window_chunks = 48  # 384 MB

        chunks: list[Chunk] = []
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

        # Call reconcile_stream_playhead with default lookahead (now 384 MB)
        cache.reconcile_stream_playhead(
            stream_id=stream.stream_id,
            cache_key=stream.file_metadata.original_filename,
            playhead_byte=playhead_byte,
        )

        # Verify that chunk 30 (240 MB) AND chunk 40 (320 MB) are BOTH protected!
        chunk_30 = chunks[30]
        key_30 = cache._key(chunk_30.cache_key, chunk_30.start)
        assert cache._is_entry_protected(key_30) is True

        chunk_40 = chunks[40]
        key_40 = cache._key(chunk_40.cache_key, chunk_40.start)
        assert (
            cache._is_entry_protected(key_40) is True
        ), "Chunk at 320 MB must be protected by default 384 MB lookahead"

        chunk_47 = chunks[47]
        key_47 = cache._key(chunk_47.cache_key, chunk_47.start)
        assert (
            cache._is_entry_protected(key_47) is True
        ), "Chunk 47 at 376 MB must be protected within 384 MB horizon"

        # Attempt eviction under pressure: all active stream chunks must refuse eviction
        initial_entries = len(cache._index)
        await cache._evict_lru(need_bytes=0)
        assert (
            len(cache._index) == initial_entries
        ), "All active playback chunks must resist eviction"
        assert cache.eviction_refusals > 0

    trio.run(_run)


def test_defect_b_eviction_invalidates_emitter_and_is_cached(tmp_path: Path) -> None:
    """DEFECT B REMEDIATION: Prove eviction invalidates ChunkCacheNotifier and Chunk.is_cached,

    restoring uncached_chunks and preventing silent un-cached HTTP fallback loops.
    """
    chunk_size = 8 * 1024 * 1024
    stream, cache = _make_stream(
        tmp_path, chunk_size=chunk_size, cache_max_bytes=10 * 1024 * 1024
    )

    async def _run() -> None:
        chunk = Chunk(
            cache_key="test_file.mkv",
            index=1,
            start=0,
            end=chunk_size - 1,
        )
        chunk_data = b"B" * chunk_size

        # 1. Chunk is cached
        await cache.put(chunk.cache_key, chunk.start, chunk_data)
        chunk.emit_cache_signal()
        assert chunk.is_cached.value is True

        # 2. Release lease and evict under pressure
        cache.release_lease(
            stream_id=stream.stream_id, cache_key=chunk.cache_key, start=chunk.start
        )
        await cache._evict_lru(need_bytes=20 * 1024 * 1024)

        # 3. VERIFY: Physical cache confirms chunk is gone
        assert not cache.has(chunk.cache_key, chunk.start, chunk.end)

        # 4. VERIFY: ChunkCacheNotifier reflects eviction immediately
        notifier = di[ChunkCacheNotifier]
        emitter = notifier.emitters.get((chunk.cache_key, chunk.index))
        assert emitter is not None
        assert (
            emitter.value is False
        ), "ChunkCacheNotifier emitter must be reset to False on eviction"

        # 5. VERIFY: Chunk.is_cached reflects actual cache residency (False)
        assert (
            chunk.is_cached.value is False
        ), "Chunk.is_cached.value must be False when chunk is evicted"

        # 6. VERIFY: uncached_chunks in a new ChunkRange includes the evicted chunk
        chunk_range = stream.chunker.get_chunk_range(position=0, size=128 * 1024)
        assert (
            len(chunk_range.uncached_chunks) == 1
        ), "uncached_chunks must NOT be empty for an evicted chunk"
        assert next(iter(chunk_range.uncached_chunks)).start == 0

    trio.run(_run)


def test_defect_c_unaligned_vfs_probe_discovers_chunk_and_hits(tmp_path: Path) -> None:
    """DEFECT C REMEDIATION: Prove Cache.get probe fallback normalizes unaligned VFS offsets

    to chunk start boundaries, discovers the file on disk, rebuilds index, and returns the slice.
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
        cache_key = "starship_troopers.mkv"
        chunk_start = 0
        full_chunk_data = b"P" * chunk_size

        # 1. Put chunk at start 0
        await cache.put(cache_key, chunk_start, full_chunk_data)

        # 2. Simulate index desync by clearing in-memory index
        async with cache.locks():
            with cache._thread_lock:
                cache._index.clear()
                cache._by_path.clear()

        # 3. Read an unaligned 128 KB block: [131072, 262143]
        vfs_start = 128 * 1024
        vfs_end = vfs_start + 128 * 1024 - 1

        result = await cache.get(cache_key, vfs_start, vfs_end)

        # 4. VERIFY: probe normalizes to chunk boundary (0), reads file, and returns correct 128 KB slice
        assert len(result) == 128 * 1024
        assert result == b"P" * (128 * 1024)

        # 5. VERIFY: in-memory index was reconstructed with chunk_start = 0
        chunk_k = cache._key(cache_key, chunk_start)
        assert chunk_k in cache._index
        assert cache._index[chunk_k].start == 0
        assert cache._index[chunk_k].size == chunk_size

    trio.run(_run)


def test_defect_c_probe_exact_and_miss_safety(tmp_path: Path) -> None:
    """DEFECT C SAFETY: Prove probe correctly handles exact boundaries and clean misses."""
    chunk_size = 8 * 1024 * 1024
    cache = Cache(
        CacheConfig(
            cache_dir=tmp_path / "cache",
            max_size_bytes=100 * 1024 * 1024,
            metrics_enabled=False,
        )
    )

    async def _run() -> None:
        cache_key = "exact_test.mkv"
        # Non-existent file returns empty cleanly
        miss_res = await cache.get(cache_key, 0, 100)
        assert miss_res == b""

        # Put chunk at 8MB boundary
        chunk_start = 8 * 1024 * 1024
        data = b"M" * chunk_size
        await cache.put(cache_key, chunk_start, data)

        # Clear index
        async with cache.locks():
            with cache._thread_lock:
                cache._index.clear()
                cache._by_path.clear()

        # Read exact boundary [8MB, 8MB + 1023]
        res = await cache.get(cache_key, chunk_start, chunk_start + 1023)
        assert len(res) == 1024
        assert res == b"M" * 1024

    trio.run(_run)


def test_multistream_independent_lease_protection_under_pressure(
    tmp_path: Path,
) -> None:
    """MULTI-STREAM REMEDIATION VERIFICATION: Ensure multiple concurrent streams
    maintain independent sliding leases under cache pressure.
    """
    chunk_size = 8 * 1024 * 1024  # 8 MB
    # Budget for 10 chunks (80 MB)
    cache = Cache(
        CacheConfig(
            cache_dir=tmp_path / "cache",
            max_size_bytes=10 * chunk_size,
            metrics_enabled=False,
        )
    )
    di[Cache] = cache

    async def _run() -> None:
        stream_a_id = "stream_a"
        stream_b_id = "stream_b"
        key_a = "movie_a.mkv"
        key_b = "movie_b.mkv"

        # Stream A caches 4 chunks [0, 1, 2, 3] (32 MB)
        for i in range(4):
            data_a = b"A" * chunk_size
            await cache.put(key_a, i * chunk_size, data_a, stream_id=stream_a_id)

        # Stream B caches 4 chunks [10, 11, 12, 13] (32 MB)
        for i in range(10, 14):
            data_b = b"B" * chunk_size
            await cache.put(key_b, i * chunk_size, data_b, stream_id=stream_b_id)

        # Also write 4 unleased/background chunks [20, 21, 22, 23] (32 MB) -> Total 96 MB > 80 MB
        for i in range(20, 24):
            data_bg = b"G" * chunk_size
            await cache.put("background.mkv", i * chunk_size, data_bg)

        # Reconcile playheads:
        # Stream A at 0, protecting 0..3
        cache.reconcile_stream_playhead(
            stream_id=stream_a_id,
            cache_key=key_a,
            playhead_byte=0,
            lookahead_bytes=384 * 1024 * 1024,
        )
        # Stream B at 10 * chunk_size, protecting 10..13
        cache.reconcile_stream_playhead(
            stream_id=stream_b_id,
            cache_key=key_b,
            playhead_byte=10 * chunk_size,
            lookahead_bytes=384 * 1024 * 1024,
        )

        # Evict under pressure: target 30 MB
        await cache._evict_lru(need_bytes=30 * 1024 * 1024)

        # Actively leased chunks of Stream A and Stream B MUST remain intact
        for i in range(4):
            assert cache.has(key_a, i * chunk_size, (i + 1) * chunk_size - 1) is True
        for i in range(10, 14):
            assert cache.has(key_b, i * chunk_size, (i + 1) * chunk_size - 1) is True

        # Unleased background chunks should be evicted first
        unleased_survivors = sum(
            1
            for i in range(20, 24)
            if cache.has("background.mkv", i * chunk_size, (i + 1) * chunk_size - 1)
        )
        assert (
            unleased_survivors < 4
        ), "Unprotected background chunks must be sacrificed first"

        # Stream A terminates and releases all leases
        cache.release_stream(stream_id=stream_a_id)

        # Further eviction should now sacrifice Stream A's chunks while protecting Stream B
        await cache._evict_lru(need_bytes=25 * 1024 * 1024)

        # Stream B's chunks MUST STILL remain fully protected
        for i in range(10, 14):
            assert cache.has(key_b, i * chunk_size, (i + 1) * chunk_size - 1) is True

    trio.run(_run)


def test_four_stream_independent_lease_protection_under_pressure(
    tmp_path: Path,
) -> None:
    """MULTI-STREAM 4-STREAM VERIFICATION (Phase 3): Ensure 4 concurrent streams maintain

    independent leases and playheads under constrained cache capacity.
    """
    chunk_size = 8 * 1024 * 1024  # 8 MB
    # Capacity for 16 chunks (128 MB)
    cache = Cache(
        CacheConfig(
            cache_dir=tmp_path / "cache4",
            max_size_bytes=16 * chunk_size,
            metrics_enabled=False,
        )
    )
    di[Cache] = cache

    async def _run() -> None:
        streams = [f"stream_{i}" for i in range(1, 5)]
        keys = [f"movie_{i}.mkv" for i in range(1, 5)]

        # Each of the 4 streams caches 3 chunks (3 * 8MB = 24MB per stream, 96MB total)
        for s_idx, (s_id, key) in enumerate(zip(streams, keys, strict=True)):
            for c_idx in range(3):
                chunk_start = (s_idx * 10 + c_idx) * chunk_size
                await cache.put(
                    key,
                    chunk_start,
                    b"D" * chunk_size,
                    stream_id=s_id,
                )

        # Write 6 unleased background chunks (48 MB) -> total 144 MB > 128 MB budget
        for i in range(6):
            await cache.put("background.mkv", i * chunk_size, b"X" * chunk_size)

        # Reconcile playhead for each stream
        for s_idx, (s_id, key) in enumerate(zip(streams, keys, strict=True)):
            cache.reconcile_stream_playhead(
                stream_id=s_id,
                cache_key=key,
                playhead_byte=s_idx * 10 * chunk_size,
                lookahead_bytes=384 * 1024 * 1024,
            )

        # Evict under pressure: target 40 MB
        await cache._evict_lru(need_bytes=40 * 1024 * 1024)

        # Invariant: All 12 chunks across all 4 streams MUST remain 100% protected
        for s_idx, (s_id, key) in enumerate(zip(streams, keys, strict=True)):
            for c_idx in range(3):
                chunk_start = (s_idx * 10 + c_idx) * chunk_size
                assert (
                    cache.has(key, chunk_start, chunk_start + chunk_size - 1) is True
                ), f"Stream {s_id} chunk {c_idx} was evicted while actively leased"

        # Background chunks were evicted
        bg_survivors = sum(
            1
            for i in range(6)
            if cache.has("background.mkv", i * chunk_size, (i + 1) * chunk_size - 1)
        )
        assert bg_survivors < 6, "Background unleased chunks must be evicted first"

        # Close stream 1: releasing stream 1 does not affect streams 2, 3, 4
        cache.release_stream(stream_id=streams[0])

        # Evict again: target 20 MB
        await cache._evict_lru(need_bytes=20 * 1024 * 1024)

        # Streams 2, 3, 4 MUST remain 100% intact
        for s_idx in range(1, 4):
            s_id = streams[s_idx]
            key = keys[s_idx]
            for c_idx in range(3):
                chunk_start = (s_idx * 10 + c_idx) * chunk_size
                assert (
                    cache.has(key, chunk_start, chunk_start + chunk_size - 1) is True
                ), f"Stream {s_id} chunk {c_idx} lost protection when stream 1 closed"

        # Close stream 2: streams 3 and 4 remain intact
        cache.release_stream(stream_id=streams[1])
        await cache._evict_lru(need_bytes=20 * 1024 * 1024)
        for s_idx in range(2, 4):
            s_id = streams[s_idx]
            key = keys[s_idx]
            for c_idx in range(3):
                chunk_start = (s_idx * 10 + c_idx) * chunk_size
                assert (
                    cache.has(key, chunk_start, chunk_start + chunk_size - 1) is True
                ), f"Stream {s_id} chunk {c_idx} lost protection when stream 2 closed"

    trio.run(_run)
