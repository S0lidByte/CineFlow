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


def test_hot_capacity_congestion_falls_back_to_warm_without_blocking(
    tmp_path: Path,
) -> None:
    """When hot capacity is full, writers fall back to warm immediately without blocking."""
    warm = tmp_path / "warm"
    hot = tmp_path / "hot"
    cache = Cache(
        CacheConfig(
            cache_dir=warm,
            max_size_bytes=10 * 1024 * 1024,
            hot_dir=hot,
            hot_max_size_bytes=100,
            metrics_enabled=False,
        )
    )

    async def _run() -> None:
        with trio.fail_after(2):
            # Fill hot tier to capacity
            await cache.put("movie1", 0, b"a" * 100)
            assert cache._index[cache._key("movie1", 0)].tier == "hot"

            # Demotion target is movie1, but if demotion fails or hot capacity is 0,
            # second put falls back immediately to warm tier without waiting
            await cache.put("movie2", 0, b"b" * 100)
            key2 = cache._key("movie2", 0)
            # Entry exists in cache and was written safely
            assert key2 in cache._index
            assert await cache.get("movie2", 0, 99) == b"b" * 100

    trio.run(_run)


def test_unlink_evicted_files_batches_per_title_shard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Evicted file unlinking groups entries by cache_key and submits batches to threadpool."""
    warm = tmp_path / "warm"
    hot = tmp_path / "hot"
    cache = Cache(
        CacheConfig(
            cache_dir=warm,
            max_size_bytes=10 * 1024 * 1024,
            hot_dir=hot,
            hot_max_size_bytes=10 * 1024 * 1024,
            metrics_enabled=False,
        )
    )

    unlinked_batches: list[list[str]] = []
    real_unlink = cache._unlink_cache_files

    def recording_unlink(keys: list[str], tiers: dict[str, str]) -> None:
        unlinked_batches.append(list(keys))
        real_unlink(keys, tiers=tiers)

    monkeypatch.setattr(cache, "_unlink_cache_files", recording_unlink)

    entries = [
        ("movie1", 0),
        ("movie1", 1000),
        ("movie1", 2000),
        ("movie2", 0),
        ("movie2", 1000),
    ]
    tiers = {cache._key(ck, st): "hot" for ck, st in entries}

    async def _run() -> None:
        await cache._unlink_evicted_files(entries, tiers=tiers)

    trio.run(_run)

    # Must be batched by cache_key: exactly 2 worker calls (one for movie1, one for movie2)
    assert len(unlinked_batches) == 2
    movie1_keys = [cache._key("movie1", st) for st in (0, 1000, 2000)]
    movie2_keys = [cache._key("movie2", st) for st in (0, 1000)]
    assert unlinked_batches[0] == movie1_keys
    assert unlinked_batches[1] == movie2_keys


def test_same_title_distinct_chunks_write_concurrently(tmp_path: Path) -> None:
    """Distinct chunk offsets of the same title must overlap via per-chunk shards."""
    cache = Cache(
        CacheConfig(
            cache_dir=tmp_path / "warm",
            max_size_bytes=10 * 1024 * 1024,
            hot_dir=tmp_path / "hot",
            hot_max_size_bytes=10 * 1024 * 1024,
            metrics_enabled=False,
        )
    )
    offsets: list[int] = []
    seen_buckets: set[int] = set()
    candidate = 0
    while len(offsets) < 3:
        bucket = cache._shard_bucket(cache._key("same_movie.mkv", candidate))
        if bucket not in seen_buckets:
            seen_buckets.add(bucket)
            offsets.append(candidate)
        candidate += 1024 * 1024

    barrier = threading.Barrier(3)
    real_write = cache._write_file_bytes

    def concurrent_write(path: Path, data: bytes) -> None:
        barrier.wait(timeout=2)
        real_write(path, data)

    cache._write_file_bytes = concurrent_write  # type: ignore[method-assign]

    async def _run() -> None:
        with trio.fail_after(2):
            async with trio.open_nursery() as nursery:
                for idx, off in enumerate(offsets):
                    payload = bytes([65 + idx]) * 128
                    nursery.start_soon(cache.put, "same_movie.mkv", off, payload)

        for idx, off in enumerate(offsets):
            assert await cache.get("same_movie.mkv", off, off + 127) == (
                bytes([65 + idx]) * 128
            )

    trio.run(_run)


def test_hot_demotion_congestion_concurrent_writers_fallback_to_warm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent writers must fall back to warm while one writer is stalled in hot demotion."""
    cache = Cache(
        CacheConfig(
            cache_dir=tmp_path / "warm",
            max_size_bytes=10 * 1024 * 1024,
            hot_dir=tmp_path / "hot",
            hot_max_size_bytes=150,
            metrics_enabled=False,
        )
    )
    demote_started = trio.Event()
    allow_demote = threading.Event()
    real_stage = cache._stage_demote_hot_to_warm_temp

    def blocking_stage(key: str):
        trio.from_thread.run_sync(demote_started.set)
        assert allow_demote.wait(5)
        return real_stage(key)

    monkeypatch.setattr(cache, "_stage_demote_hot_to_warm_temp", blocking_stage)

    async def _run() -> None:
        await cache.put("movie.mkv", 0, b"a" * 100)
        with trio.fail_after(2):
            async with trio.open_nursery() as nursery:
                nursery.start_soon(cache.put, "movie.mkv", 1000, b"b" * 100)
                await demote_started.wait()
                # Second writer must not queue on _hot_write_lock; falls back to warm immediately.
                await cache.put("movie.mkv", 2000, b"c" * 100)
                k2 = cache._key("movie.mkv", 2000)
                assert cache._index[k2].tier == "warm"
                allow_demote.set()

        assert await cache.get("movie.mkv", 2000, 2099) == b"c" * 100

    trio.run(_run)


