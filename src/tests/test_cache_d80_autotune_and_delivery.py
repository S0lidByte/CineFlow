"""
Tests for Phase D80: Cache Auto-Tune, dynamic watermarks, non-destructive demotion,
and direct foreground delivery registry when persistent cache admission fails.

Governing invariant: ACTIVE PLAYBACK DATA > CACHE RETENTION.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import trio

from program.services.streaming.cache import (
    Cache,
    CacheConfig,
    CachePutResult,
    ChunkState,
)
from program.services.streaming.media_stream import _DeliveryRegistry


def test_cache_put_result_enum_states() -> None:
    """CachePutResult defines distinct deterministic admission outcomes."""
    assert CachePutResult.STORED_HOT.value == "stored_hot"
    assert CachePutResult.STORED_WARM.value == "stored_warm"
    assert CachePutResult.SKIPPED_PREFETCH_PRESSURE.value == "skipped_prefetch_pressure"
    assert CachePutResult.REFUSED_PHYSICAL_PRESSURE.value == "refused_physical_pressure"


def test_put_returns_stored_hot_and_stored_warm(tmp_path: Path) -> None:
    """put returns STORED_HOT for two-tier hot writes and STORED_WARM for disk-only writes."""
    hot_dir = tmp_path / "hot"
    warm_dir = tmp_path / "warm"

    cache = Cache(
        CacheConfig(
            cache_dir=warm_dir,
            max_size_bytes=10000,
            hot_dir=hot_dir,
            hot_max_size_bytes=500,
            metrics_enabled=False,
        )
    )

    async def _run() -> None:
        # Small chunk fits in hot tier
        res_hot = await cache.put("movie.mkv", 0, b"H" * 100)
        assert res_hot == CachePutResult.STORED_HOT

        # Chunk larger than hot_max_size_bytes routes directly to warm tier
        res_warm = await cache.put("movie.mkv", 100, b"W" * 600)
        assert res_warm == CachePutResult.STORED_WARM

    trio.run(_run)


def test_prefetch_skipped_under_watermark_pressure(tmp_path: Path) -> None:
    """Prefetch chunk admission returns SKIPPED_PREFETCH_PRESSURE when cache is near capacity."""
    cache_dir = tmp_path / "cache"

    cache = Cache(
        CacheConfig(
            cache_dir=cache_dir,
            max_size_bytes=1000,
            warm_watermark_high_pct=80.0,
            warm_watermark_low_pct=60.0,
            metrics_enabled=False,
        )
    )

    async def _run() -> None:
        # Fill cache with protected chunks up to 85% (> 80% high watermark)
        await cache.put("movie.mkv", 0, b"P" * 850, stream_id="active_stream")

        # Demand request is still admitted (active playback priority)
        res_demand = await cache.put(
            "movie.mkv", 850, b"D" * 50, admission="demand", stream_id="active_stream"
        )
        assert res_demand in (CachePutResult.STORED_WARM, CachePutResult.STORED_HOT)

        # Prefetch request under tight pressure is dropped cleanly
        res_prefetch = await cache.put(
            "movie.mkv", 900, b"S" * 50, admission="prefetch"
        )
        assert res_prefetch == CachePutResult.SKIPPED_PREFETCH_PRESSURE

    trio.run(_run)


def test_demand_refused_under_hard_cap_exhaustion(tmp_path: Path) -> None:
    """When disk cache is fully saturated by active leases, demand write returns REFUSED_PHYSICAL_PRESSURE."""
    cache_dir = tmp_path / "cache"

    cache = Cache(
        CacheConfig(
            cache_dir=cache_dir,
            max_size_bytes=300,
            metrics_enabled=False,
        )
    )

    async def _run() -> None:
        # Fill cache to 100% with protected playback chunks
        await cache.put("stream1.mkv", 0, b"A" * 150, stream_id="s1")
        await cache.put("stream2.mkv", 0, b"B" * 150, stream_id="s2")

        # An unleased (demand) chunk cannot fit without exceeding hard cap and cannot evict s1/s2
        res = await cache.put("unleased.mkv", 0, b"C" * 100, admission="demand")
        assert res == CachePutResult.REFUSED_PHYSICAL_PRESSURE

    trio.run(_run)


def test_non_destructive_demotion_staging_and_commit(tmp_path: Path) -> None:
    """Hot-to-warm demotion stages to temporary files, and commits atomically."""
    hot_dir = tmp_path / "hot"
    warm_dir = tmp_path / "warm"

    cache = Cache(
        CacheConfig(
            cache_dir=warm_dir,
            max_size_bytes=10000,
            hot_dir=hot_dir,
            hot_max_size_bytes=200,
            metrics_enabled=False,
        )
    )

    async def _run() -> None:
        payload = b"TEST_PAYLOAD_HOT"
        await cache.put("doc.mkv", 0, payload)
        key = cache._key("doc.mkv", 0)

        assert cache._index[key].tier == "hot"
        hot_payload_file = cache._file_for(key, tier="hot")
        assert hot_payload_file.exists()

        # Overflow hot tier: add 200B chunk (16 + 200 = 216 > 200B cap)
        await cache.put("doc.mkv", 100, b"X" * 200)

        # Original chunk should have been demoted to warm
        assert cache._index[key].tier == "warm"
        warm_payload_file = cache._file_for(key, tier="warm")
        assert warm_payload_file.exists()
        # Hot payload file should have been cleaned up post-commit
        assert not hot_payload_file.exists()

        # Data readable and identical from warm tier
        read_back = await cache.get("doc.mkv", 0, len(payload) - 1)
        assert read_back == payload

    trio.run(_run)


def test_demotion_aborted_when_active_reader_present(tmp_path: Path) -> None:
    """Hot chunk cannot be demoted while an active reader is reading it."""
    hot_dir = tmp_path / "hot"
    warm_dir = tmp_path / "warm"

    cache = Cache(
        CacheConfig(
            cache_dir=warm_dir,
            max_size_bytes=10000,
            hot_dir=hot_dir,
            hot_max_size_bytes=200,
            metrics_enabled=False,
        )
    )

    async def _run() -> None:
        await cache.put("movie.mkv", 0, b"READING_NOW" * 5)
        key = cache._key("movie.mkv", 0)

        with cache.reading_chunk(key):
            # Attempt hot capacity reclaim while reader holds chunk
            await cache._ensure_hot_capacity(150)

            # Demotion MUST be skipped: entry remains safely in hot tier
            assert cache._index[key].tier == "hot"

    trio.run(_run)


def test_dynamic_watermark_configuration(tmp_path: Path) -> None:
    """update_watermarks updates thresholds dynamically with bounds validation."""
    cache = Cache(
        CacheConfig(
            cache_dir=tmp_path / "warm",
            max_size_bytes=100 * 1024 * 1024,
            hot_dir=tmp_path / "hot",
            hot_max_size_bytes=20 * 1024 * 1024,
            metrics_enabled=False,
        )
    )

    status_initial = cache.watermark_status()
    assert status_initial["warm_watermark_high_pct"] == 85.0
    assert status_initial["warm_watermark_low_pct"] == 70.0

    # Dynamically tune watermarks
    cache.update_watermarks(
        warm_watermark_high_pct=90.0,
        warm_watermark_low_pct=75.0,
        hot_watermark_high_pct=88.0,
        hot_watermark_low_pct=65.0,
    )

    status_updated = cache.watermark_status()
    assert status_updated["warm_watermark_high_pct"] == 90.0
    assert status_updated["warm_watermark_low_pct"] == 75.0
    assert status_updated["hot_watermark_high_pct"] == 88.0
    assert status_updated["hot_watermark_low_pct"] == 65.0

    # Bounds validation
    with pytest.raises(ValueError):
        cache.update_watermarks(
            warm_watermark_low_pct=95.0, warm_watermark_high_pct=80.0
        )


def test_delivery_registry_transient_foreground_handoff() -> None:
    """DeliveryRegistry delivers data to waiting consumers without duplicate provider fetches."""
    registry = _DeliveryRegistry()

    async def _run() -> None:
        start_offset = 1024 * 1024
        expected_bytes = b"PLAYBACK_CHUNK_DELIVERY_BYTES_12345678"

        # Producer registers pending slot prior to network fetch
        await registry.register_pending(start_offset)

        consumer_received: bytes | None = None

        async def _consumer() -> None:
            nonlocal consumer_received
            consumer_received = await registry.wait_for_delivery(
                start=start_offset, timeout_seconds=2.0
            )

        async def _producer() -> None:
            await trio.sleep(0.05)
            await registry.publish(start=start_offset, payload=expected_bytes)

        async with trio.open_nursery() as nursery:
            nursery.start_soon(_consumer)
            nursery.start_soon(_producer)

        assert consumer_received == expected_bytes

    trio.run(_run)


def test_delivery_registry_timeout_returns_none() -> None:
    """DeliveryRegistry returns None when waiting consumer times out."""
    registry = _DeliveryRegistry()

    async def _run() -> None:
        await registry.register_pending(0)
        res = await registry.wait_for_delivery(start=0, timeout_seconds=0.05)
        assert res is None

    trio.run(_run)
