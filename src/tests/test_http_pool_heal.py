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
