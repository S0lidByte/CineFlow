"""Deterministic reproduction test for HTTP Range response integrity and cache safety.

Verifies upstream range responses (HTTP 200 chunked, 206 mismatched, 206 missing Content-Range,
and 206 valid) against MediaStream.establish_connection and cache population.
"""

from contextlib import asynccontextmanager
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import trio
from kink import di

from program.services.streaming.cache import Cache, CacheConfig
from program.services.streaming.chunker import Chunker
from program.services.streaming.exceptions import (
    DebridServiceRefusedRangeRequestException,
)
from program.services.streaming.media_stream import MediaStream


class MockStreamResponse:
    """Mock httpx response stream simulating various upstream HTTP range behaviors."""

    def __init__(
        self,
        status_code: int,
        headers: dict[str, str],
        body: bytes,
    ) -> None:
        self.status_code = status_code
        self.headers = httpx.Headers(headers)
        self._body = body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=MagicMock(),
                response=MagicMock(status_code=self.status_code),
            )

    async def aread(self) -> bytes:
        return self._body

    async def aiter_bytes(self):
        # Yield in chunks
        chunk_size = 65536
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]


def _create_test_stream(
    tmp_path: Path, mock_response: MockStreamResponse
) -> tuple[MediaStream, Cache]:
    """Instantiate a minimal MediaStream with configured cache and mock HTTP client."""
    stream = MediaStream.__new__(MediaStream)
    stream.filename = "test_movie.mkv"
    stream.target_url = SimpleNamespace(value="https://debrid.example/stream/12345")
    stream.headers = {}
    stream.provider = "realdebrid"
    stream.file_metadata = SimpleNamespace(
        file_size=100_000_000,
        original_filename="test_movie.mkv",
        path="/test_movie.mkv",
    )
    stream.session_statistics = SimpleNamespace(
        total_session_connections=0,
        bytes_transferred=0,
    )
    stream.config = SimpleNamespace(
        chunk_size_bytes=1048576,  # 1 MiB
        chunk_wait_timeout_seconds=5.0,
        max_transport_attempts=1,
    )
    stream._use_proxy_client = False
    stream._http_pool = None
    stream.enable_tracing = False
    stream.build_log_message = lambda msg: f"[test] {msg}"
    stream._retry_with_backoff = AsyncMock(return_value=False)
    stream._refresh_download_url = AsyncMock(return_value=False)

    cache = Cache(
        CacheConfig(
            cache_dir=tmp_path / "cache",
            max_size_bytes=64 * 1024 * 1024,
        )
    )
    di[Cache] = cache
    stream.cache = cache
    stream.chunker = Chunker(
        cache_key="test_movie.mkv",
        chunk_size=1048576,
        header_size=1048576,
        footer_size=1048576,
        file_size=100_000_000,
    )

    # Mock async client context manager
    @asynccontextmanager
    async def mock_stream_ctx(*args, **kwargs):
        yield mock_response

    mock_client = MagicMock()
    mock_client.stream = mock_stream_ctx
    stream._resolve_async_client = MagicMock(return_value=mock_client)

    return stream, cache


def test_http_200_chunked_on_range_request_corrupts_cache_or_fails(
    tmp_path: Path,
) -> None:
    """Test behavior when requesting chunk 1 (1 MiB - 2 MiB) and upstream returns HTTP 200 chunked.

    Upstream returns HTTP 200 without Content-Length (chunked transfer) containing byte 0..1048575.
    Verify whether establish_connection rejects it or yields byte 0 data,
    and whether _fetch_discrete_byte_range stores byte 0 into chunk 1's position.
    """
    byte_zero_payload = (
        b"HEADER_BYTE_ZERO" * 65536
    )  # Exactly 16 * 65536 = 1,048,576 bytes
    assert len(byte_zero_payload) == 1048576

    mock_resp = MockStreamResponse(
        status_code=HTTPStatus.OK,
        headers={
            "Transfer-Encoding": "chunked",
            # No Content-Length
        },
        body=byte_zero_payload,
    )
    stream, cache = _create_test_stream(tmp_path, mock_resp)

    # Request chunk 1 (offset 1048576, size 1048576)
    target_start = 1048576
    target_size = 1048576

    async def _run():
        outcome = {"rejected": False, "exception": None, "yielded": False}
        try:
            async with stream.establish_connection(
                start=target_start, end=target_start + target_size - 1
            ) as resp:
                outcome["yielded"] = True
                data = await resp.aread()
        except Exception as exc:
            outcome["rejected"] = True
            outcome["exception"] = type(exc).__name__

        # Also test _fetch_discrete_byte_range behavior (which caches)
        cache_outcome = {"cached_byte_zero": False, "rejected": False}
        try:
            cached_data = await stream._fetch_discrete_byte_range(
                start=target_start,
                size=target_size,
                should_cache=True,
            )
            entry = await cache.get(
                "test_movie.mkv", target_start, target_start + target_size - 1
            )
            if entry and entry.startswith(b"HEADER_BYTE_ZERO"):
                cache_outcome["cached_byte_zero"] = True
        except Exception as exc:
            cache_outcome["rejected"] = True
            cache_outcome["exception"] = type(exc).__name__

        return outcome, cache_outcome

    outcome, cache_outcome = trio.run(_run)

    # Remediated behavior:
    # establish_connection rejects HTTP 200 without Content-Length for sub-range requests
    # _fetch_discrete_byte_range refuses to cache byte-0 data at offset 1048576!
    assert (
        outcome["rejected"] is True
    ), "establish_connection must reject HTTP 200 for sub-range"
    assert outcome["exception"] == "DebridServiceRefusedRangeRequestException"
    assert (
        cache_outcome["cached_byte_zero"] is False
    ), "Byte-0 data must NEVER be written to chunk 1 cache location!"


