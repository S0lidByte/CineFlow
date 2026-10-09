"""Real worker and limiter telemetry; no throughput certification."""

import trio
from trio.testing import wait_all_tasks_blocked

from program.services.streaming.cache import Cache, CacheConfig


def test_limiter_queue_is_not_physical_read_time(tmp_path):
    async def exercise():
        cache = Cache(CacheConfig(cache_dir=tmp_path))
        puts = []
        reads = []
        cache._on_put_latency = puts.append
        cache._on_read_latency = reads.append
        await cache.put("movie", 0, b"x" * 1024)
        assert puts[0].payload_write_s > 0
        assert puts[0].meta_write_s > 0
        cache._read_limiter.total_tokens = 1
        await cache._read_limiter.acquire()
        async with trio.open_nursery() as nursery:

            async def reader():
                assert await cache.get("movie", 0, 127) == b"x" * 128

            nursery.start_soon(reader)
            await wait_all_tasks_blocked()
            await trio.sleep(0.03)
            cache._read_limiter.release()
        assert reads[0].limiter_wait_s >= 0.025
        assert reads[0].slice_exec_s < reads[0].limiter_wait_s
        assert reads[0].worker_sched_delay_s < reads[0].limiter_wait_s
        assert cache._read_limiter._task_wait_times == {}

    trio.run(exercise)


def test_cancelled_queued_reader_releases_telemetry(tmp_path):
    async def exercise():
        cache = Cache(CacheConfig(cache_dir=tmp_path))
        await cache.put("movie", 0, b"x")
        cache._read_limiter.total_tokens = 1
        await cache._read_limiter.acquire()
        async with trio.open_nursery() as nursery:
            nursery.start_soon(cache.get, "movie", 0, 0)
            await wait_all_tasks_blocked()
            nursery.cancel_scope.cancel()
        cache._read_limiter.release()
        assert cache._read_limiter.borrowed_tokens == 0
        assert cache._read_limiter._task_wait_times == {}

    trio.run(exercise)
