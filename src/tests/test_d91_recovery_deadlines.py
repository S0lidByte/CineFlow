"""Deterministic unit and scenario test suite for D91 recovery deadlines and timeouts."""

from contextlib import asynccontextmanager
from dataclasses import replace
from time import monotonic
from unittest.mock import AsyncMock

import httpx
import pytest
import trio
from trio.testing import MockClock

from program.services.streaming.config import Config
from program.services.streaming.exceptions import ChunksTooSlowException
from program.services.streaming.http_pool import TrioStreamingHttpPool
from program.services.streaming.media_stream import (
    MediaStream,
    StreamRecoveryPhase,
    StreamRecoveryState,
)
from program.utils.async_client import AsyncClient
from program.utils.proxy_client import ProxyClient
from program.utils.stream_http import stream_http_timeout
from tests.test_consumer_runway import CHUNK, read
from tests.test_consumer_runway import setup_stream as stream_fixture
from tests.test_d83_transport_recovery import no_delay

setup_stream = stream_fixture


def test_d91_stream_http_timeout_custom_and_defaults():
    """Verify stream_http_timeout respects custom connect_timeout while preserving other bounds."""
    default_timeout = stream_http_timeout()
    assert default_timeout.connect == 5.0
    assert default_timeout.read == 30.0
    assert default_timeout.write == 10.0
    assert default_timeout.pool == 5.0

    custom_timeout = stream_http_timeout(connect_timeout=15.0)
    assert custom_timeout.connect == 15.0
    assert custom_timeout.read == 30.0
    assert custom_timeout.write == 10.0
    assert custom_timeout.pool == 5.0

    zero_timeout = stream_http_timeout(connect_timeout=0.0)
    assert zero_timeout.connect == 0.0
    assert zero_timeout.read == 30.0


def test_d91_http_pool_and_clients_forward_connect_timeout(monkeypatch):
    """Verify HTTP pool, AsyncClient, and ProxyClient wire connect_timeout_seconds."""
    from program.settings import settings_manager

    monkeypatch.setattr(settings_manager.settings.stream, "connect_timeout_seconds", 12)

    pool = TrioStreamingHttpPool(proxy_url=None)
    assert pool._connect_timeout == 12.0
    pool_client = pool.get_client(use_proxy=False)
    assert pool_client.timeout.connect == 12.0
    assert pool_client.timeout.read == 30.0
    assert pool_client.timeout.write == 10.0
    assert pool_client.timeout.pool == 5.0

    pool_override = TrioStreamingHttpPool(proxy_url=None, connect_timeout=8.0)
    assert pool_override._connect_timeout == 8.0
    override_client = pool_override.get_client(use_proxy=False)
    assert override_client.timeout.connect == 8.0

    async_client = AsyncClient()
    assert async_client.timeout.connect == 12.0
    assert async_client.timeout.read == 30.0

    proxy_client = ProxyClient(proxy_url="http://127.0.0.1:8080")
    assert proxy_client.timeout.connect == 12.0
    assert proxy_client.timeout.read == 30.0


def test_d91_producer_owned_deadline_lifecycle(setup_stream):
    """Verify producer sets and preserves monotonic recovery deadline across attempts."""

    async def exercise():
        async with trio.open_nursery() as nursery:
            stream, _, _, _ = setup_stream(nursery)
            now = trio.current_time()
            generation = stream.adaptive_prefetch.current_generation

            # Phase 1: Set recovery state with explicit timeout_bound
            stream._set_recovery_state(
                StreamRecoveryPhase.TRANSPORT_RETRY,
                generation=generation,
                chunk_start=0,
                attempt=1,
                timeout_bound=25.0,
            )
            state = stream._recovery_state
            assert state.phase == StreamRecoveryPhase.TRANSPORT_RETRY
            assert state.timeout_bound == 25.0
            assert state.recovery_deadline == pytest.approx(now + 25.0)
            assert state.recovery_episode == 1

            # Phase 2: Successive attempt preserves episode counter
            stream._set_recovery_state(
                StreamRecoveryPhase.TRANSPORT_RETRY,
                generation=generation,
                chunk_start=0,
                attempt=2,
                timeout_bound=15.0,
            )
            state2 = stream._recovery_state
            assert state2.attempt == 2
            assert state2.recovery_episode == 1
            assert state2.recovery_deadline == pytest.approx(now + 15.0)

            # Phase 3: Transition to IDLE clears episode and deadline
            stream._set_recovery_state(StreamRecoveryPhase.IDLE)
            state_idle = stream._recovery_state
            assert state_idle.phase == StreamRecoveryPhase.IDLE
            assert state_idle.recovery_episode == 0
            assert state_idle.recovery_deadline is None

            nursery.cancel_scope.cancel()

    trio.run(exercise, clock=MockClock(autojump_threshold=0))