def test_evict_lru_skips_index_lock_when_no_deficit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_evict_lru fast-path must return True without acquiring global _index_lock when within budget."""
    cache = Cache(
        CacheConfig(
            cache_dir=tmp_path,
            max_size_bytes=10 * 1024 * 1024,
            metrics_enabled=False,
        )
    )
    orig_locks = cache.locks
    lock_entries = 0

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def counting_locks():
        nonlocal lock_entries
        lock_entries += 1
        async with orig_locks():
            yield

    monkeypatch.setattr(cache, "locks", counting_locks)

    async def _run() -> None:
        assert await cache._evict_lru(1024) is True

    trio.run(_run)
    assert lock_entries == 0


def test_concurrent_trim_and_reserve_demotion_preserves_warm_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent trim() and write demotion must never race or delete published warm files."""
    cache = Cache(
        CacheConfig(
            cache_dir=tmp_path / "warm",
            max_size_bytes=10 * 1024 * 1024,
            hot_dir=tmp_path / "hot",
            hot_max_size_bytes=150,
            metrics_enabled=False,
        )
    )
    demote_calls = 0
    active_demotions = 0
    max_concurrent_demotions = 0
    trim_staging = trio.Event()
    allow_trim_finish = threading.Event()
    orig_stage = cache._stage_demote_hot_to_warm_temp

    def tracking_stage(key: str):
        nonlocal demote_calls, active_demotions, max_concurrent_demotions
        demote_calls += 1
        active_demotions += 1
        max_concurrent_demotions = max(max_concurrent_demotions, active_demotions)
        try:
            trio.from_thread.run_sync(trim_staging.set)
            assert allow_trim_finish.wait(5)
            return orig_stage(key)
        finally:
            active_demotions -= 1

    monkeypatch.setattr(cache, "_stage_demote_hot_to_warm_temp", tracking_stage)

    async def _run() -> None:
        await cache.put("movie.mkv", 0, b"x" * 140)
        with trio.fail_after(2):
            async with trio.open_nursery() as nursery:
                nursery.start_soon(cache.trim)
                await trim_staging.wait()
                # Launch both another _ensure_hot_capacity and a put() concurrently
                # while trim() is mid-stage; neither may double-demote or unlink warm_fp.
                nursery.start_soon(cache._ensure_hot_capacity, 0)
                nursery.start_soon(cache.put, "movie.mkv", 1000, b"y" * 100)
                await trio.sleep(0.02)
                allow_trim_finish.set()

        k0 = cache._key("movie.mkv", 0)
        assert cache._index[k0].tier == "warm"
        assert cache._file_for(k0, tier="warm").exists()
        assert await cache.get("movie.mkv", 0, 139) == b"x" * 140
        assert max_concurrent_demotions == 1
        assert demote_calls == 1

    trio.run(_run)


