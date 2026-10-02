"""
Tests for playback-protected VFS cache architecture.
Verifies that active production playback data takes absolute priority over historical cache retention:
- Active reader refcounting & stream lease eviction immunity
- Eviction refusal when all cached data is under active lease
- Safe hot-to-warm demotion without eviction victimization
- Dynamic playhead lookback/lookahead lease reconciliation & seek pruning
- Teardown lease cleanup
- Adaptive prefetching decoupling from evictable historical cache pressure
- Cgroup container memory limit awareness
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest
import trio

from program.services.streaming.adaptive_prefetch import (
    AdaptivePrefetchConfig,
    AdaptivePrefetchManager,
)
from program.services.streaming.cache import (
    Cache,
    CacheConfig,
    CacheEntry,
    ChunkState,
)
from program.services.streaming.cache_sizing import (
    get_cgroup_memory_limit,
    resolve_cache_max_bytes,
)


def test_eviction_immunity_for_leased_chunks(tmp_path: Path) -> None:
    """Historical un-leased chunks are evicted under pressure while leased active playback chunks are spared."""
    cache_dir = tmp_path / "cache"
    # Small cache: 250 bytes max. Each chunk is 100 bytes.
    cache = Cache(
        CacheConfig(
            cache_dir=cache_dir,
            max_size_bytes=250,
            metrics_enabled=False,
        )
    )

    async def _run() -> None:
        chunk1 = b"1" * 100
        chunk2 = b"2" * 100
        chunk3 = b"3" * 100

        # Put chunk1 as historical data (no stream lease)
        await cache.put("titleA.mkv", 0, chunk1)
        # Put chunk2 with active stream lease
        await cache.put("titleB.mkv", 0, chunk2, stream_id="stream_B")

        key1 = cache._key("titleA.mkv", 0)
        key2 = cache._key("titleB.mkv", 0)

        assert key1 in cache._index
        assert key2 in cache._index

        # Putting chunk3 (100 bytes) pushes total to 300 bytes > 250 max.
        # Eviction must run. Since chunk1 is unleased and chunk2 is leased,
        # chunk1 MUST be evicted even if chunk2 was touched or inserted later.
        await cache.put("titleC.mkv", 0, chunk3)

        assert key1 not in cache._index
        assert key2 in cache._index
        assert cache._key("titleC.mkv", 0) in cache._index

        # Confirm chunk2 data is intact and readable
        read2 = await cache.get("titleB.mkv", 0, 99)
        assert read2 == chunk2

    trio.run(_run)


def test_eviction_refusal_when_all_chunks_protected(tmp_path: Path) -> None:
    """When all cached data is under active lease, eviction refuses to drop active playback data."""
    cache_dir = tmp_path / "cache"
    cache = Cache(
        CacheConfig(
            cache_dir=cache_dir,
            max_size_bytes=150,
            metrics_enabled=False,
        )
    )

    async def _run() -> None:
        chunk1 = b"A" * 100
        chunk2 = b"B" * 100

        await cache.put("titleA.mkv", 0, chunk1, stream_id="stream_1")
        await cache.put("titleA.mkv", 100, chunk2, stream_id="stream_1")

        # Cache is now 200 bytes > 150 bytes budget.
        # Both chunks are actively leased by stream_1.
        initial_refusals = cache.eviction_refusals

        # Trigger LRU eviction explicitly
        await cache._evict_lru()

        # Both chunks must remain protected
        assert cache._key("titleA.mkv", 0) in cache._index
        assert cache._key("titleA.mkv", 100) in cache._index
        assert cache.eviction_refusals > initial_refusals

    trio.run(_run)


def test_safe_hot_to_warm_demotion_preserves_leases(tmp_path: Path) -> None:
    """Hot-to-warm demotion moves chunk to disk tier while keeping active lease and avoiding eviction bias."""
    warm_dir = tmp_path / "warm"
    hot_dir = tmp_path / "hot"
    cache = Cache(
        CacheConfig(
            cache_dir=warm_dir,
            max_size_bytes=10 * 1024 * 1024,
            hot_dir=hot_dir,
            hot_max_size_bytes=150,
            metrics_enabled=False,
        )
    )

    async def _run() -> None:
        chunk1 = b"H" * 100
        chunk2 = b"W" * 100

        # Put chunk1 with active lease in hot tier
        await cache.put("film.mkv", 0, chunk1, stream_id="active_user")
        key1 = cache._key("film.mkv", 0)
        assert cache._index[key1].tier == "hot"
        assert cache._is_entry_protected(key1, cache._index[key1])

        # Put chunk2, overflowing hot tier (200 > 150)
        await cache.put("film.mkv", 100, chunk2)

        # chunk1 should be demoted to warm tier, but STILL leased and protected!
        assert cache._index[key1].tier == "warm"
        assert cache._is_entry_protected(key1, cache._index[key1])
        assert cache._index[key1].chunk_state == ChunkState.WARM

        # Can still read chunk1 cleanly
        data = await cache.get("film.mkv", 0, 99)
        assert data == chunk1

    trio.run(_run)


def test_reconcile_stream_playhead_updates_and_prunes_leases(tmp_path: Path) -> None:
    """Playhead reconciliation acquires leases for chunks in window and prunes obsolete ones on seek."""
    cache = Cache(
        CacheConfig(
            cache_dir=tmp_path / "cache",
            max_size_bytes=100 * 1024 * 1024,
            metrics_enabled=False,
        )
    )

    chunk_size = 8 * 1024 * 1024
    payload = b"Z" * 1024

    async def _run() -> None:
        # Populate several chunks for a movie
        # Chunk 0: 0MB, Chunk 1: 8MB, Chunk 2: 16MB, Chunk 10: 80MB
        for i in [0, 1, 2, 10]:
            await cache.put("test_movie.mkv", i * chunk_size, payload)

        key0 = cache._key("test_movie.mkv", 0)
        key1 = cache._key("test_movie.mkv", chunk_size)
        key2 = cache._key("test_movie.mkv", 2 * chunk_size)
        key10 = cache._key("test_movie.mkv", 10 * chunk_size)

        stream_id = "user_stream_123"

        # Reconcile playhead at 1MB (Chunk 0)
        cache.reconcile_stream_playhead(
            stream_id=stream_id,
            cache_key="test_movie.mkv",
            playhead_byte=1 * 1024 * 1024,
            lookahead_bytes=10 * 1024 * 1024,
            lookback_bytes=5 * 1024 * 1024,
        )

        # Chunk 0 and Chunk 1 fall within [0, 11MB]
        assert cache._is_entry_protected(key0, cache._index[key0])
        assert cache._is_entry_protected(key1, cache._index[key1])
        # Chunk 10 is far ahead, not leased
        assert not cache._is_entry_protected(key10, cache._index[key10])

        # User seeks to Chunk 10 (80MB)
        cache.reconcile_stream_playhead(
            stream_id=stream_id,
            cache_key="test_movie.mkv",
            playhead_byte=80 * 1024 * 1024,
            lookahead_bytes=10 * 1024 * 1024,
            lookback_bytes=5 * 1024 * 1024,
        )

        # Chunk 10 is now protected
        assert cache._is_entry_protected(key10, cache._index[key10])
        # Obsolete chunks 0 and 1 are no longer leased by this stream
        assert not cache._is_entry_protected(key0, cache._index[key0])
        assert not cache._is_entry_protected(key1, cache._index[key1])

    trio.run(_run)


def test_stream_release_cleans_up_leases(tmp_path: Path) -> None:
    """Closing/releasing a stream immediately revokes its leases."""
    cache = Cache(
        CacheConfig(
            cache_dir=tmp_path / "cache",
            max_size_bytes=100 * 1024 * 1024,
            metrics_enabled=False,
        )
    )

    async def _run() -> None:
        payload = b"D" * 100
        await cache.put("series.mkv", 0, payload, stream_id="active_stream")
        key = cache._key("series.mkv", 0)

        assert cache._is_entry_protected(key, cache._index[key])

        # Release stream
        cache.release_stream("active_stream")

        # Lease is revoked; entry is now evictable
        assert not cache._is_entry_protected(key, cache._index[key])

    trio.run(_run)


def test_reader_refcount_immunity(tmp_path: Path) -> None:
    """Active reader context manager grants eviction immunity even if no stream lease exists."""
    cache = Cache(
        CacheConfig(
            cache_dir=tmp_path / "cache",
            max_size_bytes=100 * 1024 * 1024,
            metrics_enabled=False,
        )
    )

    async def _run() -> None:
        payload = b"R" * 100
        await cache.put("file.mkv", 0, payload)
        key = cache._key("file.mkv", 0)

        assert not cache._is_entry_protected(key, cache._index[key])

        with cache.reading_chunk(key):
            # While reading, entry is protected
            assert cache._is_entry_protected(key, cache._index[key])

        # After exiting context manager, entry is unprotected
        assert not cache._is_entry_protected(key, cache._index[key])

    trio.run(_run)


def test_adaptive_prefetch_protected_cache_decoupling() -> None:
    """Prefetch window is NOT throttled when cache has high usage but low protected usage."""
    config = AdaptivePrefetchConfig(
        chunk_size_bytes=8 * 1024 * 1024,
        target_buffer_seconds=15.0,
        min_window_chunks=4,
        max_window_chunks=48,
    )
    # High bitrate: 80 Mbps
    manager = AdaptivePrefetchManager(config=config, initial_bitrate=80_000_000)

    # High total cache usage (98%), but protected playback cache is only 20%
    window = manager.calculate_window(
        cache_usage_pct=98.0,
        cache_protected_pct=20.0,
    )

    # Window should NOT be clamped to min_window_chunks (4) because protected usage is healthy
    assert window > config.min_window_chunks
    assert window >= 18

    # When protected cache itself is 96%, window MUST throttle to min_window_chunks
    throttled_window = manager.calculate_window(
        cache_usage_pct=98.0,
        cache_protected_pct=96.0,
    )
    assert throttled_window == config.min_window_chunks


def test_cgroup_memory_limit_detection(tmp_path: Path) -> None:
    """Cgroup memory limit clamps tmpfs cache budget to prevent container OOM."""
    # Test resolve_cache_max_bytes with simulated cgroup limit
    with patch(
        "program.services.streaming.cache_sizing.get_cgroup_memory_limit"
    ) as mock_cgroup:
        # Simulate 2 GiB container limit (2048 MB)
        # 2048 MB - 1536 MB headroom = 512 MB safe tmpfs cap
        mock_cgroup.return_value = 2048 * 1024 * 1024

        res = resolve_cache_max_bytes(
            cache_dir=Path("/dev/shm"),
            configured_mb=4096,
            tmpfs=True,
            tmpfs_hard_cap_bytes=1024 * 1024 * 1024,  # 1024 MB
        )

        assert res.effective_max_bytes == 512 * 1024 * 1024


def test_eviction_pressure_mixed_leases_and_refusal(tmp_path: Path) -> None:
    """Under sustained cache pressure, all unleased chunks are evicted first, and eviction refuses when all remaining are leased."""
    cache = Cache(
        CacheConfig(
            cache_dir=tmp_path / "cache",
            max_size_bytes=300,
            metrics_enabled=False,
        )
    )

    async def _run() -> None:
        # Put 2 unleased chunks (100B each)
        await cache.put("unleased1.mkv", 0, b"U1" * 50)
        await cache.put("unleased2.mkv", 0, b"U2" * 50)
        # Put 1 leased chunk (100B)
        await cache.put("leased1.mkv", 0, b"L1" * 50, stream_id="s1")

        k_u1 = cache._key("unleased1.mkv", 0)
        k_u2 = cache._key("unleased2.mkv", 0)
        k_l1 = cache._key("leased1.mkv", 0)

        assert k_u1 in cache._index
        assert k_u2 in cache._index
        assert k_l1 in cache._index
        assert cache._total_bytes == 300

        # Now put leased2 (100B) -> pushes total to 400 > 300 cap.
        # Should evict unleased1 (oldest unprotected).
        await cache.put("leased2.mkv", 0, b"L2" * 50, stream_id="s1")
        k_l2 = cache._key("leased2.mkv", 0)

        assert k_u1 not in cache._index
        assert k_u2 in cache._index
        assert k_l1 in cache._index
        assert k_l2 in cache._index
        assert cache._total_bytes == 300

        # Now put leased3 (100B) -> pushes total to 400 > 300 cap.
        # Should evict unleased2 (remaining unprotected).
        await cache.put("leased3.mkv", 0, b"L3" * 50, stream_id="s2")
        k_l3 = cache._key("leased3.mkv", 0)

        assert k_u2 not in cache._index
        assert k_l1 in cache._index
        assert k_l2 in cache._index
        assert k_l3 in cache._index
        assert cache._total_bytes == 300

        # Now ALL entries (l1, l2, l3) are leased!
        # Putting leased4 (100B) MUST refuse eviction rather than dropping active playback.
        initial_refusals = cache.eviction_refusals
        await cache.put("leased4.mkv", 0, b"L4" * 50, stream_id="s2")
        k_l4 = cache._key("leased4.mkv", 0)

        assert cache.eviction_refusals > initial_refusals
        # All 4 leased chunks must be preserved
        assert k_l1 in cache._index
        assert k_l2 in cache._index
        assert k_l3 in cache._index
        assert k_l4 in cache._index
        assert cache._total_bytes == 400  # Cap gracefully exceeded for playback safety

        # Confirm all 4 are intact
        assert await cache.get("leased1.mkv", 0, 99) == b"L1" * 50
        assert await cache.get("leased2.mkv", 0, 99) == b"L2" * 50
        assert await cache.get("leased3.mkv", 0, 99) == b"L3" * 50
        assert await cache.get("leased4.mkv", 0, 99) == b"L4" * 50

    trio.run(_run)


def test_hot_tier_pressure_unprotected_before_protected_demotion(
    tmp_path: Path,
) -> None:
    """Hot tier demotes unprotected hot chunks to warm before touching protected hot chunks."""
    cache = Cache(
        CacheConfig(
            cache_dir=tmp_path / "warm",
            max_size_bytes=1000,
            hot_dir=tmp_path / "hot",
            hot_max_size_bytes=200,
            metrics_enabled=False,
        )
    )

    async def _run() -> None:
        # Put protected chunk in hot (100B)
        await cache.put("prot.mkv", 0, b"P" * 100, stream_id="stream_live")
        # Put unprotected chunk in hot (100B)
        await cache.put("unprot.mkv", 0, b"U" * 100)

        kp = cache._key("prot.mkv", 0)
        ku = cache._key("unprot.mkv", 0)

        assert cache._index[kp].tier == "hot"
        assert cache._index[ku].tier == "hot"
        assert cache._hot_bytes == 200

        # Put third chunk into hot (100B) with protection -> pushes hot to 300 > 200 cap.
        # Demotion must pick unprot.mkv FIRST because prot.mkv is protected!
        await cache.put("new.mkv", 0, b"N" * 100, stream_id="stream_live2")
        kn = cache._key("new.mkv", 0)

        assert cache._index[ku].tier == "warm"
        assert cache._index[kp].tier == "hot"
        assert cache._index[kn].tier == "hot"
        assert cache._hot_bytes == 200

        # Now put fourth chunk into hot (100B).
        # Unprotected hot chunks are exhausted, so protected chunk CAN demote to warm disk tier safely.
        await cache.put("new2.mkv", 0, b"N2" * 50)
        assert cache._index[kp].tier == "warm"
        # Crucially: it demoted to warm, but is STILL protected by active lease!
        assert cache._is_entry_protected(kp, cache._index[kp])

    trio.run(_run)


def test_concurrent_active_readers_refcount(tmp_path: Path) -> None:
    """Concurrent readers on same chunk maintain refcount and prevent eviction during read."""
    cache = Cache(
        CacheConfig(
            cache_dir=tmp_path / "cache",
            max_size_bytes=100,
            metrics_enabled=False,
        )
    )

    async def _run() -> None:
        payload = b"C" * 100
        await cache.put("shared.mkv", 0, payload)
        key = cache._key("shared.mkv", 0)

        # Reader 1 enters
        with cache.reading_chunk(key):
            assert cache._active_readers[key] == 1
            assert cache._is_entry_protected(key)

            # Reader 2 enters concurrently
            with cache.reading_chunk(key):
                assert cache._active_readers[key] == 2
                assert cache._is_entry_protected(key)

                # Attempt eviction: must be refused!
                initial_refusals = cache.eviction_refusals
                await cache._evict_lru(need_bytes=50)
                assert cache.eviction_refusals > initial_refusals
                assert key in cache._index

            # Reader 2 exited
            assert cache._active_readers[key] == 1
            assert cache._is_entry_protected(key)

        # Reader 1 exited
        assert key not in cache._active_readers
        assert not cache._is_entry_protected(key)

        # Now eviction succeeds
        await cache._evict_lru(need_bytes=50)
        assert key not in cache._index

    trio.run(_run)


def test_lease_lifecycle_ttl_and_playhead_reconciliation(tmp_path: Path) -> None:
    """Lease lifecycle: monotonic expiration, seek reconciliation, and teardown."""
    cache = Cache(
        CacheConfig(
            cache_dir=tmp_path / "cache",
            max_size_bytes=10 * 1024 * 1024,
            metrics_enabled=False,
        )
    )

    async def _run() -> None:
        # Acquire lease with 0.1s TTL
        lease = cache.acquire_lease(
            stream_id="test_stream",
            cache_key="movie.mkv",
            start=0,
            size=1024,
            lease_seconds=0.1,
        )
        k = cache._key("movie.mkv", 0)
        assert cache._is_entry_protected(k)

        # Wait for lease to expire
        await trio.sleep(0.15)

        # Protected check should clean up expired lease inline
        assert not cache._is_entry_protected(k)
        assert k not in cache._leases_by_key
        assert "test_stream" not in cache._leases_by_stream

    trio.run(_run)


def test_cache_accounting_invariants_under_operations(tmp_path: Path) -> None:
    """_total_bytes and _hot_bytes stay exactly synchronized with ground truth across puts, demotions, and evictions."""
    cache = Cache(
        CacheConfig(
            cache_dir=tmp_path / "warm",
            max_size_bytes=300,
            hot_dir=tmp_path / "hot",
            hot_max_size_bytes=150,
            metrics_enabled=False,
        )
    )

    def _verify_invariants():
        with cache._thread_lock:
            expected_total = sum(e.size for e in cache._index.values())
            expected_hot = sum(e.size for e in cache._index.values() if e.tier == "hot")
            assert cache._total_bytes == expected_total
            assert cache._hot_bytes == expected_hot
            # Protected calculation
            now = time.monotonic()
            expected_prot = sum(
                e.size
                for k, e in cache._index.items()
                if cache._active_readers.get(k, 0) > 0
                or (
                    cache._leases_by_key.get(k)
                    and any(
                        l.expires_at > now for l in cache._leases_by_key[k].values()
                    )
                )
            )
        snap_bytes, snap_entries = cache.sync_size_snapshot()
        assert snap_bytes == expected_total
        assert snap_entries == len(cache._index)
        assert cache.protected_bytes() == expected_prot

    async def _run() -> None:
        _verify_invariants()

        await cache.put("f1.mkv", 0, b"1" * 80)
        _verify_invariants()

        await cache.put("f2.mkv", 0, b"2" * 80, stream_id="s1")
        _verify_invariants()

        # Causes demotion to warm
        await cache.put("f3.mkv", 0, b"3" * 80)
        _verify_invariants()

        # Causes LRU eviction
        await cache.put("f4.mkv", 0, b"4" * 80)
        _verify_invariants()

        # Release stream
        cache.release_stream("s1")
        _verify_invariants()

    trio.run(_run)


def test_concurrency_stress_readers_writers_evictions(tmp_path: Path) -> None:
    """Stress test: 16 concurrent workers reading, writing, and evicting without deadlocks or corruption."""
    cache = Cache(
        CacheConfig(
            cache_dir=tmp_path / "warm",
            max_size_bytes=500,
            hot_dir=tmp_path / "hot",
            hot_max_size_bytes=200,
            metrics_enabled=False,
        )
    )

    payload = b"X" * 50

    async def writer(title_id: int) -> None:
        for offset in range(0, 200, 50):
            await cache.put(
                f"title_{title_id}.mkv",
                offset,
                payload,
                stream_id=f"stream_{title_id}",
            )
            await trio.sleep(0.001)

    async def reader(title_id: int) -> None:
        for offset in range(0, 200, 50):
            # Check has
            cache.has(f"title_{title_id}.mkv", offset, offset + 49)
            # Read
            await cache.get(
                f"title_{title_id}.mkv",
                offset,
                offset + 49,
                stream_id=f"stream_{title_id}",
            )
            # Reconcile playhead
            cache.reconcile_stream_playhead(
                stream_id=f"stream_{title_id}",
                cache_key=f"title_{title_id}.mkv",
                playhead_byte=offset,
            )
            await trio.sleep(0.001)

    async def _run() -> None:
        with trio.fail_after(10.0):  # Hard safety timeout to detect deadlocks
            async with trio.open_nursery() as nursery:
                for i in range(8):
                    nursery.start_soon(writer, i)
                for i in range(8):
                    nursery.start_soon(reader, i)

        # Confirm invariants still hold cleanly after high concurrency
        with cache._thread_lock:
            assert cache._total_bytes == sum(e.size for e in cache._index.values())
            assert cache._hot_bytes == sum(
                e.size for e in cache._index.values() if e.tier == "hot"
            )

    trio.run(_run)


def test_long_duration_playback_simulation(tmp_path: Path) -> None:
    """Continuous playback simulation across 3 concurrent streams: playhead advance, prefetch, LRU eviction."""
    cache = Cache(
        CacheConfig(
            cache_dir=tmp_path / "warm",
            max_size_bytes=1000,
            hot_dir=tmp_path / "hot",
            hot_max_size_bytes=400,
            metrics_enabled=False,
        )
    )

    chunk_size = 100
    chunk_payload = b"P" * chunk_size

    async def simulate_stream(stream_idx: int) -> None:
        title = f"movie_{stream_idx}.mkv"
        stream_id = f"stream_{stream_idx}"

        for chunk_idx in range(20):
            byte_pos = chunk_idx * chunk_size
            # 1. Prefetch puts next 2 chunks into cache with stream lease
            for ahead in [0, 1]:
                ahead_pos = (chunk_idx + ahead) * chunk_size
                await cache.put(
                    title,
                    ahead_pos,
                    chunk_payload,
                    stream_id=stream_id,
                )

            # 2. Player reads current chunk
            data = await cache.get(
                title,
                byte_pos,
                byte_pos + chunk_size - 1,
                stream_id=stream_id,
            )
            assert len(data) == chunk_size

            # 3. Player advances playhead (lookback 100B, lookahead 200B)
            cache.reconcile_stream_playhead(
                stream_id=stream_id,
                cache_key=title,
                playhead_byte=byte_pos,
                lookback_bytes=100,
                lookahead_bytes=200,
            )
            await trio.sleep(0.002)

        # Teardown stream
        cache.release_stream(stream_id)

    async def _run() -> None:
        with trio.fail_after(15.0):
            async with trio.open_nursery() as nursery:
                for s in range(3):
                    nursery.start_soon(simulate_stream, s)

    trio.run(_run)


def test_trim_recursion_eliminated_under_lease_saturation(tmp_path: Path) -> None:
    """Verifies that trim() does not trigger mutual recursion with _initial_scan() when eviction is refused."""
    cache = Cache(
        CacheConfig(
            cache_dir=tmp_path / "cache",
            max_size_bytes=150,
            metrics_enabled=False,
        )
    )

    scan_called = 0
    original_sync_scan = cache._sync_initial_scan

    def _tracking_scan() -> None:
        nonlocal scan_called
        scan_called += 1
        original_sync_scan()

    cache._sync_initial_scan = _tracking_scan

    async def _run() -> None:
        # Put 200 bytes under active lease (capacity = 150)
        await cache.put("film.mkv", 0, b"A" * 100, stream_id="active_stream")
        await cache.put("film.mkv", 100, b"B" * 100, stream_id="active_stream")

        assert cache._total_bytes == 200
        assert cache._total_bytes > cache.cfg.max_size_bytes

        # Calling trim() must complete immediately with 0 recursive scans
        start_t = time.monotonic()
        with trio.fail_after(2.0):
            await cache.trim()
        elapsed = time.monotonic() - start_t

        # Assert no mutual recursion occurred and execution took < 100ms
        assert scan_called == 0
        assert elapsed < 0.1
        # Data is still safely preserved under active lease
        assert cache._key("film.mkv", 0) in cache._index
        assert cache._key("film.mkv", 100) in cache._index

    trio.run(_run)


def test_container_header_protection_on_playhead_advance(tmp_path: Path) -> None:
    """Verifies that container header chunks remain protected throughout active stream playback."""
    cache = Cache(
        CacheConfig(
            cache_dir=tmp_path / "cache",
            max_size_bytes=350,
            metrics_enabled=False,
        )
    )

    async def _run() -> None:
        stream_id = "demuxer_stream"
        cache_key = "feature_film.mkv"
        header_chunk_size = 100
        body_chunk_size = 100

        # Chunk 0 is container header (0..99)
        await cache.put(cache_key, 0, b"H" * header_chunk_size, stream_id=stream_id)
        # Chunks 1, 2 are early body
        await cache.put(cache_key, 100, b"B" * body_chunk_size, stream_id=stream_id)
        await cache.put(cache_key, 200, b"B" * body_chunk_size, stream_id=stream_id)

        # Advance playhead far ahead to 50,000 bytes with lookback 50 bytes and header_bytes=100
        cache.reconcile_stream_playhead(
            stream_id=stream_id,
            cache_key=cache_key,
            playhead_byte=50_000,
            lookback_bytes=50,
            lookahead_bytes=500,
            header_bytes=header_chunk_size,
        )

        # Chunk 0 MUST remain protected despite playhead being far ahead of lookback
        assert cache.is_chunk_lease_protected(cache_key, 0) is True

        # Now put Chunk 3 (100 bytes) without lease to push cache over 350 budget (400 > 350)
        # and trigger eviction under pressure
        await cache.put(cache_key, 300, b"B" * body_chunk_size)

        # Chunk 0 MUST still be present in cache and protected
        assert cache._key(cache_key, 0) in cache._index
        assert cache.is_chunk_lease_protected(cache_key, 0) is True

        # When stream is torn down, header chunk lease is cleanly released
        cache.release_stream(stream_id)
        assert cache.is_chunk_lease_protected(cache_key, 0) is False

    trio.run(_run)


def test_compressed_time_long_play_saturation_and_late_header_read(
    tmp_path: Path,
) -> None:
    """Deterministic regression simulating >=90 minutes of continuous playback under cache saturation.

    Exercises:
    - Cache cold start and sequential playback progression
    - First-time cache saturation and continuous LRU eviction cycles
    - Active StreamLease forward/backward lookahead movement
    - Container header [0, header_bytes) protection
    - Late backward reads hitting protected header data past saturation
    - Mid-playback seek with playhead lease reconciliation
    - Zero recursive filesystem scans during background eviction
    """
    import statistics

    chunk_size = 5 * 1024
    max_bytes = (
        100 * chunk_size
    )  # Capacity = 100 chunks; 900 chunks total = 9x cache capacity rollover
    cache = Cache(
        CacheConfig(
            cache_dir=tmp_path / "cache",
            max_size_bytes=max_bytes,
            metrics_enabled=False,
        )
    )

    scan_count = 0
    orig_scan = cache._sync_initial_scan

    def _counting_scan() -> None:
        nonlocal scan_count
        scan_count += 1
        orig_scan()

    cache._sync_initial_scan = _counting_scan

    stream_id = "long_play_sim_stream"
    cache_key = "feature_film_90m.mkv"
    header_bytes = chunk_size  # Chunk 0 covers container header

    read_latencies: list[float] = []

    async def _run() -> None:
        await cache._initialize()
        nonlocal scan_count
        scan_count = 0

        # Step 1: Write initial container header chunk 0
        await cache.put(cache_key, 0, b"H" * chunk_size, stream_id=stream_id)

        # Step 2: 900 continuous playback steps (representing 90 minutes at 10 chunks/min)
        for i in range(1, 901):
            pos = i * chunk_size
            await cache.put(cache_key, pos, b"D" * chunk_size, stream_id=stream_id)

            cache.reconcile_stream_playhead(
                stream_id=stream_id,
                cache_key=cache_key,
                playhead_byte=pos,
                lookback_bytes=2 * chunk_size,
                lookahead_bytes=10 * chunk_size,
                header_bytes=header_bytes,
            )

            # Timed foreground read of current chunk
            t0 = time.monotonic()
            data = await cache.get(
                cache_key, pos, pos + chunk_size - 1, stream_id=stream_id
            )
            read_latencies.append(time.monotonic() - t0)
            assert len(data) == chunk_size

            # Late backward read at multiple saturation milestones (e.g. step 150, 600, 850)
            if i in (150, 600, 850):
                t_hdr = time.monotonic()
                hdata = await cache.get(
                    cache_key, 0, chunk_size - 1, stream_id=stream_id
                )
                read_latencies.append(time.monotonic() - t_hdr)
                assert len(hdata) == chunk_size
                assert cache.is_chunk_lease_protected(cache_key, 0) is True

            # Mid-stream seek at step 750
            if i == 750:
                seek_pos = 820 * chunk_size
                cache.reconcile_stream_playhead(
                    stream_id=stream_id,
                    cache_key=cache_key,
                    playhead_byte=seek_pos,
                    lookback_bytes=2 * chunk_size,
                    lookahead_bytes=10 * chunk_size,
                    header_bytes=header_bytes,
                )

        cache.release_stream(stream_id)

        # Assertions
        assert scan_count == 0, f"Expected 0 maintenance rescans, got {scan_count}"
        p50 = statistics.median(read_latencies)
        p95 = statistics.quantiles(read_latencies, n=100)[94]
        p99 = statistics.quantiles(read_latencies, n=100)[98]
        # Steady state read latencies must remain bounded (< 50ms)
        assert p50 < 0.05, f"p50 read latency too high: {p50*1000:.2f}ms"
        assert p95 < 0.05, f"p95 read latency too high: {p95*1000:.2f}ms"
        assert p99 < 0.10, f"p99 read latency too high: {p99*1000:.2f}ms"

    trio.run(_run)


def test_lease_and_resource_leak_stress_100_sessions_and_concurrency(
    tmp_path: Path,
) -> None:
    """Validates resource cleanup and eviction convergence across 100 sessions and 1, 4, 16 concurrent streams."""
    chunk_size = 1024
    cache = Cache(
        CacheConfig(
            cache_dir=tmp_path / "cache",
            max_size_bytes=100_000,
            metrics_enabled=False,
        )
    )

    async def _run() -> None:
        await cache._initialize()

        # Phase A: 100 sequential open/read/seek/close sessions
        for s in range(100):
            sid = f"sess_{s}"
            key = f"media_{s % 5}.mkv"
            await cache.put(key, 0, b"H" * chunk_size, stream_id=sid)
            await cache.get(key, 0, chunk_size - 1, stream_id=sid)
            cache.reconcile_stream_playhead(
                stream_id=sid,
                cache_key=key,
                playhead_byte=10_000,
                lookback_bytes=2_000,
                lookahead_bytes=5_000,
                header_bytes=chunk_size,
            )
            cache.reconcile_stream_playhead(
                stream_id=sid,
                cache_key=key,
                playhead_byte=50_000,
                lookback_bytes=2_000,
                lookahead_bytes=5_000,
                header_bytes=chunk_size,
            )
            cache.release_stream(sid)

        assert (
            len(cache._leases_by_stream) == 0
        ), "Dangling stream leases in _leases_by_stream"
        assert len(cache._leases_by_key) == 0, "Dangling key leases in _leases_by_key"
        assert (
            cache.protected_bytes() == 0
        ), "Dangling protected_bytes after 100 sessions"

        # Phase B: Concurrency stress with 1, 4, and 16 concurrent streams
        for conc in (1, 4, 16):

            async def run_client(cid: int) -> None:
                sid = f"conc_{conc}_{cid}"
                key = f"video_{cid}.mkv"
                await cache.put(key, 0, b"H" * chunk_size, stream_id=sid)
                for step in range(5):
                    pos = step * chunk_size
                    await cache.put(key, pos, b"D" * chunk_size, stream_id=sid)
                    cache.reconcile_stream_playhead(
                        stream_id=sid,
                        cache_key=key,
                        playhead_byte=pos,
                        lookback_bytes=chunk_size,
                        lookahead_bytes=2 * chunk_size,
                        header_bytes=chunk_size,
                    )
                    await cache.get(key, pos, pos + chunk_size - 1, stream_id=sid)
                    await trio.sleep(0.001)
                cache.release_stream(sid)

            async with trio.open_nursery() as nursery:
                for c in range(conc):
                    nursery.start_soon(run_client, c)

            assert (
                len(cache._leases_by_stream) == 0
            ), f"Dangling stream leases after conc={conc}"
            assert (
                len(cache._leases_by_key) == 0
            ), f"Dangling key leases after conc={conc}"
            assert (
                cache.protected_bytes() == 0
            ), f"Dangling protected_bytes after conc={conc}"

        # Phase C: Eviction convergence proof under capacity overflow
        for i in range(150):
            await cache.put("unprotected.mkv", i * chunk_size, b"U" * chunk_size)
        await cache.trim()
        assert (
            cache._total_bytes <= cache.cfg.max_size_bytes
        ), "Eviction failed to converge below capacity"

    trio.run(_run)
