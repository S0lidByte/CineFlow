"""Deterministic D83 transport budgets, context ownership and lifecycle fences."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import trio

from program.services.streaming import media_stream as module
from program.services.streaming.exceptions import (
    DebridServiceClosedConnectionException,
    DebridServiceUnableToConnectException,
)
from program.services.streaming.media_stream import MediaStream
from tests.test_consumer_runway import CHUNK, read
from tests.test_durable_link_refresh import (
    MockStreamResponse,
    _make_stream,
    _noop_admit,
)

A = "https://cdn.example.com/url_a"
B = "https://cdn.example.com/url_b"


async def no_delay(attempt, maximum, backoffs):
    return attempt < maximum - 1


@pytest.mark.parametrize(
    "error", [httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError]
)
@pytest.mark.parametrize(
    "outcome", ["success", "same", "failed", "seek", "close", "dead_replacement"]
)
def test_transport_exhaustion_once(monkeypatch, error, outcome):
    stream = _make_stream()
    stream.session_statistics.bytes_transferred = 16
    stream.is_killed = SimpleNamespace(value=False)
    stream.adaptive_prefetch = SimpleNamespace(current_generation=0)
    requests = []
    refreshes = []

    async def refresh(failed_url=None):
        refreshes.append(failed_url)
        if outcome == "seek":
            stream.adaptive_prefetch.current_generation += 1
        if outcome == "close":
            stream.is_killed.value = True
        if outcome not in ("same", "failed"):
            stream.target_url.value = B
        return outcome != "failed"

    @asynccontextmanager
    async def request(method, url, **kwargs):
        requests.append(url)
        if url == A or outcome == "dead_replacement":
            raise error("sanitized transport failure")
        yield MockStreamResponse(200, url)

    client = SimpleNamespace(stream=request)
    monkeypatch.setattr(stream, "_resolve_async_client", lambda: client)
    monkeypatch.setattr(stream, "_refresh_download_url", refresh)
    monkeypatch.setattr(stream, "_retry_with_backoff", no_delay)
    monkeypatch.setattr(module, "admit_stream_request", _noop_admit)
    # Background in telemetry: FUSE-only sequential reads should still qualify if body_read_count > 0 or consumer active
    monkeypatch.setattr(
        "program.services.streaming.telemetry.playback_telemetry_collector.is_media_foreground",
        lambda _: False,
    )
    # Ensure stream has body read and active consumer
    stream.session_statistics.body_read_count = 1
    stream._consumer_end = 1000

    async def exercise():
        if outcome == "success":
            async with stream.establish_connection(start=0):
                pass
        else:
            expected = (
                DebridServiceUnableToConnectException
                if error is httpx.ConnectError
                else DebridServiceClosedConnectionException
            )
            with pytest.raises(expected):
                async with stream.establish_connection(start=0):
                    pytest.fail("unexpected connection")
        assert refreshes == [A]
        assert requests.count(A) == 4
        assert requests.count(B) == (
            1 if outcome == "success" else 4 if outcome == "dead_replacement" else 0
        )
        # A later outer retry cannot consume another durable refresh without new bytes progression.
        assert not await stream._recover_transport_url(
            A, stream.adaptive_prefetch.current_generation
        )
        assert refreshes == [A]

    trio.run(exercise)


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectTimeout,
        httpx.RemoteProtocolError,
        httpx.ReadError,
        httpx.InvalidURL,
    ],
)
def test_exception_after_yield_never_reenters(monkeypatch, error):
    stream = _make_stream()
    client = MagicMock()
    client.stream.side_effect = lambda method, url, **kw: MockStreamResponse(200, url)
    refresh = AsyncMock()
    monkeypatch.setattr(stream, "_resolve_async_client", lambda: client)
    monkeypatch.setattr(stream, "_refresh_download_url", refresh)
    monkeypatch.setattr(module, "admit_stream_request", _noop_admit)

    async def exercise():
        failure = error("consumer failure")
        with pytest.raises(error) as caught:
            async with stream.establish_connection(start=0):
                raise failure
        assert caught.value is failure

    trio.run(exercise)
    assert client.stream.call_count == 1
    refresh.assert_not_awaited()


@pytest.mark.parametrize("scope", ["scan", "background", "initial", "invalid"])
def test_transport_recovery_exclusions(monkeypatch, scope):
    stream = _make_stream()
    stream.session_statistics.bytes_transferred = 0 if scope == "initial" else 16
    client = MagicMock()
    client.stream.side_effect = (
        httpx.InvalidURL("invalid")
        if scope == "invalid"
        else httpx.ConnectTimeout("timeout")
    )
    refresh = AsyncMock()
    monkeypatch.setattr(stream, "_resolve_async_client", lambda: client)
    monkeypatch.setattr(stream, "_refresh_download_url", refresh)
    monkeypatch.setattr(stream, "_retry_with_backoff", no_delay)
    monkeypatch.setattr(module, "admit_stream_request", _noop_admit)
    monkeypatch.setattr(
        "program.services.streaming.telemetry.playback_telemetry_collector.is_media_foreground",
        lambda _: scope != "background",
    )

    async def exercise():
        expected = (
            DebridServiceUnableToConnectException
            if scope == "invalid"
            else DebridServiceClosedConnectionException
        )
        with pytest.raises(expected):
            async with stream.establish_connection(
                start=0, end=15 if scope == "scan" else None
            ):
                pass

    trio.run(exercise)
    refresh.assert_not_awaited()


@pytest.mark.parametrize("fence", ["seek", "close"])
def test_canonical_refresh_does_not_adopt_after_fence(monkeypatch, fence):
    stream = _make_stream()
    stream.is_killed = SimpleNamespace(value=False)
    stream.adaptive_prefetch = SimpleNamespace(current_generation=0)
    calls = []

    async def database_call(function):
        calls.append(function)
        if fence == "seek":
            stream.adaptive_prefetch.current_generation += 1
        else:
            stream.is_killed.value = True
        return SimpleNamespace(url=B)

    async def exercise():
        with (
            patch.object(module.trio.to_thread, "run_sync", database_call),
            patch.object(module, "di"),
        ):
            assert not await stream._refresh_download_url(failed_url=A)
        assert stream.target_url.value == A
        assert len(calls) == 1

    trio.run(exercise)


@pytest.mark.parametrize(
    "error", [httpx.ReadError, httpx.ReadTimeout, httpx.RemoteProtocolError]
)
def test_actual_midstream_partial_chunk_recovery(setup_stream, monkeypatch, error):
    from dataclasses import replace

    from trio.testing import MockClock

    from program.services.streaming.stream_connection import StreamConnection

    async def exercise():
        async with trio.open_nursery() as nursery:
            stream, cache, _, _ = setup_stream(nursery)
            stream.config = replace(stream.config, prefetch_chunks=0)
            requests = []
            refreshed = []
            initial = stream.target_url.value
            start = stream.config.header_size

            @asynccontextmanager
            async def connect(*, position):
                requests.append(position)
                response = httpx.Response(
                    206, request=httpx.Request("GET", stream.target_url.value)
                )

                async def reader():
                    if position == start:
                        yield b"a" * CHUNK
                    if stream.target_url.value == initial:
                        yield b"partial"
                        raise error("broken body")
                    yield b"b" * CHUNK
                    await trio.sleep_forever()

                connection = StreamConnection(
                    response=response,
                    start_position=position,
                    current_read_position=position,
                    reader=reader(),
                )
                stream._active_stream_connection = connection
                try:
                    yield connection
                finally:
                    stream._active_stream_connection = None
                    stream._inflight_chunk = None

            async def refresh(failed_url=None):
                refreshed.append(failed_url)
                stream.target_url.value = B
                return True

            monkeypatch.setattr(
                stream, "_detect_read_type", AsyncMock(return_value="body_read")
            )
            monkeypatch.setattr(stream, "connect", connect)
            monkeypatch.setattr(
                stream,
                "manage_connection",
                MediaStream.manage_connection.__get__(stream),
            )
            monkeypatch.setattr(stream, "_refresh_download_url", refresh)
            monkeypatch.setattr(stream, "_retry_with_backoff", no_delay)
            monkeypatch.setattr(
                "program.services.streaming.telemetry.playback_telemetry_collector.is_media_foreground",
                lambda _: True,
            )
            assert await read(stream, start) == b"a" * (128 * 1024)
            assert await read(stream, start + CHUNK) == b"b" * (128 * 1024)
            assert refreshed == [initial]
            assert requests[0] == start
            assert all(position == start + CHUNK for position in requests[1:])
            assert cache.data[start + CHUNK] == b"b" * CHUNK
            await stream.close()
            assert stream._inflight_chunk is None
            assert not stream._delivery_registry._entries
            nursery.cancel_scope.cancel()

    trio.run(exercise, clock=MockClock(autojump_threshold=0))


@pytest.mark.parametrize(
    "error", [httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError]
)
def test_concurrent_handles_exhaust_transport_single_provider_refresh(
    monkeypatch, error
):
    from program.services.filesystem.vfs import VFSDatabase

    async def exercise():
        streams = [
            _make_stream(original_filename="d83-concurrent.mkv") for _ in range(2)
        ]
        exhausted = [trio.Event(), trio.Event()]
        requests = [[], []]
        leases = set()
        released = []
        db_calls = []
        persisted = A

        def lookup(*, original_filename, force_resolve):
            nonlocal persisted
            db_calls.append(force_resolve)
            if force_resolve:
                persisted = B
            return SimpleNamespace(url=persisted)

        async def database_call(function):
            await trio.lowlevel.checkpoint()
            return function()

        for index, stream in enumerate(streams):
            stream.fh = index + 1
            stream.session_statistics.bytes_transferred = 16

            @asynccontextmanager
            async def request(method, url, index=index, **kwargs):
                requests[index].append(url)
                if url == A:
                    if len(requests[index]) == 4:
                        exhausted[index].set()
                        await exhausted[1 - index].wait()
                    raise error("transport exhausted")
                yield MockStreamResponse(200, url)

            client = SimpleNamespace(stream=request)

            def acquire(*, use_proxy, client=client, index=index):
                lease = SimpleNamespace(client=client, generation=0)
                leases.add(id(lease))
                return lease

            async def release(lease):
                leases.remove(id(lease))
                released.append(lease)

            pool = SimpleNamespace(
                generation=0,
                admit=lambda kind, **kw: _noop_admit(kind),
                acquire_lease=acquire,
                release_lease=release,
            )
            stream._http_pool = pool
            monkeypatch.setattr(stream, "_retry_with_backoff", no_delay)

        async def consume(stream):
            async with stream.establish_connection(start=0):
                await trio.lowlevel.checkpoint()
            assert stream.target_url.value == B
            assert not await stream._recover_transport_url(B, 0)

        monkeypatch.setattr(module.trio.to_thread, "run_sync", database_call)
        monkeypatch.setattr(
            module,
            "di",
            {VFSDatabase: SimpleNamespace(get_entry_by_original_filename=lookup)},
        )
        monkeypatch.setattr(
            "program.services.streaming.telemetry.playback_telemetry_collector.is_media_foreground",
            lambda _: True,
        )
        with trio.fail_after(5):
            async with trio.open_nursery() as nursery:
                for stream in streams:
                    nursery.start_soon(consume, stream)
        assert requests == [[A] * 4 + [B], [A] * 4 + [B]]
        assert db_calls == [False, True, False]
        assert not leases
        assert len(released) == 10

    trio.run(exercise)


@pytest.mark.parametrize("fence", ["seek", "close"])
def test_actual_run_refresh_races_seek_close(setup_stream, monkeypatch, fence):
    from dataclasses import replace

    from trio.testing import wait_all_tasks_blocked

    from program.services.streaming.exceptions import MediaStreamKilledException

    async def exercise():
        with trio.fail_after(10):
            async with trio.open_nursery() as nursery:
                stream, cache, _, _ = setup_stream(nursery)
                stream.config = replace(stream.config, prefetch_chunks=0)
                start = stream.config.header_size
                destination = start + CHUNK * 80
                initial = stream.target_url.value
                refreshing = trio.Event()
                resume = trio.Event()
                requests = []
                refreshes = []
                closed = []
                active = set()
                released = []
                monkeypatch.setattr(cache, "release_stream", released.append)

                class Body(httpx.AsyncByteStream):
                    def __init__(self, position):
                        self.position = position

                    async def __aiter__(self):
                        if self.position == start:
                            yield b"a" * CHUNK
                        if self.position == destination:
                            yield b"z" * CHUNK
                            await trio.sleep_forever()
                        yield b"partial"
                        raise httpx.ReadError("body exhausted")

                    async def aclose(self):
                        closed.append(self.position)

                @asynccontextmanager
                async def request(method, url, headers, **kwargs):
                    position = int(headers["Range"].split("=")[1].split("-")[0])
                    requests.append((url, position))
                    response = httpx.Response(
                        206,
                        request=httpx.Request(method, url),
                        headers={
                            "Content-Range": f"bytes {position}-{stream.file_metadata.file_size - 1}/{stream.file_metadata.file_size}"
                        },
                        stream=Body(position),
                    )
                    try:
                        yield response
                    finally:
                        with trio.CancelScope(shield=True):
                            await response.aclose()

                client = SimpleNamespace(stream=request)

                def acquire(**kwargs):
                    lease = SimpleNamespace(client=client, generation=0)
                    active.add(id(lease))
                    return lease

                async def release(lease):
                    active.remove(id(lease))

                stream._http_pool = SimpleNamespace(
                    generation=0,
                    active_leases=0,
                    admit=lambda kind, **kw: _noop_admit(kind),
                    acquire_lease=acquire,
                    release_lease=release,
                )

                async def refresh(failed_url=None):
                    refreshes.append(failed_url)
                    refreshing.set()
                    await resume.wait()
                    return False

                monkeypatch.setattr(
                    stream,
                    "manage_connection",
                    MediaStream.manage_connection.__get__(stream),
                )
                monkeypatch.setattr(
                    stream, "_detect_read_type", AsyncMock(return_value="body_read")
                )
                monkeypatch.setattr(stream, "_refresh_download_url", refresh)
                monkeypatch.setattr(stream, "_retry_with_backoff", no_delay)
                monkeypatch.setattr(
                    "program.services.streaming.telemetry.playback_telemetry_collector.is_media_foreground",
                    lambda _: True,
                )
                assert await read(stream, start) == b"a" * (128 * 1024)
                old_scope = trio.CancelScope()

                async def old_read():
                    with old_scope:
                        try:
                            await read(stream, start + CHUNK)
                        except MediaStreamKilledException:
                            assert fence == "close"

                nursery.start_soon(old_read)
                await refreshing.wait()
                assert start + CHUNK not in cache.data
                assert not active
                if fence == "close":
                    await stream.close()
                    resume.set()
                else:
                    old_scope.cancel()
                    await wait_all_tasks_blocked()
                    result = []

                    async def seek_read():
                        result.append(await read(stream, destination))

                    nursery.start_soon(seek_read)
                    await wait_all_tasks_blocked()
                    resume.set()
                    await wait_all_tasks_blocked()
                    assert result == [b"z" * (128 * 1024)]
                    assert requests[-1] == (initial, destination)
                    assert stream._stream_error.value is None
                    await stream.close()
                assert refreshes == [initial]
                assert not active
                assert len(closed) == len(requests)
                assert stream._active_stream_connection is None
                assert stream._inflight_chunk is None
                assert not stream._delivery_registry._entries
                assert released == [stream.stream_id]
                assert not stream.is_streaming.value
                nursery.cancel_scope.cancel()

    trio.run(exercise)


def test_transport_recovery_rearm_on_progression(monkeypatch):
    """Verify that after recovering from URL A to URL B, progression on B allows B to recover to C."""
    stream = _make_stream()
    stream.session_statistics.bytes_transferred = 100
    stream.session_statistics.body_read_count = 1
    stream._consumer_end = 500
    stream.is_killed = SimpleNamespace(value=False)
    stream.adaptive_prefetch = SimpleNamespace(current_generation=0)

    C = "https://cdn.example.com/url_c"
    refreshes = []

    async def mock_refresh(failed_url=None):
        refreshes.append(failed_url)
        if failed_url == A:
            stream.target_url.value = B
            return True
        elif failed_url == B:
            stream.target_url.value = C
            return True
        return False

    monkeypatch.setattr(stream, "_refresh_download_url", mock_refresh)
    # Ensure telemetry is not marking foreground (testing FUSE-only logic)
    monkeypatch.setattr(
        "program.services.streaming.telemetry.playback_telemetry_collector.is_media_foreground",
        lambda _: False,
    )

    async def exercise():
        # First failure on URL A -> recovers to B
        gen = stream.adaptive_prefetch.current_generation
        recovered_a = await stream._recover_transport_url(A, gen)
        assert recovered_a is True
        assert stream.target_url.value == B
        assert refreshes == [A]

        # Immediate failure on B with NO byte progression -> locked out
        recovered_b_immediate = await stream._recover_transport_url(B, gen)
        assert recovered_b_immediate is False
        assert refreshes == [A]  # No new refresh attempted

        # Media progression occurs on B (+500 bytes transferred)
        stream.session_statistics.bytes_transferred += 500

        # Now failure on B succeeds and recovers to C
        recovered_b_after_progress = await stream._recover_transport_url(B, gen)
        assert recovered_b_after_progress is True
        assert stream.target_url.value == C
        assert refreshes == [A, B]

        # Repeating failure on B is rejected
        assert not await stream._recover_transport_url(B, gen)
        assert refreshes == [A, B]

    trio.run(exercise)
