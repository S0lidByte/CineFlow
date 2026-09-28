"""Unit tests for RFC 9110 byte range parsing and HTTP 416 handling in stream router."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from routers.secure.stream import (
    _build_forward_headers,
    _normalize_range_header,
    _probe_file_size,
    stream_file,
)


def _make_request(headers: dict[str, str] | None = None) -> Request:
    raw_headers = []
    for k, v in (headers or {}).items():
        raw_headers.append((k.lower().encode("latin-1"), v.encode("latin-1")))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "headers": raw_headers,
        }
    )


class TestRFC9110RangeParsing:
    """Validate all 16 required RFC 9110 range parsing and edge cases."""

    FILE_SIZE = 10_000_000  # 10 MB

    def test_case_1_bytes_0_0(self):
        """1. bytes=0-0: First single byte."""
        norm, bounds, unsat = _normalize_range_header("bytes=0-0", self.FILE_SIZE)
        assert unsat is False
        assert bounds == (0, 0)
        assert norm == "bytes=0-0"

    def test_case_2_bytes_0_1(self):
        """2. bytes=0-1: First two bytes."""
        norm, bounds, unsat = _normalize_range_header("bytes=0-1", self.FILE_SIZE)
        assert unsat is False
        assert bounds == (0, 1)
        assert norm == "bytes=0-1"

    def test_case_3_bytes_0_32767(self):
        """3. bytes=0-32767: Prefix 32 KB chunk."""
        norm, bounds, unsat = _normalize_range_header("bytes=0-32767", self.FILE_SIZE)
        assert unsat is False
        assert bounds == (0, 32767)
        assert norm == "bytes=0-32767"

    def test_case_4_bytes_0_open_ended(self):
        """4. bytes=0-: Entire file from byte 0 to EOF."""
        norm, bounds, unsat = _normalize_range_header("bytes=0-", self.FILE_SIZE)
        assert unsat is False
        assert bounds == (0, self.FILE_SIZE - 1)
        assert norm == f"bytes=0-{self.FILE_SIZE - 1}"

    def test_case_5_bytes_interior(self):
        """5. bytes=1000000-2000000: Interior range."""
        norm, bounds, unsat = _normalize_range_header(
            "bytes=1000000-2000000", self.FILE_SIZE
        )
        assert unsat is False
        assert bounds == (1000000, 2000000)
        assert norm == "bytes=1000000-2000000"

    def test_case_6_suffix_single_byte(self):
        """6. bytes=-1: Suffix range for the last byte (RFC 9110: start = max(0, L-N), end = L-1)."""
        norm, bounds, unsat = _normalize_range_header("bytes=-1", self.FILE_SIZE)
        assert unsat is False
        assert bounds == (self.FILE_SIZE - 1, self.FILE_SIZE - 1)
        assert norm == f"bytes={self.FILE_SIZE - 1}-{self.FILE_SIZE - 1}"

    def test_case_7_suffix_1024_bytes(self):
        """7. bytes=-1024: Final 1024 bytes."""
        norm, bounds, unsat = _normalize_range_header("bytes=-1024", self.FILE_SIZE)
        assert unsat is False
        assert bounds == (self.FILE_SIZE - 1024, self.FILE_SIZE - 1)
        assert norm == f"bytes={self.FILE_SIZE - 1024}-{self.FILE_SIZE - 1}"

    def test_case_8_suffix_1mb(self):
        """8. bytes=-1048576: Final 1 MB."""
        suffix = 1048576
        norm, bounds, unsat = _normalize_range_header(
            f"bytes=-{suffix}", self.FILE_SIZE
        )
        assert unsat is False
        assert bounds == (self.FILE_SIZE - suffix, self.FILE_SIZE - 1)
        assert norm == f"bytes={self.FILE_SIZE - suffix}-{self.FILE_SIZE - 1}"

    def test_case_9_open_ended_near_eof(self):
        """9. bytes=<N>- where N is near EOF."""
        start = self.FILE_SIZE - 100
        norm, bounds, unsat = _normalize_range_header(f"bytes={start}-", self.FILE_SIZE)
        assert unsat is False
        assert bounds == (start, self.FILE_SIZE - 1)
        assert norm == f"bytes={start}-{self.FILE_SIZE - 1}"

    def test_case_10_closed_range_m_gte_n(self):
        """10. bytes=<N>-<M> where M >= N within file bounds."""
        norm, bounds, unsat = _normalize_range_header("bytes=500-600", self.FILE_SIZE)
        assert unsat is False
        assert bounds == (500, 600)
        assert norm == "bytes=500-600"

    def test_case_11_closed_range_m_exceeds_eof(self):
        """11. bytes=<N>-<M> where M exceeds EOF (must be clamped to L-1 per RFC 9110 §14.1.2)."""
        norm, bounds, unsat = _normalize_range_header(
            f"bytes=5000-{self.FILE_SIZE + 50000}", self.FILE_SIZE
        )
        assert unsat is False
        assert bounds == (5000, self.FILE_SIZE - 1)
        assert norm == f"bytes=5000-{self.FILE_SIZE - 1}"

    def test_case_12_unsatisfiable_n_beyond_eof(self):
        """12. bytes=<N>-<M> where N is beyond EOF (must trigger 416)."""
        norm, bounds, unsat = _normalize_range_header(
            f"bytes={self.FILE_SIZE + 100}-{self.FILE_SIZE + 200}", self.FILE_SIZE
        )
        assert unsat is True
        assert bounds is None
        assert norm is None

    def test_case_12b_unsatisfiable_large_start(self):
        """12b. Out of bounds range such as bytes=70000000000-80000000000."""
        norm, bounds, unsat = _normalize_range_header(
            "bytes=70000000000-80000000000", self.FILE_SIZE
        )
        assert unsat is True
        assert bounds is None
        assert norm is None

    def test_case_13_malformed_ranges(self):
        """13. Malformed Range syntaxes must be ignored (serve 200)."""
        for malformed in ["bytes=abc", "bytes=--1", "bytes=100-50", "bytes=", ""]:
            norm, bounds, unsat = _normalize_range_header(malformed, self.FILE_SIZE)
            assert unsat is False
            assert bounds is None
            assert norm is None

    def test_case_14_unsupported_range_unit(self):
        """14. Unsupported range units (e.g. seconds=0-10) must be ignored."""
        norm, bounds, unsat = _normalize_range_header("seconds=0-10", self.FILE_SIZE)
        assert unsat is False
        assert bounds is None
        assert norm is None

    def test_case_15_empty_invalid_suffix_range(self):
        """15. Empty or invalid suffix ranges (e.g. bytes=-, bytes=-0) must be ignored."""
        for invalid in ["bytes=-", "bytes=-0", "bytes=-abc"]:
            norm, bounds, unsat = _normalize_range_header(invalid, self.FILE_SIZE)
            assert unsat is False
            assert bounds is None
            assert norm is None

    def test_case_16_multiple_ranges_first_selected(self):
        """16. Multiple ranges: single-part endpoint selects the first valid range."""
        norm, bounds, unsat = _normalize_range_header(
            "bytes=0-100, 200-300", self.FILE_SIZE
        )
        assert unsat is False
        assert bounds == (0, 100)
        assert norm == "bytes=0-100"

    def test_suffix_exceeds_representation_length(self):
        """Suffix larger than representation length must return entire file (RFC 9110 §14.1.2)."""
        norm, bounds, unsat = _normalize_range_header("bytes=-500", 200)
        assert unsat is False
        assert bounds == (0, 199)
        assert norm == "bytes=0-199"

    def test_zero_byte_representation_is_unsatisfiable(self):
        """Any range against 0-byte representation is unsatisfiable."""
        norm, bounds, unsat = _normalize_range_header("bytes=0-0", 0)
        assert unsat is True
        assert bounds is None
        assert norm is None


class TestStreamEndpointHTTP416AndRanges:
    """Test the stream_file endpoint handling of 416, 206, and headers."""

    @pytest.mark.asyncio
    async def test_unsatisfiable_range_returns_416_with_content_range(self):
        """Unsatisfiable range returns HTTP 416 with Content-Range: bytes */<length>."""
        req = _make_request({"Range": "bytes=90000000-99999999"})

        with patch(
            "routers.secure.stream._get_media_info",
            return_value=("http://upstream/video.mp4", "", "video.mp4", 1_000_000),
        ):
            resp = await stream_file(1, req)

            assert isinstance(resp, Response)
            assert resp.status_code == 416
            assert resp.headers.get("content-range") == "bytes */1000000"
            assert resp.headers.get("accept-ranges") == "bytes"

    @pytest.mark.asyncio
    async def test_suffix_range_forwarded_as_resolved_start_end(self):
        """Suffix range bytes=-1024 is resolved into explicit start-end to protect against buggy CDNs."""
        req = _make_request({"Range": "bytes=-1024"})
        file_size = 100_000

        mock_upstream_resp = MagicMock()
        mock_upstream_resp.status_code = 206
        mock_upstream_resp.headers = {
            "content-type": "video/mp4",
            "content-range": f"bytes {file_size - 1024}-{file_size - 1}/{file_size}",
            "content-length": "1024",
            "accept-ranges": "bytes",
        }
        mock_upstream_resp.is_closed = False

        async def dummy_bytes():
            yield b"x" * 1024

        mock_upstream_resp.aiter_bytes = dummy_bytes
        mock_upstream_resp.aclose = AsyncMock()

        mock_client = MagicMock()
        mock_client.build_request = MagicMock(return_value=MagicMock())
        mock_client.send = AsyncMock(return_value=mock_upstream_resp)

        with (
            patch(
                "routers.secure.stream._get_media_info",
                return_value=("http://upstream/video.mp4", "", "video.mp4", file_size),
            ),
            patch("routers.secure.stream._get_client", return_value=mock_client),
        ):
            resp = await stream_file(1, req)

            # Verify client.build_request was called with normalized Range header
            call_kwargs = mock_client.build_request.call_args[1]
            assert (
                call_kwargs["headers"]["Range"]
                == f"bytes={file_size - 1024}-{file_size - 1}"
            )
            assert isinstance(resp, StreamingResponse)
            assert resp.status_code == 206
            assert resp.headers.get("content-range") == (
                f"bytes {file_size - 1024}-{file_size - 1}/{file_size}"
            )
            assert resp.headers.get("content-length") == "1024"
            assert resp.headers.get("accept-ranges") == "bytes"

    @pytest.mark.asyncio
    async def test_upstream_416_handled_with_content_range(self):
        """If upstream itself returns 416, it is passed through with Content-Range and Accept-Ranges."""
        req = _make_request({"Range": "bytes=0-100"})

        mock_upstream_resp = MagicMock()
        mock_upstream_resp.status_code = 416
        mock_upstream_resp.headers = {
            "content-range": "bytes */5000",
        }
        mock_upstream_resp.aclose = AsyncMock()

        mock_client = MagicMock()
        mock_client.build_request = MagicMock(return_value=MagicMock())
        mock_client.send = AsyncMock(return_value=mock_upstream_resp)

        with (
            patch(
                "routers.secure.stream._get_media_info",
                return_value=("http://upstream/video.mp4", "", "video.mp4", 5000),
            ),
            patch("routers.secure.stream._get_client", return_value=mock_client),
        ):
            resp = await stream_file(1, req)

            assert isinstance(resp, Response)
            assert resp.status_code == 416
            assert resp.headers.get("content-range") == "bytes */5000"
            assert resp.headers.get("accept-ranges") == "bytes"
