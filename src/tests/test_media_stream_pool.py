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


def _bare_stream(
    *, use_proxy: bool = False, http_pool: Any | None = None
) -> MediaStream:
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


@pytest.mark.parametrize("use_proxy", [False, True])
@pytest.mark.parametrize("first_timeout", [None, 15])
def test_stream_connect_timeout_overrides_old_pool_without_mutating_other_handles(
    use_proxy, first_timeout
):
    """A reused client must not pin new handles to its mount-time timeout."""
    observed = []

    async def respond(request):
        observed.append(request.extensions["timeout"])
        return httpx.Response(
            206, headers={"Content-Range": "bytes 0-10/1000"}, content=b"x" * 11
        )

    @asynccontextmanager
    async def admit(kind, *, workload):
        yield

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), timeout=5
        ) as client:
            pool = MagicMock()
            pool.admit = admit
            pool.generation = 7
            pool.acquire_lease.return_value = SimpleNamespace(
                client=client, generation=7
            )
            pool.release_lease = AsyncMock()
            handles = []
            for timeout in (first_timeout, 60):
                stream = _bare_stream(use_proxy=use_proxy, http_pool=pool)
                stream.target_url = SimpleNamespace(value="https://example.test/file")
                stream.file_metadata.file_size = 1000
                if timeout is not None:
                    stream.config = SimpleNamespace(connect_timeout_seconds=timeout)
                handles.append(stream)

            for stream in (handles[0], handles[1], handles[0]):
                async with stream.establish_connection(start=0, end=10) as response:
                    assert await response.aread() == b"x" * 11

            assert [item["connect"] for item in observed] == [
                first_timeout or 5,
                60,
                first_timeout or 5,
            ]
            assert all(item["read"] == 30 for item in observed)
            assert all(item["write"] == 10 for item in observed)
            assert all(item["pool"] == 5 for item in observed)
            assert client.timeout.connect == 5
            assert pool.release_lease.await_count == 3
            assert all(
                call.kwargs["use_proxy"] == use_proxy
                for call in pool.acquire_lease.call_args_list
            )

    trio.run(run)


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

        with patch(
            "program.services.streaming.media_stream.heal_on_pool_timeout"
        ) as global_heal:
            try:
                async with stream.establish_connection(start=0, end=10):
                    pass
                raise AssertionError("expected closed connection")
            except DebridServiceClosedConnectionException:
                pass

            global_heal.assert_not_called()
            mock_pool.heal_on_pool_timeout.assert_awaited_once()
            assert (
                mock_pool.heal_on_pool_timeout.await_args.kwargs["failed_generation"]
                == 7
            )

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

        with patch(
            "program.services.streaming.media_stream.heal_on_pool_timeout"
        ) as global_heal:
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
            assert (
                mock_pool.heal_on_pool_timeout.await_args.kwargs["failed_generation"]
                == 11
            )

    trio.run(_run)


def test_trio_streaming_http_pool_teardown_shielded_under_cancellation():
    """Teardown clears _clients and closes httpx AsyncClients even when called inside a cancelled scope."""
    from program.services.streaming.http_pool import TrioStreamingHttpPool

    async def _run() -> None:
        pool = TrioStreamingHttpPool()
        assert len(pool._clients) > 0
        clients = list(pool._clients.values())
        for c in clients:
            assert not c.is_closed

        # Enter a cancelled cancel scope to simulate nursery unmount cancellation
        with trio.CancelScope() as scope:
            scope.cancel()
            assert scope.cancel_called
            await pool.teardown()

        assert pool._closed is True
        assert pool.active_leases == 0
        assert not pool._retired_generations
        assert not pool._clients
        for c in clients:
            assert c.is_closed

    trio.run(_run)


def test_sequential_non_plex_body_stream_promotes_to_foreground():
    """Established sequential non-Plex playback promotes body requests to foreground workload."""
    from types import SimpleNamespace

    from program.services.streaming.config import Config
    from program.services.streaming.media_stream import MediaStream, _iter_raw_chunks
    from program.services.streaming.telemetry import playback_telemetry_collector

    seen_workloads: list[str] = []
    playback_telemetry_collector._plex_playbacks.clear()

    file_size = 2_000_000

    def fake_stream(*, method: str, url: str, headers: Any = None, **_kwargs: Any):
        rng = (headers or {}).get("Range", "bytes=0-")
        spec = rng.removeprefix("bytes=")
        s_str, e_str = spec.split("-", 1)
        s = int(s_str)
        e = int(e_str) if e_str else file_size - 1
        resp = MagicMock()
        resp.is_closed = False
        if s == 0 and not e_str:
            resp.status_code = 200
            resp.headers = {"Content-Length": str(file_size)}
        else:
            resp.status_code = 206
            resp.headers = {
                "Content-Range": f"bytes {s}-{e}/{file_size}",
                "Content-Length": str(e - s + 1),
            }
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=None)
        return cm

    fake_client = MagicMock()
    fake_client.stream = fake_stream

    @asynccontextmanager
    async def fake_admit(kind: str, workload: Any = "neutral"):
        w = workload() if callable(workload) else workload
        seen_workloads.append(f"{kind}:{w}")
        yield

    mock_pool = MagicMock()
    mock_pool.generation = 1
    mock_pool.admit = fake_admit
    mock_pool.acquire_lease = MagicMock(
        return_value=SimpleNamespace(generation=1, client=fake_client)
    )
    mock_pool.release_lease = AsyncMock()
    mock_pool.heal_on_pool_timeout = AsyncMock(return_value=True)

    stream = _bare_stream(http_pool=mock_pool)
    stream.config = Config(
        chunk_size=1048576,
        activity_timeout_seconds=10,
        chunk_wait_timeout_seconds=5,
        connect_timeout_seconds=5,
    )
    stream.file_metadata = SimpleNamespace(
        path="/movies/non_plex_movie.mkv",
        original_filename="non_plex_movie.mkv",
        file_size=file_size,
    )
    stream.target_url = SimpleNamespace(value="https://cdn.example.test/non_plex.mkv")
    stream.stream_id = None
    stream._consumer_end = 1024
    stream._set_recovery_state = MagicMock()
    stream._clear_recovery_state = MagicMock()
    stream.session_statistics = SimpleNamespace(
        bytes_transferred=0,
        body_read_count=0,
        total_session_connections=0,
        publish=lambda: None,
    )

    async def _run() -> None:
        # Unestablished stream (0 transferred) remains neutral
        async with stream.establish_connection(start=0, end=None):
            pass
        # Established non-Plex sequential playback promotes to foreground
        stream.session_statistics.bytes_transferred = 1024 * 1024
        stream.session_statistics.body_read_count = 1
        async with stream.establish_connection(start=1048576, end=None):
            pass

        # Discrete scan remains neutral even when body playback is active
        async with stream.establish_connection(start=0, end=65535):
            pass

    trio.run(_run)
    assert seen_workloads == ["body:neutral", "body:foreground", "scan:neutral"]

    # Also verify _iter_raw_chunks forwards 256 KiB chunk_size when accepted
    seen_chunk_sizes: list[int | None] = []

    class RealSigResponse:
        async def aiter_raw(self, chunk_size: int | None = None):
            seen_chunk_sizes.append(chunk_size)
            yield b"ok"

    async def _collect() -> bytes:
        out = bytearray()
        async for part in _iter_raw_chunks(RealSigResponse()):
            out.extend(part)
        return bytes(out)

    assert trio.run(_collect) == b"ok"
    assert seen_chunk_sizes == [262_144]
