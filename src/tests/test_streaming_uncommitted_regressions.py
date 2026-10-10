"""Behavior-preservation checks for the streaming cleanup diff."""

from types import SimpleNamespace

import pytest
import trio

from program.services.streaming import http_pool
from program.services.streaming.adaptive_prefetch import AdaptivePrefetchManager
from program.services.streaming.media_stream import (
    MediaStream,
    _DeliveryRegistry,
    _DeliveryState,
)


@pytest.mark.parametrize("mount_scoped", [False, True])
@pytest.mark.parametrize("kind", ["body", "scan"])
@pytest.mark.parametrize("outcome", ["normal", "exception", "cancel"])
def test_admission_releases_tokens(monkeypatch, mount_scoped, kind, outcome):
    async def exercise():
        total = trio.CapacityLimiter(2)
        body = trio.CapacityLimiter(1)
        if mount_scoped:
            pool = http_pool.TrioStreamingHttpPool.__new__(
                http_pool.TrioStreamingHttpPool
            )
            pool._total_limiter = total
            pool._body_limiter = body
            pool._foreground_pressure = None
            admit = pool.admit
        else:
            monkeypatch.setattr(http_pool, "_get_limiters", lambda: (total, body))
            admit = http_pool.admit_stream_request

        async def request():
            async with admit(kind):
                assert total.borrowed_tokens == 1
                assert body.borrowed_tokens == (1 if kind == "body" else 0)
                if outcome == "exception":
                    raise ValueError("body failed")
                if outcome == "cancel":
                    scope.cancel()
                    await trio.lowlevel.checkpoint()

        with trio.CancelScope() as scope:
            if outcome == "exception":
                with pytest.raises(ValueError, match="body failed"):
                    await request()
            else:
                await request()
        assert scope.cancelled_caught == (outcome == "cancel")
        assert total.borrowed_tokens == body.borrowed_tokens == 0

    trio.run(exercise)


@pytest.mark.parametrize("mount_scoped", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
def test_second_acquire_failure_releases_total(monkeypatch, mount_scoped, cancel):
    async def exercise():
        total = trio.CapacityLimiter(2)

        class FailingBody:
            borrowed_tokens = 0
            total_tokens = 1
            entered = False

            async def __aenter__(self):
                self.entered = True
                assert total.borrowed_tokens == 1
                if cancel:
                    scope.cancel()
                    await trio.lowlevel.checkpoint()
                raise RuntimeError("second acquire failed")

            async def __aexit__(self, *args):
                pytest.fail("unsuccessful acquire must not be released")

        body = FailingBody()
        if mount_scoped:
            pool = http_pool.TrioStreamingHttpPool.__new__(
                http_pool.TrioStreamingHttpPool
            )
            pool._total_limiter = total
            pool._body_limiter = body
            pool._foreground_pressure = None
            admit = pool.admit
        else:
            monkeypatch.setattr(http_pool, "_get_limiters", lambda: (total, body))
            admit = http_pool.admit_stream_request

        async def request():
            async with admit("body"):
                pytest.fail("request body must not run")

        with trio.CancelScope() as scope:
            if cancel:
                await request()
            else:
                with pytest.raises(RuntimeError, match="second acquire failed"):
                    await request()
        assert body.entered
        assert scope.cancelled_caught == cancel
        assert total.borrowed_tokens == 0

    trio.run(exercise)


def test_retire_all_wakes_pending_and_ready_entries_and_is_idempotent():
    async def exercise():
        registry = _DeliveryRegistry()
        pending = await registry.register_pending(0)
        ready = await registry.register_pending(16)
        await registry.publish(16, b"payload")
        await registry.retire_all()
        assert registry._entries == {}
        for entry in (pending, ready):
            assert entry.state == _DeliveryState.RETIRED
            assert entry.ready.is_set()
            assert await registry.get_ready_payload(entry.start) is None
        await registry.retire_all()
        replacement = await registry.register_pending(0)
        assert replacement is not pending
        assert not replacement.ready.is_set()

    trio.run(exercise)


@pytest.mark.parametrize(
    "created,now,expected",
    [
        (-1.0, 100.0, False),
        (0.0, 100.0, False),
        (10.0, 40.0, False),
        (10.0, 40.001, True),
        (110.0, 100.0, False),
    ],
)
def test_unread_stream_timestamp_guard(monkeypatch, created, now, expected):
    stream = MediaStream.__new__(MediaStream)
    stream._created_at = created
    stream.recent_reads = SimpleNamespace(current_read=SimpleNamespace(value=None))
    stream.config = SimpleNamespace(activity_timeout_seconds=60)
    monkeypatch.setattr(trio, "current_time", lambda: now)
    assert stream.is_timed_out is expected


def test_timestamp_diagnostics_outside_trio():
    stream = MediaStream.__new__(MediaStream)
    assert stream.is_timed_out is False


@pytest.mark.parametrize(
    "usage,protected,expected",
    [
        (70.0, None, 48),
        (90.0, None, 24),
        (96.0, None, 4),
        (98.0, 0.0, 48),
        (98.0, 90.0, 24),
        (0.0, 96.0, 4),
    ],
)
def test_numeric_pressure_precedence(usage, protected, expected):
    manager = AdaptivePrefetchManager(initial_bitrate=1_000_000_000)
    assert (
        manager.calculate_window(
            cache_usage_pct=usage,
            cache_protected_pct=protected,
        )
        == expected
    )


def test_aborted_demotion_unlinks_bound_warm_payload_and_metadata(
    tmp_path, monkeypatch
):
    from program.services.streaming.cache import CACHE_META_SUFFIX, Cache, CacheConfig

    cache = Cache(
        CacheConfig(
            cache_dir=tmp_path / "warm",
            max_size_bytes=4096,
            hot_dir=tmp_path / "hot",
            hot_max_size_bytes=300,
            metrics_enabled=False,
        )
    )
    original = cache._place_staged_demotion
    placed = []

    def protect_after_placement(key, payload, meta):
        result = original(key, payload, meta)
        assert result
        placed.append(key)
        with cache._thread_lock:
            cache._active_readers[key] = 1
        return result

    async def exercise():
        await cache.put("movie", 0, b"a" * 100)
        await cache.put("movie", 100, b"b" * 100)
        monkeypatch.setattr(cache, "_place_staged_demotion", protect_after_placement)
        await cache._ensure_hot_capacity(300)
        assert len(placed) == 2
        for key in placed:
            assert cache._index[key].tier == "hot"
            assert cache._file_for(key, tier="hot").exists()
            assert not cache._file_for(key, tier="warm").exists()
            meta = cache._metadata_file_for(key, tier="warm")
            assert CACHE_META_SUFFIX == meta.suffix == ".meta"
            assert not meta.exists()

    trio.run(exercise)
