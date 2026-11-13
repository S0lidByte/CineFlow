"""
Unit and integration regression tests for sustained playback, container interleaving,
adaptive prefetch, and cache eviction safety.
"""

from pathlib import Path

import pytest
import trio
from kink import di

from program.services.streaming.adaptive_prefetch import (
    AdaptivePrefetchConfig,
    AdaptivePrefetchManager,
)
from program.services.streaming.cache import Cache, CacheConfig, CacheEntry
from program.services.streaming.chunker import ChunkCacheNotifier, Chunker
from program.services.streaming.config import Config
from program.services.streaming.file_metadata import FileMetadata
from program.services.streaming.media_stream import MediaStream, RecentReads

MIB = 1024 * 1024
CHUNK = 4 * MIB


def test_adaptive_prefetch_container_interleave_tracking():
    """Verify that alternating video and audio interleaved reads do not trigger false seeks."""
    config = AdaptivePrefetchConfig(
        chunk_size_bytes=CHUNK,
        sequential_tolerance_bytes=1310720,  # 1.25 MB
        interleave_tolerance_bytes=16 * MIB,  # 16 MB
    )
    mgr = AdaptivePrefetchManager(config=config, initial_bitrate=50_000_000)

    # Initial read at 20 MB (Video Keyframe 1)
    pos_v1 = 20 * MIB
    size_128k = 128 * 1024
    t = 1.0

    mgr.record_read(pos_v1, pos_v1 + size_128k - 1, current_time=t)
    assert mgr.current_generation == 0

    # Demuxer reads Audio Packet 1 interleaved 2.5 MB behind video (at 17.5 MB)
    pos_a1 = int(17.5 * MIB)
    t += 0.05
    is_seq_a1 = mgr.record_read(pos_a1, pos_a1 + size_128k - 1, current_time=t)
    assert is_seq_a1 is True
    assert (
        mgr.current_generation == 0
    ), "Audio interleave must not bump generation token"
    assert mgr.seek_count == 0

    # Demuxer resumes Video Block 2 (contiguous with Video Block 1 after audio interleave)
    pos_v2 = pos_v1 + size_128k
    t += 0.05
    is_seq_v2 = mgr.record_read(pos_v2, pos_v2 + size_128k - 1, current_time=t)
    assert is_seq_v2 is True
    assert (
        mgr.current_generation == 0
    ), "Video resumption must not bump generation token"
    assert mgr.seek_count == 0

    # Continuous rapid reads accumulating bytes for bitrate estimation
    curr = pos_v2 + size_128k
    for _ in range(30):
        t += 0.02
        mgr.record_read(curr, curr + size_128k - 1, current_time=t)
        curr += size_128k

    assert mgr.current_generation == 0
    assert (
        mgr.get_effective_bitrate() > 0
    ), "Bitrate estimation must remain positive and active"


def test_adaptive_prefetch_true_seek_detection():
    """Verify that jumps beyond interleave tolerance (>32 MB) correctly trigger true seeks."""
    config = AdaptivePrefetchConfig(
        chunk_size_bytes=CHUNK,
        interleave_tolerance_bytes=16 * MIB,
    )
    mgr = AdaptivePrefetchManager(config=config, initial_bitrate=60_000_000)

    mgr.record_read(0, 1048575, current_time=10.0)
    mgr.record_read(1048576, 2097151, current_time=10.5)
    assert mgr.current_generation == 0

    # User seeks forward 60 MB
    seek_target = 60 * MIB
    is_seq = mgr.record_read(seek_target, seek_target + 1048575, current_time=11.0)
    assert is_seq is False
    assert mgr.current_generation == 1, "True seek must advance generation token"
    assert mgr.seek_count == 1
    assert mgr.should_abort_prefetch(0) is True
    assert mgr.should_abort_prefetch(1) is False


