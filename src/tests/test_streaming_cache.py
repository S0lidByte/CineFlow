"""Characterization tests for the disk-backed streaming Cache."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import trio

from program.services.streaming.cache import Cache, CacheConfig


def _make_cache(
    cache_dir: Path,
    *,
    max_size_bytes: int = 10 * 1024 * 1024,
    eviction: str = "LRU",
    ttl_seconds: int = 3600,
    metrics_enabled: bool = True,
) -> Cache:
    return Cache(
        CacheConfig(
            cache_dir=cache_dir,
            max_size_bytes=max_size_bytes,
            eviction=eviction,  # type: ignore[arg-type]
            ttl_seconds=ttl_seconds,
            metrics_enabled=metrics_enabled,
        )
    )


def test_cache_put_get_exact_hit(tmp_path: Path) -> None:
    cache = _make_cache(tmp_path)
    payload = b"abcdefghij" * 100

    async def _run() -> None:
        await cache.put("movie.mkv", 0, payload)
        got = await cache.get("movie.mkv", 0, len(payload) - 1)
        assert got == payload
        stats = await cache.stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 0
        assert stats["bytes_from_cache"] == len(payload)
        assert stats["bytes_written"] == len(payload)
        assert stats["entries"] == 1

    trio.run(_run)


def test_cache_partial_slice_within_chunk(tmp_path: Path) -> None:
    cache = _make_cache(tmp_path)
    payload = bytes(range(256))

    async def _run() -> None:
        await cache.put("movie.mkv", 0, payload)
        got = await cache.get("movie.mkv", 10, 19)
        assert got == payload[10:20]
        stats = await cache.stats()
        assert stats["hits"] == 1

    trio.run(_run)


def test_cache_cross_chunk_stitch(tmp_path: Path) -> None:
    cache = _make_cache(tmp_path)
    first = b"A" * 100
    second = b"B" * 100

    async def _run() -> None:
        await cache.put("movie.mkv", 0, first)
        await cache.put("movie.mkv", 100, second)
        got = await cache.get("movie.mkv", 90, 109)
        assert got == (b"A" * 10) + (b"B" * 10)
        stats = await cache.stats()
        assert stats["hits"] >= 1
        assert stats["entries"] == 2

    trio.run(_run)


def test_cache_overlapping_fallback_does_not_shadow_covering_chunk(
    tmp_path: Path,
) -> None:
    """A small fallback slice must not hide an older full media chunk."""
    cache = _make_cache(tmp_path)
    full_chunk = bytes(range(128))
    fallback = b"Z" * 16
    next_chunk = b"N" * 32

    async def _run() -> None:
        await cache.put("movie.mkv", 0, full_chunk)
        await cache.put("movie.mkv", 32, fallback)
        await cache.put("movie.mkv", 128, next_chunk)

        assert cache.has("movie.mkv", 0, 79) is True
        assert await cache.get("movie.mkv", 64, 79) == full_chunk[64:80]
        assert await cache.get("movie.mkv", 24, 79) == full_chunk[24:80]
        assert await cache.get("movie.mkv", 32, 159) == full_chunk[32:] + next_chunk

    trio.run(_run)


def test_cache_shorter_same_start_does_not_replace_full_chunk(
    tmp_path: Path,
) -> None:
    cache = _make_cache(tmp_path)
    full_chunk = bytes(range(128))

    async def _run() -> None:
        await cache.put("movie.mkv", 0, full_chunk)
        await cache.put("movie.mkv", 0, full_chunk[:16])

        assert cache.has("movie.mkv", 0, 127) is True
        assert await cache.get("movie.mkv", 64, 127) == full_chunk[64:128]
        stats = await cache.stats()
        assert stats["entries"] == 1
        assert stats["bytes_written"] == len(full_chunk)

    trio.run(_run)


def test_cache_miss_returns_empty(tmp_path: Path) -> None:
    cache = _make_cache(tmp_path)

    async def _run() -> None:
        got = await cache.get("missing.mkv", 0, 99)
        assert got == b""
        stats = await cache.stats()
        assert stats["misses"] == 1
        assert stats["hits"] == 0

    trio.run(_run)


def test_cache_has_reports_coverage(tmp_path: Path) -> None:
    cache = _make_cache(tmp_path)

    async def _run() -> None:
        await cache.put("movie.mkv", 0, b"0123456789")
        assert cache.has("movie.mkv", 0, 9) is True
        assert cache.has("movie.mkv", 0, 10) is False
        assert cache.has("other.mkv", 0, 9) is False

    trio.run(_run)


def test_cache_has_rejects_truncated_payload(tmp_path: Path) -> None:
    cache = _make_cache(tmp_path)

    async def _run() -> None:
        await cache.put("movie.mkv", 0, b"0123456789")
        key = cache._key("movie.mkv", 0)
        cache._file_for(key).write_bytes(b"0123")

        assert cache.has("movie.mkv", 0, 9) is False
        assert await cache.get("movie.mkv", 0, 9) == b""

    trio.run(_run)


def test_cache_lru_eviction(tmp_path: Path) -> None:
    # Each put is 60 bytes; max 100 bytes forces eviction of the oldest entry.
    cache = _make_cache(tmp_path, max_size_bytes=100)

    async def _run() -> None:
        await cache.put("movie.mkv", 0, b"1" * 60)
        await cache.put("movie.mkv", 60, b"2" * 60)
        stats = await cache.stats()
        assert stats["entries"] == 1
        assert stats["evictions"] >= 1
        assert stats["total_bytes"] <= 100
        # First chunk should be gone
        assert await cache.get("movie.mkv", 0, 59) == b""
        # Second chunk should remain
        assert await cache.get("movie.mkv", 60, 119) == b"2" * 60

    trio.run(_run)


def test_cache_ttl_eviction(tmp_path: Path) -> None:
    cache = _make_cache(tmp_path, eviction="TTL", ttl_seconds=1)

    async def _run() -> None:
        await cache.put("movie.mkv", 0, b"ttl-data-123456")
        assert await cache.get("movie.mkv", 0, 14) == b"ttl-data-123456"
        # Age the entry past TTL
        key = cache._key("movie.mkv", 0)
        entry = cache._index[key]
        cache._index[key] = type(entry)(
            key=entry.key,
            cache_key=entry.cache_key,
            start=entry.start,
            size=entry.size,
            mtime=time.time() - 10,
        )
        await cache.trim()
        assert await cache.get("movie.mkv", 0, 14) == b""
        stats = await cache.stats()
        assert stats["evictions"] >= 1

    trio.run(_run)


def test_cache_rebuilds_index_on_restart(tmp_path: Path) -> None:
    payload = b"persist-me-please!!"

    # Cache.__init__ calls trio.run(_initialize); construct outside nested runs.
    cache = _make_cache(tmp_path)

    async def _seed() -> None:
        await cache.put("movie.mkv", 0, payload)

    trio.run(_seed)

    reloaded = _make_cache(tmp_path)

    async def _reload() -> None:
        got = await reloaded.get("movie.mkv", 0, len(payload) - 1)
        assert got == payload
        stats = await reloaded.stats()
        assert stats["entries"] == 1
        assert stats["hits"] == 1

    trio.run(_reload)


def test_cache_initial_scan_preserves_unknown_files(tmp_path: Path) -> None:
    direct_file = tmp_path / "shared-memory-owner.txt"
    nested_file = tmp_path / "other-service" / "payload.bin"
    direct_file.write_text("keep me", encoding="utf-8")
    nested_file.parent.mkdir()
    nested_file.write_bytes(b"keep me too")

    _make_cache(tmp_path)

    assert direct_file.read_text(encoding="utf-8") == "keep me"
    assert nested_file.read_bytes() == b"keep me too"


def test_cache_initial_scan_removes_cache_shaped_orphan(tmp_path: Path) -> None:
    key = "ab" + ("1" * 38)
    orphan = tmp_path / key[:2] / key
    orphan.parent.mkdir()
    orphan.write_bytes(b"incomplete cache write")

    _make_cache(tmp_path)

    assert orphan.exists() is False


def test_cache_get_miss_does_not_create_fanout_dirs(tmp_path: Path) -> None:
    """Reads must not mkdir fanout dirs (previously held the global lock)."""
    cache = _make_cache(tmp_path)

    async def _run() -> None:
        assert await cache.get("missing.mkv", 0, 99) == b""

    trio.run(_run)
    assert list(tmp_path.iterdir()) == []


def test_cache_concurrent_gets_hit(tmp_path: Path) -> None:
    cache = _make_cache(tmp_path)
    payload = b"concurrent-hit-payload!!"

    async def _run() -> None:
        await cache.put("movie.mkv", 0, payload)
        results: list[bytes] = []

        async def one() -> None:
            results.append(await cache.get("movie.mkv", 0, len(payload) - 1))

        async with trio.open_nursery() as nursery:
            for _ in range(24):
                nursery.start_soon(one)

        assert len(results) == 24
        assert all(r == payload for r in results)
        stats = await cache.stats()
        assert stats["hits"] == 24

    trio.run(_run)


def test_cache_concurrent_titles_use_different_shards(tmp_path: Path) -> None:
    """Different cache_keys must map to independent shard locks and coexist."""
    cache = _make_cache(tmp_path)

    candidates = [
        ("title-a.mkv", "title-b.mkv"),
        ("show1.mkv", "movie2.mkv"),
        ("alpha.mkv", "omega.mkv"),
        ("x", "y"),
    ]
    found_pair = None
    for ka, kb in candidates:
        if cache._shard_for(ka) is not cache._shard_for(kb):
            found_pair = (ka, kb)
            break
    assert found_pair is not None, "expected at least one distinct-shard key pair"

    ka, kb = found_pair
    payload_a = b"AAAA" * 50
    payload_b = b"BBBB" * 50

    async def _run() -> None:
        await cache.put(ka, 0, payload_a)
        await cache.put(kb, 0, payload_b)

        results: dict[str, list[bytes]] = {ka: [], kb: []}

        async def read_a() -> None:
            results[ka].append(await cache.get(ka, 0, len(payload_a) - 1))

        async def read_b() -> None:
            results[kb].append(await cache.get(kb, 0, len(payload_b) - 1))

        async with trio.open_nursery() as nursery:
            for _ in range(12):
                nursery.start_soon(read_a)
                nursery.start_soon(read_b)

        assert all(r == payload_a for r in results[ka])
        assert all(r == payload_b for r in results[kb])
        assert len(results[ka]) == 12
        assert len(results[kb]) == 12

    trio.run(_run)


def test_cache_get_disk_io_off_trio_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """File reads must go through trio.to_thread so the FUSE loop stays free."""
    cache = _make_cache(tmp_path)
    payload = b"thread-offload-payload"
    calls: list[str] = []
    real_to_thread = trio.to_thread.run_sync

    async def tracking_to_thread(fn, *args, **kwargs):
        calls.append(getattr(fn, "__name__", str(fn)))
        return await real_to_thread(fn, *args, **kwargs)

    monkeypatch.setattr(trio.to_thread, "run_sync", tracking_to_thread)

    async def _run() -> None:
        await cache.put("movie.mkv", 0, payload)
        got = await cache.get("movie.mkv", 0, len(payload) - 1)
        assert got == payload

    trio.run(_run)
    assert "_write_file_bytes" in calls
    assert "_read_file_slice" in calls


def test_cache_eviction_unlinks_without_holding_thread_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LRU/TTL unlink must run after index lock release (Plex multi-open)."""
    cache = _make_cache(tmp_path, max_size_bytes=100)
    held_during_unlink: list[bool] = []
    real_unlink = Path.unlink

    def tracking_unlink(self: Path, *args: object, **kwargs: object) -> None:
        held_during_unlink.append(cache._thread_lock.locked())
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", tracking_unlink)

    async def _run() -> None:
        await cache.put("movie.mkv", 0, b"1" * 60)
        await cache.put("movie.mkv", 60, b"2" * 60)

    trio.run(_run)
    assert held_during_unlink, "expected eviction to unlink at least one file"
    assert all(not held for held in held_during_unlink)
