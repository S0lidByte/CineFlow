"""Two-tier hot/warm streaming cache."""

from __future__ import annotations

from pathlib import Path

import trio

from program.services.streaming.cache import Cache, CacheConfig


def test_put_writes_hot_first(tmp_path: Path) -> None:
    warm = tmp_path / "warm"
    hot = tmp_path / "hot"
    cache = Cache(
        CacheConfig(
            cache_dir=warm,
            max_size_bytes=10 * 1024 * 1024,
            hot_dir=hot,
            hot_max_size_bytes=1024,
            metrics_enabled=False,
        )
    )
    payload = b"x" * 100

    async def _run() -> None:
        await cache.put("movie.mkv", 0, payload)
        got = await cache.get("movie.mkv", 0, len(payload) - 1)
        assert got == payload
        key = cache._key("movie.mkv", 0)
        assert cache._file_for(key, tier="hot").exists()
        assert not cache._file_for(key, tier="warm").exists()
        assert cache._index[key].tier == "hot"

    trio.run(_run)


def test_hot_overflow_demotes_to_warm(tmp_path: Path) -> None:
    warm = tmp_path / "warm"
    hot = tmp_path / "hot"
    cache = Cache(
        CacheConfig(
            cache_dir=warm,
            max_size_bytes=10 * 1024 * 1024,
            hot_dir=hot,
            hot_max_size_bytes=150,
            metrics_enabled=False,
        )
    )

    async def _run() -> None:
        await cache.put("movie.mkv", 0, b"a" * 100)
        await cache.put("movie.mkv", 1000, b"b" * 100)
        # First entry should have been demoted to warm
        k0 = cache._key("movie.mkv", 0)
        k1 = cache._key("movie.mkv", 1000)
        assert cache._index[k0].tier == "warm"
        assert cache._file_for(k0, tier="warm").exists()
        assert cache._index[k1].tier == "hot"
        assert cache._file_for(k1, tier="hot").exists()
        # Both still readable
        assert await cache.get("movie.mkv", 0, 99) == b"a" * 100
        assert await cache.get("movie.mkv", 1000, 1099) == b"b" * 100

    trio.run(_run)
