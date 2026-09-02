"""MediaStream pool-related helpers (resolve client + PoolTimeout heal path)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import trio

from program.services.streaming.media_stream import MediaStream


def _bare_stream(*, use_proxy: bool = False, http_pool: Any | None = None) -> MediaStream:
    stream = MediaStream.__new__(MediaStream)
    stream._use_proxy_client = use_proxy
    stream._http_pool = http_pool
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


def test_resolve_async_client_uses_injected_pool_when_provided():
    mock_pool = MagicMock()
    mock_client = MagicMock()
    mock_pool.get_client.return_value = mock_client

    stream = _bare_stream(use_proxy=False, http_pool=mock_pool)
    with patch("program.services.streaming.media_stream.di") as mock_di:
        resolved = stream._resolve_async_client()
        assert resolved is mock_client
        mock_pool.get_client.assert_called_once_with(use_proxy=False)
        mock_di.__getitem__.assert_not_called()


def test_mount_scoped_stream_requires_injected_http_pool():
    with pytest.raises(RuntimeError, match="mount-scoped Trio HTTP pool"):
        MediaStream(
            fh=1,
            file_size=1024,
            path="/x.mkv",
            original_filename="x.mkv",
            nursery=MagicMock(),
            provider="realdebrid",
            initial_url="https://example.com/file",
            require_mount_http_pool=True,
        )


def test_injected_pool_timeout_heals_injected_pool_only():
    mock_pool = MagicMock()
    mock_client = MagicMock()

    class _BoomStream:
        async def __aenter__(self):
            raise httpx.PoolTimeout("full")

        async def __aexit__(self, *args):
            return False

    mock_client.stream.return_value = _BoomStream()
    mock_pool.get_client.return_value = mock_client
    mock_pool.heal_on_pool_timeout = AsyncMock(return_value=True)
    mock_pool.release_lease = AsyncMock()

    @asynccontextmanager
    async def _admit(_kind: str):
        yield

    mock_pool.admit = _admit
    mock_pool.generation = 7
    lease = MagicMock()
    lease.client = mock_client
    lease.generation = 7
    mock_pool.acquire_lease.return_value = lease

    stream = _bare_stream(http_pool=mock_pool)
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

    async def _run() -> None:
        from program.services.streaming.exceptions import (
            DebridServiceClosedConnectionException,
        )

        with patch("program.services.streaming.media_stream.heal_on_pool_timeout") as global_heal:
            try:
                async with stream.establish_connection(start=0, end=10):
                    pass
                raise AssertionError("expected closed connection")
            except DebridServiceClosedConnectionException:
                pass

            global_heal.assert_not_called()
            mock_pool.heal_on_pool_timeout.assert_awaited_once()
            assert mock_pool.heal_on_pool_timeout.await_args.kwargs[
                "failed_generation"
            ] == 7

    trio.run(_run)


def test_injected_pool_admission_timeout_heals_failed_generation():
    mock_pool = MagicMock()
    mock_pool.generation = 11
    mock_pool.heal_on_pool_timeout = AsyncMock(return_value=True)
    mock_pool.release_lease = AsyncMock()

    @asynccontextmanager
    async def _admit(_kind: str):
        raise httpx.PoolTimeout("admission full")
        yield

    mock_pool.admit = _admit

    stream = _bare_stream(http_pool=mock_pool)
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

    async def _run() -> None:
        from program.services.streaming.exceptions import (
            DebridServiceClosedConnectionException,
        )

        with patch("program.services.streaming.media_stream.heal_on_pool_timeout") as global_heal:
            try:
                async with stream.establish_connection(start=0, end=10):
                    pass
                raise AssertionError("expected closed connection")
            except DebridServiceClosedConnectionException:
                pass

            global_heal.assert_not_called()
            mock_pool.acquire_lease.assert_not_called()
            mock_pool.release_lease.assert_not_called()
            mock_pool.heal_on_pool_timeout.assert_awaited_once()
            assert mock_pool.heal_on_pool_timeout.await_args.kwargs[
                "failed_generation"
            ] == 11

    trio.run(_run)
