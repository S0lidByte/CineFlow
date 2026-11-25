"""FIX-3: _evict_lru TTL-cached disk_usage prevents per-put statfs stall.

Without the TTL, _evict_lru() calls shutil.disk_usage() synchronously on
every Cache.put() — at 65% iowait this is a 50-300 ms stall per MB chunk,
which starves the Trio event loop and causes httpx.ReadTimeout(7.0s).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import trio

from program.services.streaming.cache import Cache, CacheConfig


def _make_cache(tmp_path: Path, *, max_size_bytes: int = 10 * 1024 * 1024) -> Cache:
    cfg = CacheConfig(
        cache_dir=tmp_path,
        max_size_bytes=max_size_bytes,
        warm_min_free_mb=0,
    )
    return Cache(cfg)


def test_cache_disk_usage_ttl_attributes_exist(tmp_path: Path) -> None:
    """FIX-3: Cache instance must expose TTL cache fields added for disk_usage."""
    cache = _make_cache(tmp_path)
    assert hasattr(
        cache, "_disk_usage_cache"
    ), "_disk_usage_cache TTL attribute missing from Cache.__init__"
    assert hasattr(
        cache, "_DISK_USAGE_TTL_SECONDS"
    ), "_DISK_USAGE_TTL_SECONDS constant missing from Cache.__init__"
    # TTL must be at least 1 second (meaningful guard against per-put statfs stall)
    assert cache._DISK_USAGE_TTL_SECONDS >= 1.0


def test_cache_disk_usage_not_called_on_every_evict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FIX-3: shutil.disk_usage must be called at most once per TTL window.

    Without the TTL, _evict_lru() calls shutil.disk_usage() synchronously on
    every Cache.put() — at 65% iowait this is a 50-300 ms stall per MB chunk,
    which starves the Trio event loop and causes httpx.ReadTimeout(7.0s).
    """
    import shutil

    usage_call_count = 0
    real_disk_usage = shutil.disk_usage

    def counting_disk_usage(path: Path | str):  # type: ignore[override]
        nonlocal usage_call_count
        usage_call_count += 1
        return real_disk_usage(path)

    monkeypatch.setattr(shutil, "disk_usage", counting_disk_usage)

    # Give cache a generous max so eviction is triggered by warm_min_free_mb
    cache = _make_cache(tmp_path, max_size_bytes=10 * 1024 * 1024)
    # Force warm_min_free_mb > 0 so the disk_usage branch fires inside _evict_lru
    cache.cfg.warm_min_free_mb = 1

    payload = b"X" * 1024  # 1 KiB

    async def _run() -> None:
        # Rapid successive puts — without TTL this would call disk_usage N times
        for i in range(10):
            await cache.put("movie.mkv", i * 1024, payload)

    trio.run(_run)

    # With the TTL guard, disk_usage must have been called far fewer times than puts
    # (ideally 0 or 1 within the same second; always < 10)
    assert usage_call_count < 10, (
        f"shutil.disk_usage called {usage_call_count} times for 10 puts — "
        "TTL cache is not working; this would cause per-put statfs stalls under I/O load"
    )