def test_sequential_cache_playback_container_interleaving():
    """Verify that _is_sequential_cache_playback recognizes interleaved video/audio reads."""
    di[ChunkCacheNotifier] = ChunkCacheNotifier()

    stream = MediaStream.__new__(MediaStream)
    stream.config = Config(
        chunk_size=CHUNK,
        activity_timeout_seconds=30,
        chunk_wait_timeout_seconds=30,
        connect_timeout_seconds=10,
        sequential_read_tolerance_blocks=10,
        prefetch_chunks=16,
    )
    stream.chunker = Chunker(
        cache_key="movie.mkv",
        chunk_size=CHUNK,
        header_size=CHUNK,
        footer_size=CHUNK,
        file_size=500 * MIB,
    )
    stream.file_metadata = FileMetadata(
        original_filename="movie.mkv",
        file_size=500 * MIB,
        path="/movie.mkv",
    )
    stream.recent_reads = RecentReads()

    # Simulate playhead at 50 MB
    stream._consumer_end = 50 * MIB

    # Read 1: Interleaved audio packet at 48 MB (2 MB behind consumer playhead)
    range_audio = stream.chunker.get_chunk_range(position=48 * MIB, size=128 * 1024)
    assert stream._is_sequential_cache_playback(range_audio) is True

    # Read 2: Video cluster at 53 MB (3 MB ahead of consumer playhead)
    range_video = stream.chunker.get_chunk_range(position=53 * MIB, size=128 * 1024)
    assert stream._is_sequential_cache_playback(range_video) is True

    # Read 3: Out-of-bounds isolated probe at 300 MB (far outside 16 MB tolerance)
    range_probe = stream.chunker.get_chunk_range(position=300 * MIB, size=128 * 1024)
    assert stream._is_sequential_cache_playback(range_probe) is False


def test_cache_eviction_immunity_for_sustained_prefetch_buffer(tmp_path):
    """Verify that chunks ahead of the playhead remain protected and are never evicted under pressure."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    # Cap cache at 20 MB so 5 chunks (20 MB) + 4 MB need puts it over budget
    cfg = CacheConfig(
        cache_dir=cache_dir,
        max_size_bytes=20 * MIB,
    )
    cache = Cache(cfg)

    stream_id = "test_stream:1"
    cache_key = "movie.mkv"

    # Simulate 5 chunks in cache (each 4 MB = 20 MB total)
    # Chunks: 0, 4MB, 8MB, 12MB, 16MB
    chunks = [0, 4 * MIB, 8 * MIB, 12 * MIB, 16 * MIB]
    for start in chunks:
        k = cache._key(cache_key, start)
        entry = CacheEntry(
            key=k,
            cache_key=cache_key,
            start=start,
            size=4 * MIB,
            mtime=100.0,
            tier="warm",
        )
        cache._index[k] = entry
        cache._total_bytes += entry.size
        cache._by_path.setdefault(cache_key, []).append(start)

    # Reconcile playhead at 8 MB with lookahead 16 MB (protects 8 MB, 12 MB, 16 MB)
    # Lookback 4 MB (protects 4 MB) -> Chunk 0 is obsolete
    cache.reconcile_stream_playhead(
        stream_id=stream_id,
        cache_key=cache_key,
        playhead_byte=8 * MIB,
        lookback_bytes=4 * MIB,
        lookahead_bytes=16 * MIB,
        lease_seconds=120.0,
        header_bytes=0,
    )

    # Verify Chunk 0 is not protected, but Chunks 4MB, 8MB, 12MB, 16MB are protected
    assert not cache.is_protected(cache_key, 0)
    assert cache.is_protected(cache_key, 4 * MIB)
    assert cache.is_protected(cache_key, 8 * MIB)
    assert cache.is_protected(cache_key, 12 * MIB)
    assert cache.is_protected(cache_key, 16 * MIB)

    # Trigger eviction to free 4 MB
    # Chunk 0 should be evicted, but leased chunks must NOT be evicted
    async def run_evict():
        return await cache._evict_lru(need_bytes=4 * MIB)

    trio.run(run_evict)
    k0 = cache._key(cache_key, 0)
    k4 = cache._key(cache_key, 4 * MIB)
    k8 = cache._key(cache_key, 8 * MIB)
    k12 = cache._key(cache_key, 12 * MIB)
    k16 = cache._key(cache_key, 16 * MIB)

    assert k0 not in cache._index, "Obsolete past chunk should be evicted"
    assert k4 in cache._index, "Lookback chunk must remain in cache"
    assert k8 in cache._index, "Playhead chunk must remain in cache"
    assert k12 in cache._index, "Prefetched chunk must remain in cache"
    assert k16 in cache._index, "Prefetched chunk must remain in cache"
