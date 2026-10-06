"""Production read/run integration with a deterministic HTTP response boundary."""

from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import trio
from kink import di
from trio.testing import wait_all_tasks_blocked

from program.services.streaming.cache import Cache, CacheConfig
from program.services.streaming.chunker import ChunkCacheNotifier, Chunker
from program.services.streaming.media_stream import MediaStream


def test_older_demand_survives_latest_read_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        cache = Cache(
            CacheConfig(
                cache_dir=tmp_path / "warm", max_size_bytes=32, metrics_enabled=False
            )
        )
        dependencies = {Cache: cache, ChunkCacheNotifier: ChunkCacheNotifier()}
        monkeypatch.setattr("program.services.streaming.media_stream.di", dependencies)
        monkeypatch.setattr("program.services.streaming.chunker.di", dependencies)
        await cache.put("protected", 0, b"x" * 32, stream_id="owner")
        payload = bytes(range(128))
        allow_response = trio.Event()
        requests: list[int] = []
        results: dict[int, bytes] = {}

        async with trio.open_nursery() as nursery:
            stream = MediaStream(
                fh=42,
                file_size=len(payload),
                path="test.mkv",
                original_filename="test.mkv",
                nursery=nursery,
                provider="test",
                initial_url="https://example.test/media",
            )
            from dataclasses import replace

            stream.config = replace(
                stream.config,
                chunk_size=16,
                prefetch_chunks=0,
                chunk_wait_timeout_seconds=2,
            )
            stream.chunker = Chunker(
                cache_key="test.mkv",
                chunk_size=16,
                header_size=16,
                footer_size=16,
                file_size=len(payload),
            )

            async def body_read(*, chunk_range):
                return "body_read"

            class MockStream(httpx.AsyncByteStream):
                def __init__(self, data: bytes):
                    self._data = data

                async def __aiter__(self):
                    for i in range(0, len(self._data), 16):
                        yield self._data[i : i + 16]

            @asynccontextmanager
            async def response(*, start, end=None):
                requests.append(start)
                await allow_response.wait()
                stream_body = MockStream(payload[start:])
                yield httpx.Response(
                    206,
                    headers={
                        "Content-Range": f"bytes {start}-{len(payload)-1}/{len(payload)}"
                    },
                    stream=stream_body,
                    request=httpx.Request("GET", "https://example.test/media"),
                )

            async def no_fallback(**kwargs):
                pytest.fail(f"unexpected discrete fallback: {kwargs}")

            monkeypatch.setattr(stream, "_detect_read_type", body_read)
            monkeypatch.setattr(stream, "establish_connection", response)
            monkeypatch.setattr(stream, "_fetch_discrete_byte_range", no_fallback)

            async def read(start: int) -> None:
                results[start] = await stream.read(
                    request_start=start, request_end=start + 15, request_size=16
                )

            # Producer is already alive but blocked before it can observe A.
            nursery.start_soon(stream.run, 16)
            await wait_all_tasks_blocked()
            nursery.start_soon(read, 16)
            await wait_all_tasks_blocked()
            nursery.start_soon(read, 32)
            await wait_all_tasks_blocked()
            assert stream.recent_reads.current_read.value.chunk_range.position == 32
            allow_response.set()
            with trio.fail_after(3):
                while len(results) != 2:
                    await wait_all_tasks_blocked()
            assert results == {16: payload[16:32], 32: payload[32:48]}
            assert requests == [16]
            assert stream._delivery_registry._entries == {}
            nursery.cancel_scope.cancel()

    trio.run(exercise)