def test_d91_consumer_waiter_follows_producer_deadline_not_static_40s(
    setup_stream, monkeypatch
):
    """Consumer waiter calculates dynamic remaining grace from producer-owned deadline."""

    async def exercise():
        async with trio.open_nursery() as nursery:
            stream, _, _, _ = setup_stream(nursery)
            stream.config = replace(stream.config, chunk_wait_timeout_seconds=5)
            monkeypatch.setattr(stream, "_is_foreground_eligible", lambda: True)
            chunk_range = stream.chunker.get_chunk_range(
                position=stream.config.header_size
            )
            generation = stream.adaptive_prefetch.current_generation

            # Producer sets dynamic deadline of 75.0s (far exceeding old static 40s grace)
            stream._set_recovery_state(
                StreamRecoveryPhase.TRANSPORT_RETRY,
                generation=generation,
                chunk_start=chunk_range.first_chunk.start,
                timeout_bound=75.0,
            )

            # Simulate recovery producing chunks after 45s (which would have failed under 40s static grace)
            async def producer():
                await trio.sleep(45.0)
                for chunk in chunk_range.chunks:
                    await stream._cache_chunk(start=chunk.start, data=b"z" * chunk.size)
                    chunk.emit_cache_signal()
                stream._set_recovery_state(StreamRecoveryPhase.IDLE)

            nursery.start_soon(producer)
            started = trio.current_time()
            await stream._wait_until_chunks_ready(chunk_range=chunk_range)
            # Should successfully complete without ChunksTooSlowException at 45.0s
            assert trio.current_time() - started == pytest.approx(45.0)

            nursery.cancel_scope.cancel()

    trio.run(exercise, clock=MockClock(autojump_threshold=0))


def test_d91_consumer_expires_when_producer_deadline_exhausted(
    setup_stream, monkeypatch
):
    """Consumer raises ChunksTooSlowException when producer-owned dynamic deadline expires."""

    async def exercise():
        async with trio.open_nursery() as nursery:
            stream, _, _, _ = setup_stream(nursery)
            stream.config = replace(stream.config, chunk_wait_timeout_seconds=5)
            monkeypatch.setattr(stream, "_is_foreground_eligible", lambda: True)
            chunk_range = stream.chunker.get_chunk_range(
                position=stream.config.header_size
            )
            generation = stream.adaptive_prefetch.current_generation

            # Producer bounds recovery at 18.0s
            stream._set_recovery_state(
                StreamRecoveryPhase.TRANSPORT_RETRY,
                generation=generation,
                chunk_start=chunk_range.first_chunk.start,
                timeout_bound=18.0,
            )

            started = trio.current_time()
            with pytest.raises(ChunksTooSlowException):
                # Dynamic grace wait runs until recovery deadline
                await stream._wait_until_chunks_ready(chunk_range=chunk_range)

            # Total duration: nominal timeout (5s) + dynamic grace (remaining at t=5s is 18 - 5 = 13s) -> finishes at 18s total!
            assert trio.current_time() - started == pytest.approx(18.0)
            nursery.cancel_scope.cancel()

    trio.run(exercise, clock=MockClock(autojump_threshold=0))


def test_d91_no_deadline_extension_loop_same_episode(setup_stream):
    """Verify state transitions in same recovery episode cannot extend deadline indefinitely."""

    async def exercise():
        async with trio.open_nursery() as nursery:
            stream, _, _, _ = setup_stream(nursery)
            generation = stream.adaptive_prefetch.current_generation

            # Start recovery episode 1 with 20.0s bound at t=0
            stream._set_recovery_state(
                StreamRecoveryPhase.TRANSPORT_RETRY,
                generation=generation,
                timeout_bound=20.0,
            )
            assert stream._recovery_episode == 1
            original_deadline = stream._recovery_state.recovery_deadline
            assert original_deadline == pytest.approx(20.0)

            # Transition from TRANSPORT_RETRY -> URL_REFRESH preserves episode 1
            # Explicitly preserving original episode's deadline
            stream._set_recovery_state(
                StreamRecoveryPhase.URL_REFRESH,
                generation=generation,
                recovery_deadline=original_deadline,
            )
            assert stream._recovery_episode == 1
            assert stream._recovery_state.recovery_deadline == original_deadline

            # Idle resets episode and deadline
            stream._set_recovery_state(StreamRecoveryPhase.IDLE)
            assert stream._recovery_episode == 0
            assert stream._recovery_state.recovery_deadline is None

            nursery.cancel_scope.cancel()

    trio.run(exercise, clock=MockClock(autojump_threshold=0))