def test_http_206_mismatched_content_range_corrupts_cache(tmp_path: Path) -> None:
    """Test behavior when requesting chunk 1 (1048576..) and upstream returns 206 for chunk 0 (0-1048575)."""
    byte_zero_payload = b"MISMATCHED_ZERO_" * 65536  # 16 * 65536 = 1,048,576 bytes
    assert len(byte_zero_payload) == 1048576
    mock_resp = MockStreamResponse(
        status_code=HTTPStatus.PARTIAL_CONTENT,
        headers={
            "Content-Range": "bytes 0-1048575/100000000",
            "Content-Length": "1048576",
        },
        body=byte_zero_payload,
    )
    stream, cache = _create_test_stream(tmp_path, mock_resp)

    target_start = 1048576
    target_size = 1048576

    async def _run():
        with pytest.raises(DebridServiceRefusedRangeRequestException):
            await stream._fetch_discrete_byte_range(
                start=target_start,
                size=target_size,
                should_cache=True,
            )
        entry = await cache.get(
            "test_movie.mkv", target_start, target_start + target_size - 1
        )
        return entry

    cached_entry = trio.run(_run)
    # The remediated code rejects mismatched Content-Range and refuses to cache!
    assert not cached_entry, "Mismatched Content-Range must NOT be cached!"


def test_http_206_missing_content_range_is_accepted(tmp_path: Path) -> None:
    """Test behavior when upstream returns HTTP 206 with MISSING Content-Range header."""
    payload = b"NO_CONTENT_RANG_" * 65536  # 16 * 65536 = 1,048,576 bytes
    assert len(payload) == 1048576
    mock_resp = MockStreamResponse(
        status_code=HTTPStatus.PARTIAL_CONTENT,
        headers={
            "Content-Length": "1048576",
        },
        body=payload,
    )
    stream, cache = _create_test_stream(tmp_path, mock_resp)

    target_start = 1048576
    target_size = 1048576

    async def _run():
        with pytest.raises(DebridServiceRefusedRangeRequestException):
            await stream._fetch_discrete_byte_range(
                start=target_start,
                size=target_size,
                should_cache=True,
            )
        entry = await cache.get(
            "test_movie.mkv", target_start, target_start + target_size - 1
        )
        return entry

    cached_entry = trio.run(_run)
    # The remediated code rejects missing Content-Range and refuses to cache!
    assert not cached_entry, "Missing Content-Range must NOT be cached!"


def test_http_206_matching_content_range_accepted_cleanly(tmp_path: Path) -> None:
    """Control test: when upstream returns valid 206 with matching Content-Range, data is cached correctly."""
    valid_payload = b"VALID_CHUNK_ONE_" * 65536  # 16 * 65536 = 1,048,576 bytes
    assert len(valid_payload) == 1048576
    mock_resp = MockStreamResponse(
        status_code=HTTPStatus.PARTIAL_CONTENT,
        headers={
            "Content-Range": "bytes 1048576-2097151/100000000",
            "Content-Length": "1048576",
        },
        body=valid_payload,
    )
    stream, cache = _create_test_stream(tmp_path, mock_resp)

    target_start = 1048576
    target_size = 1048576

    async def _run():
        cached_data = await stream._fetch_discrete_byte_range(
            start=target_start,
            size=target_size,
            should_cache=True,
        )
        entry = await cache.get(
            "test_movie.mkv", target_start, target_start + target_size - 1
        )
        return entry

    cached_entry = trio.run(_run)
    assert cached_entry is not None
    assert cached_entry.startswith(b"VALID_CHUNK_ONE_")


def test_range_header_zero_byte_probe_formats_as_open_ended(tmp_path: Path) -> None:
    """Demonstrate BUG-05: start=0, end=0 formats Range header as 'bytes=0-' instead of 'bytes=0-0'."""
    captured_headers: dict[str, str] = {}

    @asynccontextmanager
    async def capturing_stream_ctx(*args, **kwargs):
        captured_headers.update(kwargs.get("headers", {}))
        yield MockStreamResponse(
            206, {"Content-Range": "bytes 0-0/100000000", "Content-Length": "1"}, b"X"
        )

    mock_client = MagicMock()
    mock_client.stream = capturing_stream_ctx

    stream = MediaStream.__new__(MediaStream)
    stream.filename = "test_movie.mkv"
    stream.target_url = SimpleNamespace(value="https://debrid.example/stream/12345")
    stream.headers = {}
    stream.provider = "realdebrid"
    stream.file_metadata = SimpleNamespace(
        file_size=100_000_000, original_filename="test_movie.mkv", path="/test"
    )
    stream.session_statistics = SimpleNamespace(
        total_session_connections=0, bytes_transferred=0
    )
    stream.config = SimpleNamespace(
        chunk_size_bytes=1048576,
        chunk_wait_timeout_seconds=5.0,
        max_transport_attempts=1,
    )
    stream._use_proxy_client = False
    stream._http_pool = None
    stream.enable_tracing = False
    stream.build_log_message = lambda msg: f"[test] {msg}"
    stream._retry_with_backoff = AsyncMock(return_value=False)
    stream._refresh_download_url = AsyncMock(return_value=False)
    stream._resolve_async_client = MagicMock(return_value=mock_client)

    async def _run():
        async with stream.establish_connection(start=0, end=0):
            pass

    trio.run(_run)

    # Remediated behavior: start=0, end=0 formats Range header as 'bytes=0-0'
    range_val = captured_headers.get("Range") or captured_headers.get("range")
    assert range_val == "bytes=0-0", f"Expected bytes=0-0, got {range_val}"
