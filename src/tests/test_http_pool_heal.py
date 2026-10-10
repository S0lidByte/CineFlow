"""HTTP pool admission and recycle auto-heal."""

from __future__ import annotations

from unittest.mock import patch

import httpx
import trio
from kink import di

from program.services.streaming import http_pool
from program.utils.async_client import AsyncClient
from program.utils.stream_http import MAX_BODY_STREAMS, MAX_TOTAL_STREAM_REQUESTS


def setup_function() -> None:
    http_pool.reset_http_pool_state_for_tests()


def teardown_function() -> None:
    http_pool.reset_http_pool_state_for_tests()
    if AsyncClient in di:
        try:
            del di[AsyncClient]
        except Exception:
            pass


def test_admit_stream_request_allows_under_cap():
    async def _run() -> None:
        async with http_pool.admit_stream_request("scan"):
            pass

    trio.run(_run)


def test_admit_body_saturated_raises_pool_timeout():
    async def _run() -> None:
        total, body = http_pool._get_limiters()
        tokens = [object() for _ in range(MAX_BODY_STREAMS)]
        for token in tokens:
            await body.acquire_on_behalf_of(token)

        try:
            async with http_pool.admit_stream_request("body"):
                raise AssertionError("should not acquire body slot")
        except httpx.PoolTimeout:
            pass
        finally:
            for token in tokens:
                body.release_on_behalf_of(token)

    trio.run(_run)


def test_admit_total_saturated_raises_pool_timeout():
    async def _run() -> None:
        total, _body = http_pool._get_limiters()
        tokens = [object() for _ in range(MAX_TOTAL_STREAM_REQUESTS)]
        for token in tokens:
            await total.acquire_on_behalf_of(token)

        try:
            async with http_pool.admit_stream_request("scan"):
                raise AssertionError("should not acquire scan slot")
        except httpx.PoolTimeout:
            pass
        finally:
            for token in tokens:
                total.release_on_behalf_of(token)

    trio.run(_run)


def test_recycle_async_clients_swaps_di_and_bumps_generation():
    async def _run() -> None:
        first = AsyncClient()
        di[AsyncClient] = first
        gen0 = http_pool.pool_generation()

        async def _fake_aclose(_client: httpx.AsyncClient) -> None:
            return None

        with patch.object(http_pool, "_aclose_client", new=_fake_aclose):
            gen1 = await http_pool.recycle_async_clients(reason="test")

        assert gen1 == gen0 + 1
        assert http_pool.pool_generation() == gen1
        assert di[AsyncClient] is not first
        await di[AsyncClient].aclose()

    trio.run(_run)


def test_heal_on_pool_timeout_calls_shed_and_recycles_once():
    async def _run() -> None:
        di[AsyncClient] = AsyncClient()
        shed_calls = {"n": 0}

        async def _shed() -> None:
            shed_calls["n"] += 1

        http_pool.register_stream_shed_callback(_shed)

        async def _fake_aclose(_client: httpx.AsyncClient) -> None:
            return None

        with patch.object(http_pool, "_aclose_client", new=_fake_aclose):
            first = await http_pool.heal_on_pool_timeout(pool_repr="pool")
            second = await http_pool.heal_on_pool_timeout(pool_repr="pool")

        assert first is True
        # Second heal is allowed after first completes (not concurrent).
        assert second is True
        assert shed_calls["n"] == 2
        assert http_pool.pool_generation() >= 2
        await di[AsyncClient].aclose()

    trio.run(_run)


def test_concurrent_heal_only_one_recycles():
    async def _run() -> None:
        di[AsyncClient] = AsyncClient()
        results: list[bool] = []

        async def _fake_aclose(_client: httpx.AsyncClient) -> None:
            await trio.sleep(0.05)

        async def _one() -> None:
            results.append(await http_pool.heal_on_pool_timeout())

        with patch.object(http_pool, "_aclose_client", new=_fake_aclose):
            async with trio.open_nursery() as nursery:
                nursery.start_soon(_one)
                nursery.start_soon(_one)
                nursery.start_soon(_one)

        assert results.count(True) == 1
        assert results.count(False) == 2
        await di[AsyncClient].aclose()

    trio.run(_run)


