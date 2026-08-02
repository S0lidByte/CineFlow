"""Two-tier hot/warm streaming cache."""

from __future__ import annotations

import threading
from pathlib import Path

import trio

from program.services.streaming.cache import Cache, CacheConfig, CacheEntry


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


def test_demotion_publishes_warm_tier_after_payload_move(tmp_path: Path) -> None:
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
    observed_moves: list[tuple[str, bool, bool, bool, bool]] = []
    real_demote = cache._demote_files_to_warm

    def observe_demotion(key: str) -> None:
        before_hot = cache._file_for(key, tier="hot").exists()
        before_warm = cache._file_for(key, tier="warm").exists()
        observed_tier = cache._index[key].tier
        real_demote(key)
        observed_moves.append(
            (
                observed_tier,
                before_hot,
                before_warm,
                cache._file_for(key, tier="hot").exists(),
                cache._file_for(key, tier="warm").exists(),
            )
        )

    cache._demote_files_to_warm = observe_demotion  # type: ignore[method-assign]

    async def _run() -> None:
        await cache.put("movie.mkv", 0, b"a" * 100)
        await cache.put("movie.mkv", 1000, b"b" * 100)

    trio.run(_run)

    assert observed_moves == [("hot", True, False, False, True)]


def test_partial_read_survives_hot_to_warm_index_handoff(tmp_path: Path) -> None:
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
    payload = bytes(range(100))

    async def _run() -> None:
        await cache.put("movie.mkv", 0, payload)
        key = cache._key("movie.mkv", 0)
        entry = cache._index[key]
        assert cache._file_for(key, tier="hot").exists()
        assert not cache._file_for(key, tier="warm").exists()

        # Reproduce the production race: the index observes the destination
        # tier while the payload is still available only in tmpfs.
        cache._index[key] = CacheEntry(
            key=entry.key,
            cache_key=entry.cache_key,
            start=entry.start,
            size=entry.size,
            mtime=entry.mtime,
            tier="warm",
        )

        assert cache.has("movie.mkv", 0, 99) is True
        assert await cache.get("movie.mkv", 10, 19) == payload[10:20]

    trio.run(_run)


def test_different_titles_write_hot_payloads_concurrently(tmp_path: Path) -> None:
    cache = Cache(
        CacheConfig(
            cache_dir=tmp_path / "warm",
            max_size_bytes=4096,
            hot_dir=tmp_path / "hot",
            hot_max_size_bytes=1024,
            metrics_enabled=False,
        )
    )
    write_barrier = threading.Barrier(2)
    real_write = cache._write_file_bytes

    def synchronized_write(path: Path, data: bytes) -> None:
        write_barrier.wait(timeout=2)
        real_write(path, data)

    cache._write_file_bytes = synchronized_write  # type: ignore[method-assign]

    async def _run() -> None:
        async with trio.open_nursery() as nursery:
            nursery.start_soon(cache.put, "first.mkv", 0, b"a" * 100)
            nursery.start_soon(cache.put, "second.mkv", 0, b"b" * 100)

        assert await cache.get("first.mkv", 0, 99) == b"a" * 100
        assert await cache.get("second.mkv", 0, 99) == b"b" * 100

    trio.run(_run)


def test_failed_hot_demotion_falls_back_to_warm(tmp_path: Path) -> None:
    cache = Cache(
        CacheConfig(
            cache_dir=tmp_path / "warm",
            max_size_bytes=4096,
            hot_dir=tmp_path / "hot",
            hot_max_size_bytes=150,
            metrics_enabled=False,
        )
    )

    async def _run() -> None:
        await cache.put("movie.mkv", 0, b"a" * 100)

        def fail_demotion(key: str) -> None:
            raise OSError(f"cannot demote {key}")

        cache._demote_files_to_warm = fail_demotion  # type: ignore[method-assign]
        await cache.put("movie.mkv", 1000, b"b" * 100)

        second_key = cache._key("movie.mkv", 1000)
        assert cache._index[second_key].tier == "warm"
        assert cache._file_for(second_key, tier="warm").exists()
        assert await cache.get("movie.mkv", 1000, 1099) == b"b" * 100

    trio.run(_run)