def test_cache_trim_invalidates_disk_usage_ttl_and_release_hot_capacity_nonblocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FIX-3: trim() refreshes TTL cache; _release_hot_capacity does not block on _hot_write_lock."""
    import shutil
    from types import SimpleNamespace

    calls = 0

    def mock_usage(_path: Path | str) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        return SimpleNamespace(free=500 * 1024 * 1024)

    monkeypatch.setattr(shutil, "disk_usage", mock_usage)
    cache = _make_cache(tmp_path, max_size_bytes=10 * 1024 * 1024)
    cache.cfg.warm_min_free_mb = 1

    async def _run() -> None:
        await cache.put("movie.mkv", 0, b"a" * 1024)
        await cache.put("movie.mkv", 1024, b"b" * 1024)
        assert calls == 1  # Second put hit 5s TTL

        await cache.trim()
        assert calls == 2  # Explicit trim invalidated TTL

        cache._hot_reserved_bytes = 256
        async with cache._hot_write_lock:
            with trio.fail_after(1.0):
                await cache._release_hot_capacity(256)
        assert cache._hot_reserved_bytes == 0

    trio.run(_run)


def test_cache_disk_usage_reclaim_reconciled_and_hot_puts_do_not_drain_warm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FIX-3: Eviction credits reclaimed warm bytes so hot/warm puts do not repeatedly drain warm cache."""
    import shutil
    from types import SimpleNamespace

    kib = 1024
    mib = 1024 * 1024
    warm_dir = tmp_path / "warm"
    hot_dir = tmp_path / "hot"
    calls = 0

    cache = Cache(
        CacheConfig(
            cache_dir=warm_dir,
            hot_dir=hot_dir,
            max_size_bytes=10 * mib,
            hot_max_size_bytes=512,
            warm_min_free_mb=0,
            metrics_enabled=False,
        )
    )

    async def _run() -> None:
        # Seed two 250 KiB warm chunks (hot_max=512 -> written to warm tier).
        await cache.put("movie.mkv", 0, b"a" * (250 * kib))
        await cache.put("movie.mkv", 250 * kib, b"b" * (250 * kib))
        assert cache.has("movie.mkv", 0, 250 * kib - 1)
        assert cache.has("movie.mkv", 250 * kib, 500 * kib - 1)

        # Enable 1 MiB min-free guard while OS initially reports 800 KiB free (224 KiB deficit).
        def low_free(_path: Path | str) -> SimpleNamespace:
            nonlocal calls
            calls += 1
            return SimpleNamespace(free=800 * kib)

        monkeypatch.setattr(shutil, "disk_usage", low_free)
        cache.cfg.warm_min_free_mb = 1
        # Expand hot cap so subsequent 128-byte writes stay on hot tier.
        cache.cfg.hot_max_size_bytes = 1 * mib

        # Hot put #1 triggers _evict_lru(0): evicts ONE 250 KiB chunk to close 224 KiB deficit,
        # reconciling cached free space to 800 + 250 = 1050 KiB >= 1 MiB.
        await cache.put("movie.mkv", 500 * kib, b"h" * 128)
        assert not cache.has("movie.mkv", 0, 250 * kib - 1)
        assert cache.has("movie.mkv", 250 * kib, 500 * kib - 1)

        # Hot puts #2 and #3 within TTL see reconciled 1050 KiB >= 1 MiB and must NOT drain chunk #2.
        await cache.put("movie.mkv", 500 * kib + 128, b"i" * 128)
        await cache.put("movie.mkv", 500 * kib + 256, b"j" * 128)
        assert calls == 1
        assert cache.has("movie.mkv", 250 * kib, 500 * kib - 1)

    trio.run(_run)


def test_cache_disk_usage_concurrent_expiry_coalesced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FIX-3: Concurrent puts arriving on an expired sample coalesce to one disk_usage refresh."""
    import shutil
    import time
    from types import SimpleNamespace

    calls = 0

    def slow_usage(_path: Path | str) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        time.sleep(0.05)
        return SimpleNamespace(free=500 * 1024 * 1024)

    monkeypatch.setattr(shutil, "disk_usage", slow_usage)
    cache = _make_cache(tmp_path, max_size_bytes=10 * 1024 * 1024)
    cache.cfg.warm_min_free_mb = 1

    async def _run() -> None:
        async with trio.open_nursery() as nursery:
            for i in range(5):
                nursery.start_soon(cache.put, f"title_{i}.mkv", 0, b"x" * 1024)

        assert calls == 1

    trio.run(_run)


def test_cache_disk_usage_concurrent_shortfall_evicts_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FIX-3: Concurrent callers sampling the same shortfall before locking evict once."""
    import shutil
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    kib = 1024
    mib = 1024 * 1024
    cache = Cache(
        CacheConfig(
            cache_dir=tmp_path / "warm",
            hot_dir=tmp_path / "hot",
            max_size_bytes=10 * mib,
            hot_max_size_bytes=512,
            warm_min_free_mb=0,
            metrics_enabled=False,
        )
    )

    async def _run() -> None:
        await cache.put("movie.mkv", 0, b"a" * (250 * kib))
        await cache.put("movie.mkv", 250 * kib, b"b" * (250 * kib))

        calls = 0

        def low_free(_path: Path | str) -> SimpleNamespace:
            nonlocal calls
            calls += 1
            return SimpleNamespace(free=800 * kib)

        monkeypatch.setattr(shutil, "disk_usage", low_free)
        cache.cfg.warm_min_free_mb = 1
        cache.cfg.hot_max_size_bytes = 1 * mib

        # Force both callers to complete _get_warm_free_bytes_cached() before acquiring locks().
        orig_locks = cache.locks
        arrivals = 0
        both_sampled = trio.Event()

        @asynccontextmanager
        async def synced_locks():
            nonlocal arrivals
            arrivals += 1
            if arrivals == 2:
                both_sampled.set()
            await both_sampled.wait()
            async with orig_locks():
                yield

        monkeypatch.setattr(cache, "locks", synced_locks)

        with trio.fail_after(2.0):
            async with trio.open_nursery() as nursery:
                nursery.start_soon(cache._evict_lru, 0)
                nursery.start_soon(cache._evict_lru, 0)

        # Follow with real hot-tier put on separate hot_dir; still no extra warm eviction.
        await cache.put("movie.mkv", 500 * kib, b"h" * 128)

        assert calls == 1
        assert not cache.has("movie.mkv", 0, 250 * kib - 1)
        assert cache.has("movie.mkv", 250 * kib, 500 * kib - 1)

    trio.run(_run)
