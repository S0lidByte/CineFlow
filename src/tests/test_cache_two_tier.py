"""Two-tier hot/warm streaming cache."""

from __future__ import annotations

import inspect
import textwrap
import threading
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path

import pytest
import trio
from trio.testing import wait_all_tasks_blocked

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
    real_demote = cache._place_staged_demotion

    def observe_demotion(key: str, payload: Path, meta: Path) -> bool:
        before_hot = cache._file_for(key, tier="hot").exists()
        before_warm = cache._file_for(key, tier="warm").exists()
        observed_tier = cache._index[key].tier
        result = real_demote(key, payload, meta)
        assert cache._index[key].tier == "hot"
        observed_moves.append(
            (
                observed_tier,
                before_hot,
                before_warm,
                cache._file_for(key, tier="hot").exists(),
                cache._file_for(key, tier="warm").exists(),
            )
        )

        return result

    cache._place_staged_demotion = observe_demotion  # type: ignore[method-assign]

    async def _run() -> None:
        await cache.put("movie.mkv", 0, b"a" * 100)
        await cache.put("movie.mkv", 1000, b"b" * 100)

    trio.run(_run)

    assert observed_moves == [("hot", True, False, True, True)]
    key = cache._key("movie.mkv", 0)
    assert cache._index[key].tier == "warm"
    assert not cache._file_for(key, tier="hot").exists()
    assert cache._file_for(key, tier="warm").read_bytes() == b"a" * 100


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

        def fail_demotion(key: str, payload: Path, meta: Path) -> bool:
            return False

        cache._place_staged_demotion = fail_demotion  # type: ignore[method-assign]
        await cache.put("movie.mkv", 1000, b"b" * 100)

        second_key = cache._key("movie.mkv", 1000)
        assert cache._index[second_key].tier == "warm"
        assert cache._file_for(second_key, tier="warm").exists()
        assert await cache.get("movie.mkv", 1000, 1099) == b"b" * 100

    trio.run(_run)


@pytest.mark.parametrize("shielded", [True, False])
def test_put_cancellation_releases_hot_capacity(tmp_path: Path, shielded: bool) -> None:
    cache = Cache(
        CacheConfig(
            cache_dir=tmp_path / "warm",
            max_size_bytes=4096,
            hot_dir=tmp_path / "hot",
            hot_max_size_bytes=1024,
            metrics_enabled=False,
        )
    )
    if not shielded:
        # Exercise precisely the pre-fix finally block without modifying source on disk.
        source = textwrap.dedent(inspect.getsource(Cache._put))
        assert "with trio.CancelScope(shield=True):" in source
        source = source.replace(
            "with trio.CancelScope(shield=True):", "with nullcontext():"
        )
        namespace = {**Cache._put.__globals__, "nullcontext": nullcontext}
        exec(compile(source, "<unshielded-put>", "exec"), namespace)
        cache._put = namespace["_put"].__get__(cache, Cache)

    async def run() -> None:
        started = trio.Event()
        release = threading.Event()
        real_write = cache._write_file_bytes
        cancelled = False

        def blocked_write(path: Path, data: bytes) -> None:
            trio.from_thread.run_sync(started.set)
            assert release.wait(5)
            real_write(path, data)

        cache._write_file_bytes = blocked_write

        async def writer(*, task_status=trio.TASK_STATUS_IGNORED) -> None:
            nonlocal cancelled
            with trio.CancelScope() as scope:
                task_status.started(scope)
                try:
                    await cache.put("movie", 0, b"x" * 200)
                except trio.Cancelled:
                    cancelled = True
                    raise
                else:
                    pytest.fail("writer was not cancelled")

        with trio.fail_after(5):
            async with trio.open_nursery() as nursery:
                scope = await nursery.start(writer)
                try:
                    await started.wait()
                    assert cache._hot_reserved_bytes == 200
                    scope.cancel()
                finally:
                    release.set()
        assert cancelled
        assert cache._hot_reserved_bytes == (0 if shielded else 200)
        if shielded:
            await cache.put("next", 0, b"y" * 200)
            assert cache._hot_reserved_bytes == 0

    trio.run(run)


@pytest.mark.parametrize("policy", ["LRU", "TTL"])
@pytest.mark.parametrize("window", ["unpublished", "post_check"])
def test_delayed_eviction_preserves_replacement(tmp_path, monkeypatch, policy, window):
    cache = Cache(
        CacheConfig(
            cache_dir=tmp_path,
            max_size_bytes=150,
            eviction=policy,
            ttl_seconds=60,
            metrics_enabled=False,
        )
    )
    key = cache._key("movie", 0)
    payload_path = cache._file_for(key)
    real_unlink_evicted = cache._unlink_evicted_files
    real_write = cache._write_file_bytes
    real_path_unlink = Path.unlink

    async def run():
        await cache.put("movie", 0, b"a" * 100)
        if policy == "TTL":
            cache._index[key] = replace(cache._index[key], mtime=0)
        selected = trio.Event()
        checked = trio.Event()
        written = trio.Event()
        deleted = trio.Event()
        proceed = trio.Event()
        release_delete = threading.Event()
        release_write = threading.Event()

        async def delayed_unlink(entries, tiers):
            selected.set()
            await proceed.wait()
            try:
                await real_unlink_evicted(entries, tiers)
            finally:
                deleted.set()

        def paused_unlink(path, *args, **kwargs):
            if path == payload_path and window == "post_check":
                trio.from_thread.run_sync(checked.set)
                assert release_delete.wait(5)
            return real_path_unlink(path, *args, **kwargs)

        def paused_write(path, data):
            real_write(path, data)
            if data == b"c" * 100:
                trio.from_thread.run_sync(written.set)
                if window == "unpublished":
                    assert release_write.wait(5)

        monkeypatch.setattr(cache, "_unlink_evicted_files", delayed_unlink)
        monkeypatch.setattr(Path, "unlink", paused_unlink)
        monkeypatch.setattr(cache, "_write_file_bytes", paused_write)

        async def evict():
            if policy == "LRU":
                await cache._evict_lru(100)
            else:
                await cache._evict_ttl()

        with trio.fail_after(5):
            async with trio.open_nursery() as nursery:
                nursery.start_soon(evict)
                try:
                    await selected.wait()
                    assert key not in cache._index
                    if window == "post_check":
                        proceed.set()
                        await checked.wait()
                        nursery.start_soon(cache.put, "movie", 0, b"c" * 100)
                        await wait_all_tasks_blocked()
                        assert not written.is_set(), "writer crossed the deletion guard"
                        release_delete.set()
                    else:
                        nursery.start_soon(cache.put, "movie", 0, b"c" * 100)
                        await written.wait()
                        assert key not in cache._index
                        proceed.set()
                        await wait_all_tasks_blocked()
                        release_write.set()
                finally:
                    proceed.set()
                    release_delete.set()
                    release_write.set()
        assert deleted.is_set()
        assert key in cache._index
        assert payload_path.read_bytes() == b"c" * 100
        assert cache._read_metadata(key) == ("movie", 0)
        assert cache.has("movie", 0, 99)
        assert await cache.get("movie", 0, 99) == b"c" * 100
        assert cache._total_bytes == 100

    trio.run(run)
