"""MediaStream pool-related helpers (resolve client + PoolTimeout heal path)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import trio

from program.services.streaming.media_stream import MediaStream


def _bare_stream(*, use_proxy: bool = False) -> MediaStream:
    stream = MediaStream.__new__(MediaStream)
    stream._use_proxy_client = use_proxy
    stream.provider = "realdebrid"
    stream.fh = 1
    stream.file_metadata = SimpleNamespace(path="/x.mkv", original_filename="x.mkv")
    stream.session_statistics = SimpleNamespace(
        bytes_transferred=0,
        total_session_connections=0,
    )
    stream._active_stream_connection = None
    stream.enable_tracing = False
    stream.build_log_message = lambda msg: msg  # type: ignore[method-assign]
    return stream


def test_resolve_async_client_uses_di_async_client():
    stream = _bare_stream(use_proxy=False)
    client = MagicMock()
    with patch("program.services.streaming.media_stream.di") as mock_di:
        mock_di.__getitem__.return_value = client
        assert stream._resolve_async_client() is client
        mock_di.__getitem__.assert_called()


def test_force_aclose_active_response_closes_httpx_response():
    stream = _bare_stream()
    response = MagicMock()
    response.aclose = AsyncMock()
    connection = SimpleNamespace(response=response)
    stream._active_stream_connection = connection

    async def _run() -> None:
        await stream._force_aclose_active_response()

    trio.run(_run)

    response.aclose.assert_awaited_once()
    assert stream._active_stream_connection is None


def test_pool_timeout_triggers_heal_once_then_raises():
    stream = _bare_stream()
    stream.target_url = SimpleNamespace(value="https://example.com/file")
    stream.file_metadata = SimpleNamespace(
        path="/x.mkv",
        original_filename="x.mkv",
        file_size=1000,
    )
    stream.session_statistics = SimpleNamespace(
        bytes_transferred=0,
        total_session_connections=0,
    )

    client = MagicMock()

    class _BoomStream:
        async def __aenter__(self):
            raise httpx.PoolTimeout("full")

        async def __aexit__(self, *args):
            return False

    client.stream.return_value = _BoomStream()
    heal = AsyncMock(return_value=True)

    @asynccontextmanager
    async def _admit(_kind: str):
        yield

    async def _run() -> None:
        with (
            patch.object(stream, "_resolve_async_client", return_value=client),
            patch(
                "program.services.streaming.media_stream.admit_stream_request",
                new=_admit,
            ),
            patch(
                "program.services.streaming.media_stream.heal_on_pool_timeout",
                new=heal,
            ),
        ):
            from program.services.streaming.exceptions import (
                DebridServiceClosedConnectionException,
            )

            try:
                async with stream.establish_connection(start=0, end=10):
                    pass
                raise AssertionError("expected closed connection")
            except DebridServiceClosedConnectionException:
                pass

        assert heal.await_count == 1

    trio.run(_run)