def test_put_warm_fallback_overwrite_cleans_stale_hot_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Overwriting a shorter hot entry on warm fallback must delete the orphaned hot file."""
    cache = Cache(
        CacheConfig(
            cache_dir=tmp_path / "warm",
            max_size_bytes=10 * 1024 * 1024,
            hot_dir=tmp_path / "hot",
            hot_max_size_bytes=1024,
            metrics_enabled=False,
        )
    )

    async def _run() -> None:
        k = cache._key("movie.mkv", 0)
        await cache.put("movie.mkv", 0, b"s" * 100)
        assert cache._index[k].tier == "hot"
        assert cache._file_for(k, tier="hot").exists()

        # Simulate hot congestion forcing a larger overwrite onto warm tier.
        async def force_warm(*args, **kwargs) -> bool:
            return False

        monkeypatch.setattr(cache, "_reserve_hot_capacity", force_warm)
        await cache.put("movie.mkv", 0, b"L" * 200)

        assert cache._index[k].tier == "warm"
        assert cache._index[k].size == 200
        assert cache._hot_bytes == 0
        assert not cache._file_for(k, tier="hot").exists()
        assert not cache._metadata_file_for(k, tier="hot").exists()
        assert cache._file_for(k, tier="warm").exists()
        assert await cache.get("movie.mkv", 0, 199) == b"L" * 200

    trio.run(_run)


def test_demotion_shard_serializes_concurrent_warm_fallback_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Demotion Phase 2..4 holds per-chunk shard so concurrent warm-fallback upgrade never loses or reverts bytes."""
    cache = Cache(
        CacheConfig(
            cache_dir=tmp_path / "warm",
            max_size_bytes=10 * 1024 * 1024,
            hot_dir=tmp_path / "hot",
            hot_max_size_bytes=150,
            metrics_enabled=False,
        )
    )
    stage_done = trio.Event()
    resume_after_stage = threading.Event()
    orig_stage = cache._stage_demote_hot_to_warm_temp

    def pausing_stage(key: str):
        res = orig_stage(key)
        trio.from_thread.run_sync(stage_done.set)
        assert resume_after_stage.wait(5)
        return res

    # Scenario A: writer upgrades k0 via warm fallback right after Phase 1 staging
    # (before _ensure_hot_capacity acquires _shard(k0) for Phase 2..4).
    monkeypatch.setattr(cache, "_stage_demote_hot_to_warm_temp", pausing_stage)

    async def _run_scenario_a() -> None:
        k0 = cache._key("movie.mkv", 0)
        await cache.put("movie.mkv", 0, b"a" * 140)
        assert cache._index[k0].tier == "hot"

        with trio.fail_after(2):
            async with trio.open_nursery() as nursery:
                nursery.start_soon(cache.trim)
                await stage_done.wait()
                # Concurrent warm-fallback overwrite while _hot_demotion_in_progress=True
                await cache.put("movie.mkv", 0, b"Z" * 180)
                assert cache._index[k0].tier == "warm"
                assert cache._index[k0].size == 180
                resume_after_stage.set()

        assert cache._index[k0].tier == "warm"
        assert cache._index[k0].size == 180
        assert await cache.get("movie.mkv", 0, 179) == b"Z" * 180

    trio.run(_run_scenario_a)

    # Scenario B: writer attempts upgrade while Phase 2..4 is already executing;
    # _shard(k0) serializes Phase 2..4 before writer enters.
    monkeypatch.undo()
    cache2 = Cache(
        CacheConfig(
            cache_dir=tmp_path / "warm2",
            max_size_bytes=10 * 1024 * 1024,
            hot_dir=tmp_path / "hot2",
            hot_max_size_bytes=150,
            metrics_enabled=False,
        )
    )
    place_entered = trio.Event()
    allow_place = threading.Event()
    orig_place = cache2._place_staged_demotion
    writer_finished_before_place = False

    def blocking_place(key: str, staged_payload, staged_meta):
        trio.from_thread.run_sync(place_entered.set)
        assert allow_place.wait(5)
        return orig_place(key, staged_payload, staged_meta)

    monkeypatch.setattr(cache2, "_place_staged_demotion", blocking_place)

    async def _run_scenario_b() -> None:
        nonlocal writer_finished_before_place
        k0 = cache2._key("movie2.mkv", 0)
        await cache2.put("movie2.mkv", 0, b"m" * 140)

        async def _writer() -> None:
            nonlocal writer_finished_before_place
            await cache2.put("movie2.mkv", 0, b"N" * 180)
            if not allow_place.is_set():
                writer_finished_before_place = True

        with trio.fail_after(2):
            async with trio.open_nursery() as nursery:
                nursery.start_soon(cache2.trim)
                await place_entered.wait()
                nursery.start_soon(_writer)
                await wait_all_tasks_blocked()
                allow_place.set()

        assert not writer_finished_before_place
        assert cache2._index[k0].tier == "warm"
        assert cache2._index[k0].size == 180
        assert await cache2.get("movie2.mkv", 0, 179) == b"N" * 180
        assert not cache2._file_for(k0, tier="hot").exists()

    trio.run(_run_scenario_b)