def test_trio_streaming_http_pool_initialization_and_di_isolation():
    """TrioStreamingHttpPool creates clients under Trio and never touches global DI."""

    async def _run() -> None:
        import sniffio

        assert sniffio.current_async_library() == "trio"

        sentinel_client = AsyncClient()
        di[AsyncClient] = sentinel_client

        pool = http_pool.TrioStreamingHttpPool(proxy_url="http://127.0.0.1:8888")
        try:
            assert pool.generation >= 1
            client = pool.get_client(use_proxy=False)
            proxy_client = pool.get_client(use_proxy=True)

            assert isinstance(client, httpx.AsyncClient)
            assert isinstance(proxy_client, httpx.AsyncClient)
            assert client is not sentinel_client
            assert proxy_client is not sentinel_client

            # DI MUST be unchanged
            assert di[AsyncClient] is sentinel_client
        finally:
            await pool.teardown()
            await sentinel_client.aclose()

    trio.run(_run)


def test_trio_streaming_http_pool_foreground_bypasses_pressure_and_background_waits():
    async def _run() -> None:
        pool = http_pool.TrioStreamingHttpPool()
        pressure = {"active": True}
        pool.register_foreground_pressure_callback(lambda: pressure["active"])
        try:
            async with pool.admit("scan", workload="foreground"):
                pass

            completed = {"value": False}

            async def _background() -> None:
                async with pool.admit("scan", workload="background"):
                    completed["value"] = True

            async with trio.open_nursery() as nursery:
                nursery.start_soon(_background)
                await trio.sleep(0.3)
                assert not completed["value"]
                pressure["active"] = False

            assert completed["value"]
        finally:
            await pool.teardown()

    trio.run(_run)


def test_trio_streaming_http_pool_foreground_pressure_gates_neutral_body_admission():
    """Confirmed foreground runway pressure gates non-foreground neutral body acquisition."""

    async def _run() -> None:
        pool = http_pool.TrioStreamingHttpPool()
        pressure = {"active": True}
        pool.register_foreground_pressure_callback(lambda: pressure["active"])
        try:
            # 1. Foreground body request proceeds immediately despite pressure
            fg_done = False
            async with pool.admit("body", workload="foreground"):
                fg_done = True
            assert fg_done

            # 2. Non-foreground/neutral body acquisition must wait while pressure is active
            neutral_done = {"value": False}

            async def _neutral_body() -> None:
                async with pool.admit("body", workload="neutral"):
                    neutral_done["value"] = True

            async with trio.open_nursery() as nursery:
                nursery.start_soon(_neutral_body)
                await trio.sleep(0.3)
                # MUST be false while pressure is active!
                assert not neutral_done["value"]

                # Clearing pressure allows neutral body acquisition to proceed
                pressure["active"] = False

            assert neutral_done["value"]
        finally:
            await pool.teardown()

    trio.run(_run)


def test_trio_streaming_http_pool_multiple_neutral_waiters_and_cancellation():
    """Multiple neutral requests queue during foreground pressure and clear without lost wakeup or token leak."""

    async def _run() -> None:
        pool = http_pool.TrioStreamingHttpPool()
        pressure = {"active": True}
        pool.register_foreground_pressure_callback(lambda: pressure["active"])
        try:
            waiter_a = {"done": False}
            waiter_b = {"done": False}

            async def _task_a():
                async with pool.admit("body", workload="neutral"):
                    waiter_a["done"] = True

            async def _task_b():
                async with pool.admit("body", workload="neutral"):
                    waiter_b["done"] = True

            async with trio.open_nursery() as nursery:
                nursery.start_soon(_task_a)
                nursery.start_soon(_task_b)
                await trio.sleep(0.3)
                assert not waiter_a["done"]
                assert not waiter_b["done"]

                # Release pressure
                pressure["active"] = False

            assert waiter_a["done"]
            assert waiter_b["done"]

            # Test cancellation during pressure wait: tokens must not leak
            pressure["active"] = True

            async def _cancelled_task():
                async with pool.admit("body", workload="neutral"):
                    pass

            with trio.move_on_after(0.1):
                async with trio.open_nursery() as nursery:
                    nursery.start_soon(_cancelled_task)

            # Pool limiters must be clean and unborrowed
            assert pool._total_limiter.borrowed_tokens == 0
            assert pool._body_limiter.borrowed_tokens == 0
        finally:
            await pool.teardown()

    trio.run(_run)