def test_d91_complete_d89_shaped_recovery(setup_stream, monkeypatch):
    """
    Phase 7: Complete D89-shaped recovery test.
    Models established foreground body stream, missing demanded chunk,
    retries passing nominal wait, URL refresh yielding replacement URL,
    and missing chunk delivered.
    """

    async def exercise():
        async with trio.open_nursery() as nursery:
            stream, _, _, _ = setup_stream(nursery)
            stream.config = replace(stream.config, chunk_wait_timeout_seconds=5)
            monkeypatch.setattr(stream, "_is_foreground_eligible", lambda: True)

            chunk_range = stream.chunker.get_chunk_range(
                position=stream.config.header_size
            )
            target_chunk = chunk_range.first_chunk
            generation = stream.adaptive_prefetch.current_generation

            # Set established session stats
            stream.session_statistics.bytes_transferred = 50 * 1024 * 1024
            stream.session_statistics.body_read_count = 10

            original_url = stream.target_url.value
            replacement_url = original_url + "/refreshed_d89"

            # Background producer simulates retry -> refresh -> reconnect -> chunk publication
            async def producer():
                # t=0..4: TRANSPORT_RETRY
                stream._set_recovery_state(
                    StreamRecoveryPhase.TRANSPORT_RETRY,
                    generation=generation,
                    chunk_start=target_chunk.start,
                    failed_url=original_url,
                    attempt=1,
                    timeout_bound=35.0,
                )
                await trio.sleep(4.0)

                # t=4..12: URL_REFRESH passes nominal 5s chunk wait!
                episode_deadline = stream._recovery_state.recovery_deadline
                stream._set_recovery_state(
                    StreamRecoveryPhase.URL_REFRESH,
                    generation=generation,
                    chunk_start=target_chunk.start,
                    failed_url=original_url,
                    attempt=1,
                    recovery_deadline=episode_deadline,
                )
                await trio.sleep(10.0)

                # Replacement URL adopted
                stream.target_url.value = replacement_url

                # Publish exact demanded chunk at t=14.0s (past nominal 5s wait)
                await stream._cache_chunk(
                    start=target_chunk.start, data=b"x" * target_chunk.size
                )
                target_chunk.emit_cache_signal()
                stream._set_recovery_state(StreamRecoveryPhase.IDLE)

            nursery.start_soon(producer)
            started = trio.current_time()
            await stream._wait_until_chunks_ready(chunk_range=chunk_range)

            # Consumer survived past nominal wait and received chunk
            assert trio.current_time() - started == pytest.approx(14.0)
            assert stream._recovery_state.phase == StreamRecoveryPhase.IDLE
            assert stream.target_url.value == replacement_url
            nursery.cancel_scope.cancel()

    trio.run(exercise, clock=MockClock(autojump_threshold=0))


def test_d91_complete_bounded_failure_exhaustion(setup_stream, monkeypatch):
    """
    Phase 8: Complete bounded-failure test.
    Simulates producer exhaustion across retries and refresh.
    Consumer wakes immediately on deadline exhaustion and raises ChunksTooSlowException.
    """

    async def exercise():
        async with trio.open_nursery() as nursery:
            stream, _, _, _ = setup_stream(nursery)
            stream.config = replace(stream.config, chunk_wait_timeout_seconds=5)
            monkeypatch.setattr(stream, "_is_foreground_eligible", lambda: True)

            chunk_range = stream.chunker.get_chunk_range(
                position=stream.config.header_size
            )
            target_chunk = chunk_range.first_chunk
            generation = stream.adaptive_prefetch.current_generation

            # Bound entire recovery episode at 15.0s
            stream._set_recovery_state(
                StreamRecoveryPhase.TRANSPORT_RETRY,
                generation=generation,
                chunk_start=target_chunk.start,
                timeout_bound=15.0,
            )

            started = trio.current_time()
            with pytest.raises(ChunksTooSlowException):
                await stream._wait_until_chunks_ready(chunk_range=chunk_range)

            # Nominal (5s) + remaining grace (10s) = exactly 15.0s
            assert trio.current_time() - started == pytest.approx(15.0)
            nursery.cancel_scope.cancel()

    trio.run(exercise, clock=MockClock(autojump_threshold=0))