def test_demotion_cancellation_after_placement_cleans_warm_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelling demotion between Phase 2 placement and Phase 3 commit unlinks warm orphan cleanly."""
    from contextlib import asynccontextmanager

    cache = Cache(
        CacheConfig(
            cache_dir=tmp_path / "warm",
            max_size_bytes=10 * 1024 * 1024,
            hot_dir=tmp_path / "hot",
            hot_max_size_bytes=150,
            metrics_enabled=False,
        )
    )
    orig_locks = cache.locks
    locks_calls = 0
    phase3_entered = trio.Event()

    @asynccontextmanager
    async def intercepted_locks():
        nonlocal locks_calls
        locks_calls += 1
        # Call #1: candidate selection, #2: pre-stage fence, #3: pre-Phase2 revalidation, #4: Phase 3 commit
        if locks_calls == 4:
            phase3_entered.set()
            await trio.sleep_forever()
        async with orig_locks():
            yield

    async def _run() -> None:
        k0 = cache._key("cancel.mkv", 0)
        await cache.put("cancel.mkv", 0, b"c" * 140)
        monkeypatch.setattr(cache, "locks", intercepted_locks)

        with trio.fail_after(2):
            async with trio.open_nursery() as nursery:
                nursery.start_soon(cache.trim)
                await phase3_entered.wait()
                # Phase 2 already placed warm_fp; cancel trim before Phase 3 index flip.
                nursery.cancel_scope.cancel()

        assert cache._index[k0].tier == "hot"
        assert cache._file_for(k0, tier="hot").exists()
        assert not cache._file_for(k0, tier="warm").exists()
        assert not cache._metadata_file_for(k0, tier="warm").exists()
        assert cache._hot_demotion_in_progress is False
        assert await cache.get("cancel.mkv", 0, 139) == b"c" * 140

    trio.run(_run)
