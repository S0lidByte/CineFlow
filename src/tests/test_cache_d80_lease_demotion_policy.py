"""Playback leases veto tier changes without measured runway evidence."""

from pathlib import Path

import pytest
import trio

from program.services.streaming.cache import Cache, CacheConfig


def make_cache(root: Path) -> Cache:
    return Cache(
        CacheConfig(
            cache_dir=root / "warm",
            max_size_bytes=4096,
            hot_dir=root / "hot",
            hot_max_size_bytes=200,
            metrics_enabled=False,
            warm_min_free_mb=0,
        )
    )


def test_leased_chunk_stays_hot_under_pressure(tmp_path: Path) -> None:
    async def scenario() -> None:
        cache = make_cache(tmp_path)
        await cache.put("movie", 0, b"a" * 100, stream_id="playback")
        await cache._ensure_hot_capacity(200)
        key = cache._key("movie", 0)
        assert cache._index[key].tier == "hot"
        assert cache._hot_bytes == 100
        assert cache._file_for(key, tier="hot").read_bytes() == b"a" * 100
        assert not cache._file_for(key, tier="warm").exists()

    trio.run(scenario)


def test_lease_acquired_during_staging_aborts_demotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        cache = make_cache(tmp_path)
        await cache.put("movie", 0, b"a" * 100)
        stage = cache._stage_demote_hot_to_warm_temp

        def stage_with_new_lease(key: str) -> tuple[Path | None, Path | None]:
            paths = stage(key)
            cache.acquire_lease(
                stream_id="playback", cache_key="movie", start=0, size=100
            )
            return paths

        monkeypatch.setattr(
            cache, "_stage_demote_hot_to_warm_temp", stage_with_new_lease
        )
        await cache._ensure_hot_capacity(200)
        key = cache._key("movie", 0)
        assert cache._index[key].tier == "hot"
        assert cache._hot_bytes == 100
        assert cache._file_for(key, tier="hot").read_bytes() == b"a" * 100
        assert not cache._file_for(key, tier="warm").exists()
        assert not list((tmp_path / "warm").rglob("*.tmp"))

    trio.run(scenario)


def test_reader_acquired_during_staging_aborts_demotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        cache = make_cache(tmp_path)
        await cache.put("movie", 0, b"a" * 100)
        stage = cache._stage_demote_hot_to_warm_temp
        reader_cm = None

        def stage_with_new_reader(key: str) -> tuple[Path | None, Path | None]:
            nonlocal reader_cm
            paths = stage(key)
            reader_cm = cache.reading_chunk(key)
            reader_cm.__enter__()
            return paths

        monkeypatch.setattr(
            cache, "_stage_demote_hot_to_warm_temp", stage_with_new_reader
        )
        try:
            await cache._ensure_hot_capacity(200)
            key = cache._key("movie", 0)
            assert cache._index[key].tier == "hot"
            assert cache._hot_bytes == 100
            assert cache._file_for(key, tier="hot").read_bytes() == b"a" * 100
            assert not cache._file_for(key, tier="warm").exists()
            assert not list((tmp_path / "warm").rglob("*.tmp"))
        finally:
            if reader_cm is not None:
                reader_cm.__exit__(None, None, None)

    trio.run(scenario)


def test_lease_expiry_allows_hot_to_warm_demotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        cache = make_cache(tmp_path)
        await cache.put("movie", 0, b"a" * 100, stream_id="playback")
        key = cache._key("movie", 0)

        # While active, lease protects chunk from demotion
        await cache._ensure_hot_capacity(200)
        assert cache._index[key].tier == "hot"

        # Simulate time passing past lease ttl (default ttl is 30.0s)
        import time

        orig_monotonic = time.monotonic
        monkeypatch.setattr(time, "monotonic", lambda: orig_monotonic() + 100.0)

        # Lease is now expired; chunk becomes eligible for hot-to-warm demotion
        await cache._ensure_hot_capacity(200)
        assert cache._index[key].tier == "warm"
        assert cache._hot_bytes == 0
        assert cache._file_for(key, tier="warm").read_bytes() == b"a" * 100
        assert not cache._file_for(key, tier="hot").exists()

    trio.run(scenario)
