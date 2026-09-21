"""Regression tests for safe upstream HTTP headers used by HLS tools."""

from __future__ import annotations

import asyncio
import subprocess
from unittest.mock import AsyncMock, Mock, patch

import pytest
from starlette.requests import Request

from routers.secure import stream


def _request(headers: dict[str, str] | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "headers": [
                (name.lower().encode("ascii"), value.encode("latin-1"))
                for name, value in (headers or {}).items()
            ],
            "path": "/hls/1/segment/0.ts",
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
        }
    )


def test_build_ffmpeg_headers_uses_browser_user_agent_by_default():
    assert stream.build_ffmpeg_headers() == (
        f"User-Agent: {stream.DEFAULT_STREAM_USER_AGENT}\r\n"
    )


def test_build_ffmpeg_headers_filters_and_sanitizes_untrusted_values():
    headers = stream.build_ffmpeg_headers(
        {
            "Accept": "video/*",
            "Referer": "https://cineflow.example/watch\r\nX-Injected: true",
            "Authorization": "Bearer secret",
            "Connection": "keep-alive",
            "X-Api-Key": "service-secret",
            "Bad Header": "discard",
        }
    )

    assert "Accept: video/*\r\n" in headers
    assert "Referer: https://cineflow.example/watchX-Injected: true\r\n" in headers
    assert "Authorization" not in headers
    assert "Connection" not in headers
    assert "X-Api-Key" not in headers
    assert "Bad Header" not in headers
    assert headers.endswith("\r\n")


def test_ffprobe_receives_headers_immediately_before_input_url():
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"42.5\n")

    with patch.object(stream.subprocess, "run", return_value=completed) as run:
        duration = stream._get_video_duration(
            "https://cdn.example.test/video.mkv",
            {"Referer": "https://cineflow.example"},
        )

    assert duration == 42.5
    command = run.call_args.args[0]
    input_index = command.index("-i")
    assert command[input_index - 2] == "-headers"
    assert command[input_index - 1] == stream.build_ffmpeg_headers(
        {"Referer": "https://cineflow.example"}
    )
    assert command[input_index + 1] == "https://cdn.example.test/video.mkv"


@pytest.mark.asyncio
async def test_ffmpeg_receives_headers_immediately_before_input_url():
    process = Mock(
        stdout=Mock(read=AsyncMock(return_value=b"")),
        stderr=Mock(read=AsyncMock(return_value=b"")),
        returncode=0,
        wait=AsyncMock(),
    )

    with (
        patch.object(
            stream,
            "_get_media_info",
            return_value=("https://cdn.example.test/video.mkv", "", "video.mkv"),
        ),
        patch.object(
            stream.asyncio,
            "create_subprocess_exec",
            AsyncMock(return_value=process),
        ) as create_process,
    ):
        await stream.get_hls_segment(
            1,
            0,
            _request({"Referer": "https://cineflow.example"}),
            video_profile=None,
        )

    command = create_process.call_args.args
    input_index = command.index("-i")
    assert command[input_index - 2] == "-headers"
    assert command[input_index - 1] == stream.build_ffmpeg_headers(
        {"referer": "https://cineflow.example"}
    )
    assert command[input_index + 1] == "https://cdn.example.test/video.mkv"
