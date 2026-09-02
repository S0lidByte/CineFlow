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
