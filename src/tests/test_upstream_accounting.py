"""Response-boundary accounting, independent of cache publication and pool leases."""

from contextlib import asynccontextmanager

import httpx
import pytest
import trio

from program.services.streaming.session_statistics import (
    SessionStatistics,
    observe_response,
)


@pytest.mark.parametrize("failure", [False, True])
def test_partial_payload_before_httpx_chunk_buffer_and_close(failure):
    async def run():
        stats = SessionStatistics()

        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"abc"
                if failure:
                    raise httpx.ReadError("partial body")
                yield b"de"

            async def aclose(self):
                pass

        @asynccontextmanager
        async def request():
            response = httpx.Response(206, stream=Body())
            try:
                yield response
            finally:
                await response.aclose()

        async with observe_response(request(), stats, speculative=True) as response:
            assert stats.active_body_responses == stats.peak_body_responses == 1
            if failure:
                with pytest.raises(httpx.ReadError):
                    await anext(response.aiter_raw(chunk_size=8))
            else:
                assert await response.aread() == b"abcde"
            assert stats.upstream_bytes == (3 if failure else 5)
            assert stats.speculative_bytes == stats.upstream_bytes
            await response.aclose()
            assert stats.active_body_responses == 0
        assert stats.active_body_responses == 0
        assert stats.total_session_connections == 1
        assert stats.upstream_transfer_seconds >= 0

    trio.run(run)


def test_cancelled_read_counts_partial_and_total_response_overlap():
    async def run():
        stats = SessionStatistics()
        entered = trio.Event()

        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"abc"
                entered.set()
                await trio.sleep_forever()

        @asynccontextmanager
        async def request():
            response = httpx.Response(206, stream=Body())
            try:
                yield response
            finally:
                await response.aclose()

        async with observe_response(request(), stats):
            async with trio.open_nursery() as nursery:

                async def worker():
                    async with observe_response(
                        request(), stats, speculative=True
                    ) as response:
                        await response.aread()

                nursery.start_soon(worker)
                await entered.wait()
                assert stats.active_body_responses == stats.peak_body_responses == 2
                nursery.cancel_scope.cancel()
            assert stats.active_body_responses == 1
        assert stats.active_body_responses == 0
        assert stats.upstream_bytes == stats.speculative_bytes == 3
        assert stats.total_session_connections == 2

    trio.run(run)