def test_trio_streaming_http_pool_late_foreground_promotion_breaks_wait():
    """A neutral waiter whose workload promotes to foreground proceeds immediately without waiting for pressure clear."""

    async def _run() -> None:
        pool = http_pool.TrioStreamingHttpPool()
        pressure = {"active": True}
        pool.register_foreground_pressure_callback(lambda: pressure["active"])
        try:
            current_workload = {"val": "neutral"}
            b_done = {"val": False}

            async def _task_b():
                async with pool.admit("body", workload=lambda: current_workload["val"]):
                    b_done["val"] = True

            async with trio.open_nursery() as nursery:
                nursery.start_soon(_task_b)
                await trio.sleep(0.3)
                # B must be waiting because it is currently neutral and pressure is active
                assert not b_done["val"]

                # Promote B to foreground while pressure remains active!
                current_workload["val"] = "foreground"
                # Wait for poll interval (0.25s) to detect promotion
                await trio.sleep(0.35)
                # Pressure is STILL active, but B became foreground so it MUST have proceeded
                assert b_done["val"]
                assert pressure["active"] is True
        finally:
            await pool.teardown()

    trio.run(_run)


def test_trio_streaming_http_pool_two_simultaneous_playback_promotions():
    """Two concurrent playback startups that promote to foreground both proceed under pressure."""

    async def _run() -> None:
        pool = http_pool.TrioStreamingHttpPool()
        pressure = {"active": True}
        pool.register_foreground_pressure_callback(lambda: pressure["active"])
        try:
            wl_1 = {"val": "neutral"}
            wl_2 = {"val": "neutral"}
            done_1 = {"val": False}
            done_2 = {"val": False}

            async def _stream_1():
                async with pool.admit("body", workload=lambda: wl_1["val"]):
                    done_1["val"] = True

            async def _stream_2():
                async with pool.admit("body", workload=lambda: wl_2["val"]):
                    done_2["val"] = True

            async with trio.open_nursery() as nursery:
                nursery.start_soon(_stream_1)
                nursery.start_soon(_stream_2)
                await trio.sleep(0.3)
                assert not done_1["val"]
                assert not done_2["val"]

                # Webhooks arrive for both playbacks
                wl_1["val"] = "foreground"
                wl_2["val"] = "foreground"
                await trio.sleep(0.35)
                assert done_1["val"]
                assert done_2["val"]
                assert pressure["active"] is True
        finally:
            await pool.teardown()

    trio.run(_run)


def test_trio_streaming_http_pool_two_foreground_and_one_unconfirmed_neutral():
    """Two foreground streams proceed while an unconfirmed neutral stream remains yielding under pressure."""

    async def _run() -> None:
        pool = http_pool.TrioStreamingHttpPool()
        pressure = {"active": True}
        pool.register_foreground_pressure_callback(lambda: pressure["active"])
        try:
            wl_fg1 = {"val": "neutral"}
            wl_fg2 = {"val": "neutral"}
            wl_neutral = {"val": "neutral"}
            done_fg1 = {"val": False}
            done_fg2 = {"val": False}
            done_neutral = {"val": False}

            async def _fg1():
                async with pool.admit("body", workload=lambda: wl_fg1["val"]):
                    done_fg1["val"] = True

            async def _fg2():
                async with pool.admit("body", workload=lambda: wl_fg2["val"]):
                    done_fg2["val"] = True

            async def _neutral():
                async with pool.admit("body", workload=lambda: wl_neutral["val"]):
                    done_neutral["val"] = True

            async with trio.open_nursery() as nursery:
                nursery.start_soon(_fg1)
                nursery.start_soon(_fg2)
                nursery.start_soon(_neutral)
                await trio.sleep(0.3)
                assert not done_fg1["val"]
                assert not done_fg2["val"]
                assert not done_neutral["val"]

                # Only two streams receive foreground confirmation
                wl_fg1["val"] = "foreground"
                wl_fg2["val"] = "foreground"
                await trio.sleep(0.35)
                assert done_fg1["val"]
                assert done_fg2["val"]
                # Neutral stream remains yielding because pressure is still active!
                assert not done_neutral["val"]

                # Clear pressure: neutral stream now completes
                pressure["active"] = False
                await trio.sleep(0.35)
                assert done_neutral["val"]
        finally:
            await pool.teardown()

    trio.run(_run)


def test_trio_streaming_http_pool_no_confirmed_foreground_stays_waiting():
    """Streams without foreground confirmation continue waiting as long as pressure is active."""

    async def _run() -> None:
        pool = http_pool.TrioStreamingHttpPool()
        pressure = {"active": True}
        pool.register_foreground_pressure_callback(lambda: pressure["active"])
        try:
            done = {"val": False}

            async def _neutral():
                async with pool.admit("body", workload="neutral"):
                    done["val"] = True

            async with trio.open_nursery() as nursery:
                nursery.start_soon(_neutral)
                await trio.sleep(0.6)
                assert not done["val"]

                pressure["active"] = False
                await trio.sleep(0.35)
                assert done["val"]
        finally:
            await pool.teardown()

    trio.run(_run)


