"""Regression tests for HLS FFmpeg process ownership."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from routers.secure import stream


class FakeStdout:
    def __init__(self, chunks: list[bytes], blocked: asyncio.Event | None = None):
        self.chunks = iter(chunks)
        self.blocked = blocked

    async def read(self, _size: int) -> bytes:
        if self.blocked is not None:
            await self.blocked.wait()
        return next(self.chunks, b"")


class FakeProcess:
    def __init__(self, stdout: FakeStdout, returncode: int | None = 0):
        self.stdout = stdout
        self.stderr = AsyncMock()
        self.returncode = returncode
        self.wait = AsyncMock(side_effect=self._reap)
        self.terminate = self._terminate
        self.kill = self._kill
        self.terminate_called = 0
        self.kill_called = 0
        self.waited = False

    async def _reap(self):
        self.waited = True
        if self.returncode is None:
            self.returncode = 0

    def _terminate(self):
        self.terminate_called += 1
        self.returncode = 0

    def _kill(self):
        self.kill_called += 1
        self.returncode = -9


@pytest.fixture
def media_info():
    with patch.object(stream, "_get_media_info", return_value=("http://media", "", "test.ts")):
        yield


@pytest.mark.asyncio
async def test_hls_normal_completion_reaps_process(media_info):
    process = FakeProcess(FakeStdout([b"segment", b""]))

    with patch.object(stream.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)):
        response = await stream.get_hls_segment(1, 0, video_profile=None)
        chunks = [chunk async for chunk in response.body_iterator]

    assert chunks == [b"segment"]
    process.wait.assert_awaited_once()
    assert process.terminate_called == 0
    assert process.kill_called == 0
    assert process.waited


@pytest.mark.asyncio
async def test_hls_cancellation_terminates_and_reaps_process(media_info):
    blocked = asyncio.Event()
    process = FakeProcess(FakeStdout([], blocked=blocked), returncode=None)

    with patch.object(stream.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)):
        response = await stream.get_hls_segment(1, 0, video_profile=None)
        iterator = response.body_iterator
        task = asyncio.create_task(iterator.__anext__())
        await asyncio.sleep(0)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

    assert process.terminate_called == 1
    process.wait.assert_awaited_once()
    assert process.kill_called == 0
    assert process.waited


@pytest.mark.asyncio
async def test_hls_nonzero_exit_is_reaped(media_info):
    process = FakeProcess(FakeStdout([b""]), returncode=1)
    process.stderr.read.return_value = b"ffmpeg failed"

    with patch.object(stream.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)):
        response = await stream.get_hls_segment(1, 0, video_profile=None)
        chunks = [chunk async for chunk in response.body_iterator]

    assert chunks == []
    process.wait.assert_awaited_once()
    assert process.terminate_called == 0
    assert process.kill_called == 0
    assert process.waited
