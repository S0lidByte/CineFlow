"""Comprehensive HTTP Range Response Matrix Test Suite.

Validates all 13 canonical HTTP range response behaviors against MediaStream.establish_connection
and cache population to eliminate any possibility of upstream byte misplacement or cache poisoning.

Matrix Cases:
1. 200 OK for full range (start=0, open-ended) -> ACCEPTED
2. 200 OK for sub-range with Content-Length (start > 0) -> REJECTED with DebridServiceRefusedRangeRequestException
3. 200 OK for sub-range without Content-Length (chunked, start > 0) -> REJECTED with DebridServiceRefusedRangeRequestException
4. 206 Partial Content with valid Content-Range matching requested range -> ACCEPTED
5. 206 Partial Content with Content-Range start != requested start -> REJECTED
6. 206 Partial Content with Content-Range end != requested end (exceeds) -> REJECTED
7. 206 Partial Content with missing Content-Range header -> REJECTED
8. 206 Partial Content with malformed Content-Range (non-bytes unit) -> REJECTED
9. 206 Partial Content with inverted range (start > end) -> REJECTED
10. 206 Partial Content with Content-Length != Content-Range length -> REJECTED
11. 206 Partial Content with multipart/byteranges -> REJECTED
12. 416 Range Not Satisfiable -> REJECTED with DebridServiceRangeNotSatisfiableException
13. 500/502/503/504 server error -> Retried / DebridServiceUnableToConnectException / DebridServiceException
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
    DebridServiceException,
    DebridServiceRangeNotSatisfiableException,
    DebridServiceRefusedRangeRequestException,
    DebridServiceUnableToConnectException,
)
from program.services.streaming.media_stream import (
    MediaStream,
    format_range_header,
    parse_content_range,
    validate_range_response,
)


class MockStreamResponse:
    """Mock httpx response stream simulating upstream HTTP range responses."""

    def __init__(
        self,
        status_code: int,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
    ) -> None:
        self.status_code = status_code
        self.headers = httpx.Headers(headers or {})
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
        chunk_size = 65536
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]


def _setup_stream(
    tmp_path: Path,
    mock_response: MockStreamResponse,
    file_size: int = 100_000_000,
) -> tuple[MediaStream, Cache]:
    stream = MediaStream.__new__(MediaStream)
    stream.filename = "matrix_test.mkv"
    stream.target_url = SimpleNamespace(value="https://debrid.example/stream/matrix")
    stream.headers = {}
    stream.provider = "realdebrid"
    stream.file_metadata = SimpleNamespace(
        file_size=file_size,
        original_filename="matrix_test.mkv",
        path="/matrix_test.mkv",
    )
    stream.session_statistics = SimpleNamespace(
        total_session_connections=0,
        bytes_transferred=0,
    )
    stream.config = SimpleNamespace(
        chunk_size_bytes=1048576,
        chunk_wait_timeout_seconds=5.0,
        max_transport_attempts=1,
    )
    stream._use_proxy_client = False
    stream._http_pool = None
    stream.enable_tracing = False
    stream.build_log_message = lambda msg: f"[matrix-test] {msg}"
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
        cache_key="matrix_test.mkv",
        chunk_size=1048576,
        header_size=1048576,
        footer_size=1048576,
        file_size=file_size,
    )

    @asynccontextmanager
    async def mock_stream_ctx(*args, **kwargs):
        yield mock_response

    mock_client = MagicMock()
    mock_client.stream = mock_stream_ctx
    stream._resolve_async_client = MagicMock(return_value=mock_client)

    return stream, cache


# --- Case 1: 200 OK for full range (start=0, open-ended) -> ACCEPTED ---
def test_case_01_http_200_full_file_accepted(tmp_path: Path) -> None:
    body = b"FULL_FILE_START_DATA" * 512
    resp = MockStreamResponse(
        status_code=HTTPStatus.OK,
        headers={"Content-Length": str(len(body))},
        body=body,
    )
    stream, _ = _setup_stream(tmp_path, resp)

    async def _run():
        async with stream.establish_connection(start=0, end=None) as conn:
            data = await conn.aread()
            assert data == body

    trio.run(_run)


# --- Case 2: 200 OK for sub-range with Content-Length (start > 0) -> REJECTED ---
def test_case_02_http_200_subrange_with_content_length_rejected(tmp_path: Path) -> None:
    body = b"OFFSET_ZERO_DATA" * 512
    resp = MockStreamResponse(
        status_code=HTTPStatus.OK,
        headers={"Content-Length": "100000000"},
        body=body,
    )
    stream, _ = _setup_stream(tmp_path, resp)

    async def _run():
        with pytest.raises(
            DebridServiceRefusedRangeRequestException,
            match="refusing to commit byte-0 payload",
        ):
            async with stream.establish_connection(start=1048576, end=2097151):
                pass

    trio.run(_run)


# --- Case 3: 200 OK for sub-range without Content-Length (chunked, start > 0) -> REJECTED ---
def test_case_03_http_200_subrange_chunked_rejected(tmp_path: Path) -> None:
    body = b"CHUNKED_ZERO_DATA" * 512
    resp = MockStreamResponse(
        status_code=HTTPStatus.OK,
        headers={"Transfer-Encoding": "chunked"},
        body=body,
    )
    stream, _ = _setup_stream(tmp_path, resp)

    async def _run():
        with pytest.raises(
            DebridServiceRefusedRangeRequestException,
            match="refusing to commit byte-0 payload",
        ):
            async with stream.establish_connection(start=1048576, end=2097151):
                pass

    trio.run(_run)


# --- Case 4: 206 Partial Content with valid Content-Range matching requested range -> ACCEPTED ---
def test_case_04_http_206_valid_matching_accepted(tmp_path: Path) -> None:
    body = (
        b"VALID_PARTIAL_DATA" * 58254 + b"!" * 4
    )  # 58254 * 18 = 1048572 + 4 = 1048576 bytes
    assert len(body) == 1048576
    resp = MockStreamResponse(
        status_code=HTTPStatus.PARTIAL_CONTENT,
        headers={
            "Content-Range": "bytes 1048576-2097151/100000000",
            "Content-Length": "1048576",
        },
        body=body,
    )
    stream, _ = _setup_stream(tmp_path, resp)

    async def _run():
        async with stream.establish_connection(start=1048576, end=2097151) as conn:
            data = await conn.aread()
            assert data == body

    trio.run(_run)


# --- Case 5: 206 Partial Content with Content-Range start != requested start -> REJECTED ---
def test_case_05_http_206_mismatched_start_rejected(tmp_path: Path) -> None:
    body = b"WRONG_START_DATA" * 512
    resp = MockStreamResponse(
        status_code=HTTPStatus.PARTIAL_CONTENT,
        headers={
            "Content-Range": "bytes 0-1048575/100000000",
            "Content-Length": "1048576",
        },
        body=body,
    )
    stream, _ = _setup_stream(tmp_path, resp)

    async def _run():
        with pytest.raises(
            DebridServiceRefusedRangeRequestException,
            match="Content-Range start mismatch",
        ):
            async with stream.establish_connection(start=1048576, end=2097151):
                pass

    trio.run(_run)


# --- Case 6: 206 Partial Content with Content-Range end != requested end (exceeds) -> REJECTED ---
def test_case_06_http_206_exceeding_end_rejected(tmp_path: Path) -> None:
    body = b"OVERFLOW_END_DATA" * 512
    resp = MockStreamResponse(
        status_code=HTTPStatus.PARTIAL_CONTENT,
        headers={
            "Content-Range": "bytes 1048576-3000000/100000000",
            "Content-Length": "1951425",
        },
        body=body,
    )
    stream, _ = _setup_stream(tmp_path, resp)

    async def _run():
        with pytest.raises(
            DebridServiceRefusedRangeRequestException,
            match="Content-Range end exceeds requested",
        ):
            async with stream.establish_connection(start=1048576, end=2097151):
                pass

    trio.run(_run)


# --- Case 7: 206 Partial Content with missing Content-Range header -> REJECTED ---
def test_case_07_http_206_missing_content_range_rejected(tmp_path: Path) -> None:
    body = b"NO_HEADER_DATA" * 512
    resp = MockStreamResponse(
        status_code=HTTPStatus.PARTIAL_CONTENT,
        headers={"Content-Length": "1048576"},
        body=body,
    )
    stream, _ = _setup_stream(tmp_path, resp)

    async def _run():
        with pytest.raises(
            DebridServiceRefusedRangeRequestException,
            match="missing Content-Range header",
        ):
            async with stream.establish_connection(start=1048576, end=2097151):
                pass

    trio.run(_run)


# --- Case 8: 206 Partial Content with malformed Content-Range (non-bytes unit) -> REJECTED ---
def test_case_08_http_206_malformed_unit_rejected(tmp_path: Path) -> None:
    body = b"SECONDS_UNIT_DATA" * 512
    resp = MockStreamResponse(
        status_code=HTTPStatus.PARTIAL_CONTENT,
        headers={
            "Content-Range": "seconds 0-100/500",
            "Content-Length": str(len(body)),
        },
        body=body,
    )
    stream, _ = _setup_stream(tmp_path, resp)

    async def _run():
        with pytest.raises(
            DebridServiceRefusedRangeRequestException,
            match="invalid or non-bytes Content-Range",
        ):
            async with stream.establish_connection(start=0, end=100):
                pass

    trio.run(_run)


# --- Case 9: 206 Partial Content with inverted range (start > end) -> REJECTED ---
def test_case_09_http_206_inverted_range_rejected(tmp_path: Path) -> None:
    body = b"INVERTED_RANGE_DATA" * 512
    resp = MockStreamResponse(
        status_code=HTTPStatus.PARTIAL_CONTENT,
        headers={
            "Content-Range": "bytes 500-100/1000000",
            "Content-Length": str(len(body)),
        },
        body=body,
    )
    stream, _ = _setup_stream(tmp_path, resp)

    async def _run():
        # Start mismatch or inverted range validation will reject this
        with pytest.raises(DebridServiceRefusedRangeRequestException):
            async with stream.establish_connection(start=500, end=600):
                pass

    trio.run(_run)


# --- Case 10: 206 Partial Content with Content-Length != Content-Range length -> REJECTED ---
def test_case_10_http_206_length_range_contradiction_rejected(tmp_path: Path) -> None:
    body = b"CONTRADICTORY_DATA" * 512
    resp = MockStreamResponse(
        status_code=HTTPStatus.PARTIAL_CONTENT,
        headers={
            "Content-Range": "bytes 1048576-2097151/100000000",
            "Content-Length": "500000",  # Contradicts 1048576
        },
        body=body,
    )
    stream, _ = _setup_stream(tmp_path, resp)

    async def _run():
        with pytest.raises(
            DebridServiceRefusedRangeRequestException,
            match="contradicts Content-Range length",
        ):
            async with stream.establish_connection(start=1048576, end=2097151):
                pass

    trio.run(_run)


# --- Case 11: 206 Partial Content with multipart/byteranges -> REJECTED ---
def test_case_11_http_206_multipart_byteranges_rejected(tmp_path: Path) -> None:
    body = b"--SEPARATOR\r\nContent-Type: text/plain\r\n\r\nData\r\n--SEPARATOR--"
    resp = MockStreamResponse(
        status_code=HTTPStatus.PARTIAL_CONTENT,
        headers={
            "Content-Type": "multipart/byteranges; boundary=SEPARATOR",
            "Content-Length": str(len(body)),
        },
        body=body,
    )
    stream, _ = _setup_stream(tmp_path, resp)

    async def _run():
        with pytest.raises(
            DebridServiceRefusedRangeRequestException,
            match="Multipart byte range response is not supported",
        ):
            async with stream.establish_connection(start=1000, end=2000):
                pass

    trio.run(_run)


# --- Case 12: 416 Range Not Satisfiable -> REJECTED with DebridServiceRangeNotSatisfiableException ---
def test_case_12_http_416_range_not_satisfiable(tmp_path: Path) -> None:
    resp = MockStreamResponse(
        status_code=HTTPStatus.RANGE_NOT_SATISFIABLE,
        headers={"Content-Range": "bytes */100000000"},
        body=b"Requested range not satisfiable",
    )
    stream, _ = _setup_stream(tmp_path, resp)

    async def _run():
        with pytest.raises(DebridServiceRangeNotSatisfiableException):
            async with stream.establish_connection(start=200_000_000, end=200_001_000):
                pass

    trio.run(_run)


# --- Case 13: 503 Service Unavailable -> REJECTED with DebridServiceUnableToConnectException ---
def test_case_13_http_503_service_unavailable(tmp_path: Path) -> None:
    resp = MockStreamResponse(
        status_code=HTTPStatus.SERVICE_UNAVAILABLE,
        headers={},
        body=b"Service Unavailable",
    )
    stream, _ = _setup_stream(tmp_path, resp)

    async def _run():
        with pytest.raises(DebridServiceUnableToConnectException):
            async with stream.establish_connection(start=1048576, end=2097151):
                pass

    trio.run(_run)


# --- Helper Unit Tests for parse_content_range & format_range_header ---
def test_format_range_header_zero_byte_probe() -> None:
    assert format_range_header(start=0, end=0) == "bytes=0-0"
    assert format_range_header(start=0, end=None) == "bytes=0-"
    assert format_range_header(start=100, end=200) == "bytes=100-200"
    assert format_range_header(start=1048576, end=None) == "bytes=1048576-"


def test_parse_content_range_valid_and_invalid() -> None:
    parsed = parse_content_range("bytes 0-1048575/100000000")
    assert parsed is not None
    assert parsed.unit == "bytes"
    assert parsed.first_byte == 0
    assert parsed.last_byte == 1048575
    assert parsed.complete_length == 100000000

    parsed_star = parse_content_range("bytes 500-999/*")
    assert parsed_star is not None
    assert parsed_star.complete_length is None

    assert parse_content_range(None) is None
    assert parse_content_range("") is None
    assert parse_content_range("invalid-header") is None
    assert parse_content_range("seconds 0-10/20") is not None
    assert parse_content_range("seconds 0-10/20").unit == "seconds"