def test_trio_streaming_http_pool_admission_limits():
    """TrioStreamingHttpPool enforces total and body capacity limiters."""

    async def _run() -> None:
        pool = http_pool.TrioStreamingHttpPool()
        try:
            # Under capacity
            async with pool.admit("scan"):
                pass
            async with pool.admit("body"):
                pass

            # Saturate total limiter
            total_tokens = [object() for _ in range(MAX_TOTAL_STREAM_REQUESTS)]
            for t in total_tokens:
                await pool._total_limiter.acquire_on_behalf_of(t)

            try:
                async with pool.admit("scan"):
                    raise AssertionError("should have failed fast with PoolTimeout")
            except httpx.PoolTimeout:
                pass
            finally:
                for t in total_tokens:
                    pool._total_limiter.release_on_behalf_of(t)

            # Saturate body limiter
            body_tokens = [object() for _ in range(MAX_BODY_STREAMS)]
            for t in body_tokens:
                await pool._body_limiter.acquire_on_behalf_of(t)

            try:
                async with pool.admit("body"):
                    raise AssertionError("should have failed fast with PoolTimeout")
            except httpx.PoolTimeout:
                pass
            finally:
                for t in body_tokens:
                    pool._body_limiter.release_on_behalf_of(t)
        finally:
            await pool.teardown()

    trio.run(_run)


def test_trio_streaming_http_pool_lease_and_single_flight_heal():
    """TrioStreamingHttpPool manages leases and single-flight healing without mutating DI."""

    async def _run() -> None:
        sentinel_client = AsyncClient()
        di[AsyncClient] = sentinel_client

        shed_called = {"count": 0}

        async def _shed():
            shed_called["count"] += 1

        pool = http_pool.TrioStreamingHttpPool()
        pool.register_stream_shed_callback(_shed)

        try:
            gen0 = pool.generation
            lease1 = pool.acquire_lease(use_proxy=False)
            assert lease1.generation == gen0
            assert pool.active_leases == 1

            # Concurrent heal attempts
            results: list[bool] = []

            async def _attempt_heal():
                res = await pool.heal_on_pool_timeout()
                results.append(res)

            async with trio.open_nursery() as nursery:
                nursery.start_soon(_attempt_heal)
                nursery.start_soon(_attempt_heal)
                nursery.start_soon(_attempt_heal)

            assert results.count(True) == 1
            assert results.count(False) == 2
            assert shed_called["count"] == 1
            assert pool.generation == gen0 + 1

            # Release lease from older generation
            await pool.release_lease(lease1)
            assert pool.active_leases == 0

            # DI remains unchanged
            assert di[AsyncClient] is sentinel_client
        finally:
            await pool.teardown()
            await sentinel_client.aclose()

    trio.run(_run)


def test_trio_streaming_http_pool_skips_stale_generation_heal():
    """A late timeout from a retired generation cannot recycle the new pool."""

    async def _run() -> None:
        shed_called = {"count": 0}

        async def _shed() -> None:
            shed_called["count"] += 1

        pool = http_pool.TrioStreamingHttpPool()
        pool.register_stream_shed_callback(_shed)
        try:
            assert await pool.heal_on_pool_timeout(failed_generation=1)
            assert pool.generation == 2
            assert not await pool.heal_on_pool_timeout(failed_generation=1)
            assert pool.generation == 2
            assert shed_called["count"] == 1
        finally:
            await pool.teardown()

    trio.run(_run)


def test_trio_streaming_http_pool_drain_and_teardown():
    """TrioStreamingHttpPool handles multiple generations and teardown."""

    async def _run() -> None:
        pool = http_pool.TrioStreamingHttpPool()
        try:
            lease1 = pool.acquire_lease(use_proxy=False)
            assert lease1.generation == 1

            # First heal: generation 1 retired with 1 active lease
            await pool.heal_on_pool_timeout()
            assert pool.generation == 2
            assert 1 in pool._retired_generations

            lease2 = pool.acquire_lease(use_proxy=False)
            assert lease2.generation == 2

            # Second heal: generation 2 retired
            await pool.heal_on_pool_timeout()
            assert pool.generation == 3
            assert 2 in pool._retired_generations

            # Release leases; each final release drains its retired generation.
            await pool.release_lease(lease1)
            await pool.release_lease(lease2)
            assert pool.active_leases == 0
            assert len(pool._retired_generations) == 0
        finally:
            await pool.teardown()

    trio.run(_run)
