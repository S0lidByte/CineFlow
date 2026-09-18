"""
Phase 4.3 Track 1: Durable Link Refresh for HTTP 401/403 CDN Failures.

Tests cover:
- 401/403 durable recovery with bounded refresh attempts (max 2)
- Exhaustion at 2 attempts raising DebridServiceForbiddenException
- Identical URL / failed refresh fail-over without infinite loops
- LinkUnavailable propagation
- Non-refreshable statuses (429 rate limit backoff, 416 range error) without refresh
- Single-flight cross-instance locking and DB re-check (0 provider calls when DB updated)
- Persisted URL storage in DB
- Existing 404 / 410 / 503 one-shot recovery preservation
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
import trio

from program.media.filesystem_entry import FilesystemEntry
from program.media.media_entry import MediaEntry
from program.services.filesystem.vfs.db import (
    GetEntryByOriginalFilenameResult,
    VFSDatabase,
)
from program.services.streaming.exceptions import (
    DebridServiceForbiddenException,
    DebridServiceLinkUnavailable,
    DebridServiceRangeNotSatisfiableException,
    DebridServiceRateLimitedException,
    DebridServiceUnableToConnectException,
)
from program.services.streaming.media_stream import (
    _MEDIA_STREAM_REFRESH_LOCKS,
    _REFRESH_LOCKS_MUTEX,
    MediaStream,
)


@pytest.fixture(autouse=True)
def _clear_refresh_locks():
    with _REFRESH_LOCKS_MUTEX:
        _MEDIA_STREAM_REFRESH_LOCKS.clear()
    yield
    with _REFRESH_LOCKS_MUTEX:
        _MEDIA_STREAM_REFRESH_LOCKS.clear()


def _make_stream(
    original_filename: str = "test_movie.mkv",
    initial_url: str = "https://cdn.example.com/url_a",
    provider: str = "realdebrid",
) -> MediaStream:
    stream = MediaStream.__new__(MediaStream)
    stream.fh = 1
    stream.provider = provider
    stream._use_proxy_client = False
    stream._http_pool = None
    stream.target_url = SimpleNamespace(value=initial_url)
    stream.file_metadata = SimpleNamespace(
        path=f"/{original_filename}",
        original_filename=original_filename,
        file_size=1024 * 1024 * 100,
    )
    stream.session_statistics = SimpleNamespace(
        bytes_transferred=0,
        total_session_connections=0,
    )
    stream._active_stream_connection = None
    stream.enable_tracing = False
    stream.build_log_message = lambda msg: msg  # type: ignore[method-assign]
    return stream


class MockStreamResponse:
    def __init__(
        self,
        status_code: int,
        url: str,
        content: bytes = b"test content data",
        headers: dict[str, str] | None = None,
    ):
        self.status_code = status_code
        self.http_version = "HTTP/1.1"
        self.content = content
        hdrs = {
            "Content-Length": str(len(content)),
            "Content-Range": f"bytes 0-{len(content) - 1}/{len(content)}",
            "Accept-Ranges": "bytes",
        }
        if headers:
            hdrs.update(headers)
        self.headers = httpx.Headers(hdrs)
        self.request = httpx.Request("GET", url)

    def raise_for_status(self):
        if self.status_code >= 400:
            response = httpx.Response(
                self.status_code, request=self.request, headers=self.headers
            )
            raise httpx.HTTPStatusError(
                message=f"HTTP {self.status_code}",
                request=self.request,
                response=response,
            )

    async def __aenter__(self):
        self.raise_for_status()
        return self

    async def __aexit__(self, *args):
        return False


@asynccontextmanager
async def _noop_admit(_kind: str):
    yield


# =========================================================================
# Basic Recovery Scenarios
# =========================================================================


def test_403_refresh_success():
    """URL-A -> 403 -> refresh -> URL-B -> 200. Exactly 1 durable refresh."""
    stream = _make_stream(initial_url="https://cdn.example.com/url_a")
    unrestrict_call_count = 0

    def mock_get_entry(original_filename: str, force_resolve: bool = False):
        nonlocal unrestrict_call_count
        if force_resolve:
            unrestrict_call_count += 1
            return GetEntryByOriginalFilenameResult(
                original_filename=original_filename,
                download_url="https://provider.example.com/dl",
                unrestricted_url="https://cdn.example.com/url_b",
                provider="realdebrid",
                provider_download_id="123",
                size=1000,
                created="2026-01-01T00:00:00",
                modified="2026-01-01T00:00:00",
                entry_type="media",
            )
        return GetEntryByOriginalFilenameResult(
            original_filename=original_filename,
            download_url="https://provider.example.com/dl",
            unrestricted_url="https://cdn.example.com/url_a",
            provider="realdebrid",
            provider_download_id="123",
            size=1000,
            created="2026-01-01T00:00:00",
            modified="2026-01-01T00:00:00",
            entry_type="media",
        )

    mock_vfs_db = MagicMock(spec=VFSDatabase)
    mock_vfs_db.get_entry_by_original_filename.side_effect = mock_get_entry

    mock_client = MagicMock()

    def stream_handler(method: str, url: str, **kwargs):
        if url == "https://cdn.example.com/url_a":
            return MockStreamResponse(403, url)
        elif url == "https://cdn.example.com/url_b":
            return MockStreamResponse(200, url)
        return MockStreamResponse(404, url)

    mock_client.stream.side_effect = stream_handler

    async def _run():
        with (
            patch.object(stream, "_resolve_async_client", return_value=mock_client),
            patch(
                "program.services.streaming.media_stream.admit_stream_request",
                new=_noop_admit,
            ),
            patch("program.services.streaming.media_stream.di") as mock_di,
        ):
            mock_di.__getitem__.return_value = mock_vfs_db

            async with stream.establish_connection(start=0) as response:
                assert response.status_code == 200

    trio.run(_run)

    assert unrestrict_call_count == 1
    assert stream.target_url.value == "https://cdn.example.com/url_b"


def test_401_refresh_success():
    """URL-A -> 401 -> refresh -> URL-B -> 200. Exactly 1 durable refresh."""
    stream = _make_stream(initial_url="https://cdn.example.com/url_a")
    unrestrict_call_count = 0

    def mock_get_entry(original_filename: str, force_resolve: bool = False):
        nonlocal unrestrict_call_count
        if force_resolve:
            unrestrict_call_count += 1
            return GetEntryByOriginalFilenameResult(
                original_filename=original_filename,
                download_url="https://provider.example.com/dl",
                unrestricted_url="https://cdn.example.com/url_b",
                provider="realdebrid",
                provider_download_id="123",
                size=1000,
                created="2026-01-01T00:00:00",
                modified="2026-01-01T00:00:00",
                entry_type="media",
            )
        return GetEntryByOriginalFilenameResult(
            original_filename=original_filename,
            download_url="https://provider.example.com/dl",
            unrestricted_url="https://cdn.example.com/url_a",
            provider="realdebrid",
            provider_download_id="123",
            size=1000,
            created="2026-01-01T00:00:00",
            modified="2026-01-01T00:00:00",
            entry_type="media",
        )

    mock_vfs_db = MagicMock(spec=VFSDatabase)
    mock_vfs_db.get_entry_by_original_filename.side_effect = mock_get_entry

    mock_client = MagicMock()

    def stream_handler(method: str, url: str, **kwargs):
        if url == "https://cdn.example.com/url_a":
            return MockStreamResponse(401, url)
        elif url == "https://cdn.example.com/url_b":
            return MockStreamResponse(200, url)
        return MockStreamResponse(404, url)

    mock_client.stream.side_effect = stream_handler

    async def _run():
        with (
            patch.object(stream, "_resolve_async_client", return_value=mock_client),
            patch(
                "program.services.streaming.media_stream.admit_stream_request",
                new=_noop_admit,
            ),
            patch("program.services.streaming.media_stream.di") as mock_di,
        ):
            mock_di.__getitem__.return_value = mock_vfs_db

            async with stream.establish_connection(start=0) as response:
                assert response.status_code == 200

    trio.run(_run)

    assert unrestrict_call_count == 1
    assert stream.target_url.value == "https://cdn.example.com/url_b"


def test_403_two_step_recovery():
    """URL-A -> 403 -> URL-B -> 403 -> URL-C -> 200. Exactly 2 durable refreshes."""
    stream = _make_stream(initial_url="https://cdn.example.com/url_a")
    unrestrict_call_count = 0
    urls = [
        "https://cdn.example.com/url_b",
        "https://cdn.example.com/url_c",
    ]

    current_persisted_url = "https://cdn.example.com/url_a"

    def mock_get_entry(original_filename: str, force_resolve: bool = False):
        nonlocal unrestrict_call_count, current_persisted_url
        if force_resolve:
            current_persisted_url = urls[unrestrict_call_count]
            unrestrict_call_count += 1
            return GetEntryByOriginalFilenameResult(
                original_filename=original_filename,
                download_url="https://provider.example.com/dl",
                unrestricted_url=current_persisted_url,
                provider="realdebrid",
                provider_download_id="123",
                size=1000,
                created="2026-01-01T00:00:00",
                modified="2026-01-01T00:00:00",
                entry_type="media",
            )
        return GetEntryByOriginalFilenameResult(
            original_filename=original_filename,
            download_url="https://provider.example.com/dl",
            unrestricted_url=current_persisted_url,
            provider="realdebrid",
            provider_download_id="123",
            size=1000,
            created="2026-01-01T00:00:00",
            modified="2026-01-01T00:00:00",
            entry_type="media",
        )

    mock_vfs_db = MagicMock(spec=VFSDatabase)
    mock_vfs_db.get_entry_by_original_filename.side_effect = mock_get_entry

    mock_client = MagicMock()

    def stream_handler(method: str, url: str, **kwargs):
        if url in ("https://cdn.example.com/url_a", "https://cdn.example.com/url_b"):
            return MockStreamResponse(403, url)
        elif url == "https://cdn.example.com/url_c":
            return MockStreamResponse(200, url)
        return MockStreamResponse(404, url)

    mock_client.stream.side_effect = stream_handler

    async def _run():
        with (
            patch.object(stream, "_resolve_async_client", return_value=mock_client),
            patch(
                "program.services.streaming.media_stream.admit_stream_request",
                new=_noop_admit,
            ),
            patch("program.services.streaming.media_stream.di") as mock_di,
        ):
            mock_di.__getitem__.return_value = mock_vfs_db

            async with stream.establish_connection(start=0) as response:
                assert response.status_code == 200

    trio.run(_run)

    assert unrestrict_call_count == 2
    assert stream.target_url.value == "https://cdn.example.com/url_c"


def test_refresh_exhausted_at_two_attempts():
    """URL-A -> 403 -> URL-B -> 403 -> URL-C -> 403. Max 2 refreshes, 3rd fails with DebridServiceForbiddenException."""
    stream = _make_stream(initial_url="https://cdn.example.com/url_a")
    unrestrict_call_count = 0
    urls = [
        "https://cdn.example.com/url_b",
        "https://cdn.example.com/url_c",
        "https://cdn.example.com/url_d",
    ]
    current_persisted_url = "https://cdn.example.com/url_a"

    def mock_get_entry(original_filename: str, force_resolve: bool = False):
        nonlocal unrestrict_call_count, current_persisted_url
        if force_resolve:
            current_persisted_url = urls[unrestrict_call_count]
            unrestrict_call_count += 1
            return GetEntryByOriginalFilenameResult(
                original_filename=original_filename,
                download_url="https://provider.example.com/dl",
                unrestricted_url=current_persisted_url,
                provider="realdebrid",
                provider_download_id="123",
                size=1000,
                created="2026-01-01T00:00:00",
                modified="2026-01-01T00:00:00",
                entry_type="media",
            )
        return GetEntryByOriginalFilenameResult(
            original_filename=original_filename,
            download_url="https://provider.example.com/dl",
            unrestricted_url=current_persisted_url,
            provider="realdebrid",
            provider_download_id="123",
            size=1000,
            created="2026-01-01T00:00:00",
            modified="2026-01-01T00:00:00",
            entry_type="media",
        )

    mock_vfs_db = MagicMock(spec=VFSDatabase)
    mock_vfs_db.get_entry_by_original_filename.side_effect = mock_get_entry

    mock_client = MagicMock()

    def stream_handler(method: str, url: str, **kwargs):
        # All URLs return 403
        return MockStreamResponse(403, url)

    mock_client.stream.side_effect = stream_handler

    async def _run():
        with (
            patch.object(stream, "_resolve_async_client", return_value=mock_client),
            patch(
                "program.services.streaming.media_stream.admit_stream_request",
                new=_noop_admit,
            ),
            patch("program.services.streaming.media_stream.di") as mock_di,
        ):
            mock_di.__getitem__.return_value = mock_vfs_db

            with pytest.raises(DebridServiceForbiddenException):
                async with stream.establish_connection(start=0):
                    pass

    trio.run(_run)

    # Must be capped at exactly 2 provider unrestrict calls
    assert unrestrict_call_count == 2


# =========================================================================
# Failed Refresh Scenarios
# =========================================================================


def test_identical_url_fails_over():
    """Provider returns identical URL to failed URL. Must not retry endlessly; raises DebridServiceForbiddenException."""
    stream = _make_stream(initial_url="https://cdn.example.com/url_a")
    unrestrict_call_count = 0

    def mock_get_entry(original_filename: str, force_resolve: bool = False):
        nonlocal unrestrict_call_count
        if force_resolve:
            unrestrict_call_count += 1
            # Provider returns the exact same dead URL
            return GetEntryByOriginalFilenameResult(
                original_filename=original_filename,
                download_url="https://provider.example.com/dl",
                unrestricted_url="https://cdn.example.com/url_a",
                provider="realdebrid",
                provider_download_id="123",
                size=1000,
                created="2026-01-01T00:00:00",
                modified="2026-01-01T00:00:00",
                entry_type="media",
            )
        return GetEntryByOriginalFilenameResult(
            original_filename=original_filename,
            download_url="https://provider.example.com/dl",
            unrestricted_url="https://cdn.example.com/url_a",
            provider="realdebrid",
            provider_download_id="123",
            size=1000,
            created="2026-01-01T00:00:00",
            modified="2026-01-01T00:00:00",
            entry_type="media",
        )

    mock_vfs_db = MagicMock(spec=VFSDatabase)
    mock_vfs_db.get_entry_by_original_filename.side_effect = mock_get_entry

    mock_client = MagicMock()
    mock_client.stream.side_effect = lambda method, url, **kwargs: MockStreamResponse(
        403, url
    )

    async def _run():
        with (
            patch.object(stream, "_resolve_async_client", return_value=mock_client),
            patch(
                "program.services.streaming.media_stream.admit_stream_request",
                new=_noop_admit,
            ),
            patch("program.services.streaming.media_stream.di") as mock_di,
        ):
            mock_di.__getitem__.return_value = mock_vfs_db

            with pytest.raises(DebridServiceForbiddenException):
                async with stream.establish_connection(start=0):
                    pass

    trio.run(_run)

    # Exactly 1 unrestrict attempted before recognizing identical dead link
    assert unrestrict_call_count == 1


def test_link_unavailable_fallback():
    """Downloader/provider raises DebridServiceLinkUnavailable during refresh."""
    stream = _make_stream(initial_url="https://cdn.example.com/url_a")

    def mock_get_entry(original_filename: str, force_resolve: bool = False):
        if force_resolve:
            raise DebridServiceLinkUnavailable(
                provider="realdebrid", link="https://provider.example.com/dl"
            )
        return GetEntryByOriginalFilenameResult(
            original_filename=original_filename,
            download_url="https://provider.example.com/dl",
            unrestricted_url="https://cdn.example.com/url_a",
            provider="realdebrid",
            provider_download_id="123",
            size=1000,
            created="2026-01-01T00:00:00",
            modified="2026-01-01T00:00:00",
            entry_type="media",
        )

    mock_vfs_db = MagicMock(spec=VFSDatabase)
    mock_vfs_db.get_entry_by_original_filename.side_effect = mock_get_entry

    mock_client = MagicMock()
    mock_client.stream.side_effect = lambda method, url, **kwargs: MockStreamResponse(
        403, url
    )

    async def _run():
        with (
            patch.object(stream, "_resolve_async_client", return_value=mock_client),
            patch(
                "program.services.streaming.media_stream.admit_stream_request",
                new=_noop_admit,
            ),
            patch("program.services.streaming.media_stream.di") as mock_di,
        ):
            mock_di.__getitem__.return_value = mock_vfs_db

            with pytest.raises(DebridServiceLinkUnavailable):
                async with stream.establish_connection(start=0):
                    pass

    trio.run(_run)


# =========================================================================
# Non-refreshable Statuses
# =========================================================================


def test_non_refreshable_429():
    """HTTP 429 must trigger rate-limit backoff without refreshing the URL."""
    stream = _make_stream(initial_url="https://cdn.example.com/url_a")
    mock_vfs_db = MagicMock(spec=VFSDatabase)

    mock_client = MagicMock()
    mock_client.stream.side_effect = lambda method, url, **kwargs: MockStreamResponse(
        429, url
    )

    retry_calls = 0

    async def _mock_retry(*args):
        nonlocal retry_calls
        retry_calls += 1
        return retry_calls < 4

    async def _run():
        with (
            patch.object(stream, "_resolve_async_client", return_value=mock_client),
            patch.object(stream, "_retry_with_backoff", new=_mock_retry),
            patch(
                "program.services.streaming.media_stream.admit_stream_request",
                new=_noop_admit,
            ),
            patch("program.services.streaming.media_stream.di") as mock_di,
        ):
            mock_di.__getitem__.return_value = mock_vfs_db

            with pytest.raises(DebridServiceRateLimitedException):
                async with stream.establish_connection(start=0):
                    pass

    trio.run(_run)

    # 0 provider unrestrict calls
    mock_vfs_db.get_entry_by_original_filename.assert_not_called()
    assert retry_calls == 4


def test_non_refreshable_416():
    """HTTP 416 Range Not Satisfiable must raise DebridServiceRangeNotSatisfiableException immediately with 0 refreshes."""
    stream = _make_stream(initial_url="https://cdn.example.com/url_a")
    mock_vfs_db = MagicMock(spec=VFSDatabase)

    mock_client = MagicMock()
    mock_client.stream.side_effect = lambda method, url, **kwargs: MockStreamResponse(
        416, url
    )

    async def _run():
        with (
            patch.object(stream, "_resolve_async_client", return_value=mock_client),
            patch(
                "program.services.streaming.media_stream.admit_stream_request",
                new=_noop_admit,
            ),
            patch("program.services.streaming.media_stream.di") as mock_di,
        ):
            mock_di.__getitem__.return_value = mock_vfs_db

            with pytest.raises(DebridServiceRangeNotSatisfiableException):
                async with stream.establish_connection(start=0):
                    pass

    trio.run(_run)

    mock_vfs_db.get_entry_by_original_filename.assert_not_called()


# =========================================================================
# Concurrency & Single-Flight Coordination
# =========================================================================


def test_concurrent_readers_single_stream():
    """Concurrent tasks using one MediaStream encountering 403 on URL-A result in 1 provider unrestrict call."""
    stream = _make_stream(initial_url="https://cdn.example.com/url_a")
    unrestrict_call_count = 0
    persisted_url = "https://cdn.example.com/url_a"

    def mock_get_entry(original_filename: str, force_resolve: bool = False):
        nonlocal unrestrict_call_count, persisted_url
        if force_resolve:
            unrestrict_call_count += 1
            persisted_url = "https://cdn.example.com/url_b"
            return GetEntryByOriginalFilenameResult(
                original_filename=original_filename,
                download_url="https://provider.example.com/dl",
                unrestricted_url=persisted_url,
                provider="realdebrid",
                provider_download_id="123",
                size=1000,
                created="2026-01-01T00:00:00",
                modified="2026-01-01T00:00:00",
                entry_type="media",
            )
        return GetEntryByOriginalFilenameResult(
            original_filename=original_filename,
            download_url="https://provider.example.com/dl",
            unrestricted_url=persisted_url,
            provider="realdebrid",
            provider_download_id="123",
            size=1000,
            created="2026-01-01T00:00:00",
            modified="2026-01-01T00:00:00",
            entry_type="media",
        )

    mock_vfs_db = MagicMock(spec=VFSDatabase)
    mock_vfs_db.get_entry_by_original_filename.side_effect = mock_get_entry

    mock_client = MagicMock()

    def stream_handler(method: str, url: str, **kwargs):
        if url == "https://cdn.example.com/url_a":
            return MockStreamResponse(403, url)
        elif url == "https://cdn.example.com/url_b":
            return MockStreamResponse(200, url)
        return MockStreamResponse(404, url)

    mock_client.stream.side_effect = stream_handler

    async def _reader():
        async with stream.establish_connection(start=0) as response:
            assert response.status_code == 200

    async def _run():
        with (
            patch.object(stream, "_resolve_async_client", return_value=mock_client),
            patch(
                "program.services.streaming.media_stream.admit_stream_request",
                new=_noop_admit,
            ),
            patch("program.services.streaming.media_stream.di") as mock_di,
        ):
            mock_di.__getitem__.return_value = mock_vfs_db

            async with trio.open_nursery() as nursery:
                nursery.start_soon(_reader)
                nursery.start_soon(_reader)
                nursery.start_soon(_reader)

    trio.run(_run)

    assert unrestrict_call_count == 1
    assert stream.target_url.value == "https://cdn.example.com/url_b"


def test_concurrent_readers_separate_streams():
    """Two different MediaStream instances for the same original_filename coordinate via scoped lock; provider called once."""
    stream_a = _make_stream(
        original_filename="shared_file.mkv", initial_url="https://cdn.example.com/url_a"
    )
    stream_b = _make_stream(
        original_filename="shared_file.mkv", initial_url="https://cdn.example.com/url_a"
    )

    unrestrict_call_count = 0
    persisted_url = "https://cdn.example.com/url_a"

    def mock_get_entry(original_filename: str, force_resolve: bool = False):
        nonlocal unrestrict_call_count, persisted_url
        if force_resolve:
            unrestrict_call_count += 1
            persisted_url = "https://cdn.example.com/url_b"
            return GetEntryByOriginalFilenameResult(
                original_filename=original_filename,
                download_url="https://provider.example.com/dl",
                unrestricted_url=persisted_url,
                provider="realdebrid",
                provider_download_id="123",
                size=1000,
                created="2026-01-01T00:00:00",
                modified="2026-01-01T00:00:00",
                entry_type="media",
            )
        return GetEntryByOriginalFilenameResult(
            original_filename=original_filename,
            download_url="https://provider.example.com/dl",
            unrestricted_url=persisted_url,
            provider="realdebrid",
            provider_download_id="123",
            size=1000,
            created="2026-01-01T00:00:00",
            modified="2026-01-01T00:00:00",
            entry_type="media",
        )

    mock_vfs_db = MagicMock(spec=VFSDatabase)
    mock_vfs_db.get_entry_by_original_filename.side_effect = mock_get_entry

    mock_client = MagicMock()

    def stream_handler(method: str, url: str, **kwargs):
        if url == "https://cdn.example.com/url_a":
            return MockStreamResponse(403, url)
        elif url == "https://cdn.example.com/url_b":
            return MockStreamResponse(200, url)
        return MockStreamResponse(404, url)

    mock_client.stream.side_effect = stream_handler

    async def _reader_a():
        async with stream_a.establish_connection(start=0) as response:
            assert response.status_code == 200

    async def _reader_b():
        async with stream_b.establish_connection(start=0) as response:
            assert response.status_code == 200

    async def _run():
        with (
            patch.object(stream_a, "_resolve_async_client", return_value=mock_client),
            patch.object(stream_b, "_resolve_async_client", return_value=mock_client),
            patch(
                "program.services.streaming.media_stream.admit_stream_request",
                new=_noop_admit,
            ),
            patch("program.services.streaming.media_stream.di") as mock_di,
        ):
            mock_di.__getitem__.return_value = mock_vfs_db

            async with trio.open_nursery() as nursery:
                nursery.start_soon(_reader_a)
                nursery.start_soon(_reader_b)

    trio.run(_run)

    assert unrestrict_call_count == 1
    assert stream_a.target_url.value == "https://cdn.example.com/url_b"
    assert stream_b.target_url.value == "https://cdn.example.com/url_b"


def test_race_reader_adopts_persisted_url():
    """Reader B checks DB when DB already has URL-B; Reader B makes 0 provider unrestrict calls and adopts URL-B."""
    stream_b = _make_stream(
        original_filename="movie.mkv", initial_url="https://cdn.example.com/url_a"
    )

    unrestrict_call_count = 0

    def mock_get_entry(original_filename: str, force_resolve: bool = False):
        nonlocal unrestrict_call_count
        if force_resolve:
            unrestrict_call_count += 1
            return GetEntryByOriginalFilenameResult(
                original_filename=original_filename,
                download_url="https://provider.example.com/dl",
                unrestricted_url="https://cdn.example.com/url_c",
                provider="realdebrid",
                provider_download_id="123",
                size=1000,
                created="2026-01-01T00:00:00",
                modified="2026-01-01T00:00:00",
                entry_type="media",
            )
        # DB already has URL-B from another reader
        return GetEntryByOriginalFilenameResult(
            original_filename=original_filename,
            download_url="https://provider.example.com/dl",
            unrestricted_url="https://cdn.example.com/url_b",
            provider="realdebrid",
            provider_download_id="123",
            size=1000,
            created="2026-01-01T00:00:00",
            modified="2026-01-01T00:00:00",
            entry_type="media",
        )

    mock_vfs_db = MagicMock(spec=VFSDatabase)
    mock_vfs_db.get_entry_by_original_filename.side_effect = mock_get_entry

    async def _run():
        with patch("program.services.streaming.media_stream.di") as mock_di:
            mock_di.__getitem__.return_value = mock_vfs_db
            res = await stream_b._refresh_download_url(
                failed_url="https://cdn.example.com/url_a"
            )
            assert res is True
            assert stream_b.target_url.value == "https://cdn.example.com/url_b"

    trio.run(_run)

    # 0 unrestrict calls made by Reader B because DB had newer URL
    assert unrestrict_call_count == 0


# =========================================================================
# Persistence Verification
# =========================================================================


def test_refreshed_url_persistence():
    """Verify that VFSDatabase.refresh_unrestricted_url persists the new URL to DB and session commit."""
    mock_service = MagicMock()
    mock_service.key = "realdebrid"
    mock_service.unrestrict_link.return_value = SimpleNamespace(
        download="https://cdn.example.com/fresh_persisted_url"
    )

    mock_downloader = MagicMock()
    mock_downloader.services = {"RealDebrid": mock_service}

    vfs_db = VFSDatabase(downloader=mock_downloader)

    entry = MediaEntry(
        original_filename="persisted_test.mkv",
        download_url="https://real-debrid.com/d/123",
        unrestricted_url="https://cdn.example.com/stale_url",
        provider="realdebrid",
    )

    session = MagicMock()

    refreshed_url = vfs_db.refresh_unrestricted_url(entry, session=session)

    assert refreshed_url == "https://cdn.example.com/fresh_persisted_url"
    assert entry.unrestricted_url == "https://cdn.example.com/fresh_persisted_url"
    session.merge.assert_called_once_with(entry)
    session.commit.assert_called_once()


# =========================================================================
# Preserved One-Shot Statuses (404, 410, 503)
# =========================================================================


def test_existing_404_preserved():
    """HTTP 404 triggers one-shot URL refresh on attempt 0 and reconnects successfully."""
    stream = _make_stream(initial_url="https://cdn.example.com/url_a")
    unrestrict_call_count = 0

    def mock_get_entry(original_filename: str, force_resolve: bool = False):
        nonlocal unrestrict_call_count
        if force_resolve:
            unrestrict_call_count += 1
            return GetEntryByOriginalFilenameResult(
                original_filename=original_filename,
                download_url="https://provider.example.com/dl",
                unrestricted_url="https://cdn.example.com/url_b",
                provider="realdebrid",
                provider_download_id="123",
                size=1000,
                created="2026-01-01T00:00:00",
                modified="2026-01-01T00:00:00",
                entry_type="media",
            )
        return GetEntryByOriginalFilenameResult(
            original_filename=original_filename,
            download_url="https://provider.example.com/dl",
            unrestricted_url="https://cdn.example.com/url_a",
            provider="realdebrid",
            provider_download_id="123",
            size=1000,
            created="2026-01-01T00:00:00",
            modified="2026-01-01T00:00:00",
            entry_type="media",
        )

    mock_vfs_db = MagicMock(spec=VFSDatabase)
    mock_vfs_db.get_entry_by_original_filename.side_effect = mock_get_entry

    mock_client = MagicMock()

    def stream_handler(method: str, url: str, **kwargs):
        if url == "https://cdn.example.com/url_a":
            return MockStreamResponse(404, url)
        elif url == "https://cdn.example.com/url_b":
            return MockStreamResponse(200, url)
        return MockStreamResponse(404, url)

    mock_client.stream.side_effect = stream_handler

    async def _run():
        with (
            patch.object(stream, "_resolve_async_client", return_value=mock_client),
            patch.object(stream, "_retry_with_backoff", return_value=True),
            patch(
                "program.services.streaming.media_stream.admit_stream_request",
                new=_noop_admit,
            ),
            patch("program.services.streaming.media_stream.di") as mock_di,
        ):
            mock_di.__getitem__.return_value = mock_vfs_db

            async with stream.establish_connection(start=0) as response:
                assert response.status_code == 200

    trio.run(_run)

    assert unrestrict_call_count == 1
    assert stream.target_url.value == "https://cdn.example.com/url_b"


def test_existing_410_preserved():
    """HTTP 410 triggers one-shot URL refresh on attempt 0 and reconnects successfully."""
    stream = _make_stream(initial_url="https://cdn.example.com/url_a")
    unrestrict_call_count = 0

    def mock_get_entry(original_filename: str, force_resolve: bool = False):
        nonlocal unrestrict_call_count
        if force_resolve:
            unrestrict_call_count += 1
            return GetEntryByOriginalFilenameResult(
                original_filename=original_filename,
                download_url="https://provider.example.com/dl",
                unrestricted_url="https://cdn.example.com/url_b",
                provider="realdebrid",
                provider_download_id="123",
                size=1000,
                created="2026-01-01T00:00:00",
                modified="2026-01-01T00:00:00",
                entry_type="media",
            )
        return GetEntryByOriginalFilenameResult(
            original_filename=original_filename,
            download_url="https://provider.example.com/dl",
            unrestricted_url="https://cdn.example.com/url_a",
            provider="realdebrid",
            provider_download_id="123",
            size=1000,
            created="2026-01-01T00:00:00",
            modified="2026-01-01T00:00:00",
            entry_type="media",
        )

    mock_vfs_db = MagicMock(spec=VFSDatabase)
    mock_vfs_db.get_entry_by_original_filename.side_effect = mock_get_entry

    mock_client = MagicMock()

    def stream_handler(method: str, url: str, **kwargs):
        if url == "https://cdn.example.com/url_a":
            return MockStreamResponse(410, url)
        elif url == "https://cdn.example.com/url_b":
            return MockStreamResponse(200, url)
        return MockStreamResponse(404, url)

    mock_client.stream.side_effect = stream_handler

    async def _run():
        with (
            patch.object(stream, "_resolve_async_client", return_value=mock_client),
            patch.object(stream, "_retry_with_backoff", return_value=True),
            patch(
                "program.services.streaming.media_stream.admit_stream_request",
                new=_noop_admit,
            ),
            patch("program.services.streaming.media_stream.di") as mock_di,
        ):
            mock_di.__getitem__.return_value = mock_vfs_db

            async with stream.establish_connection(start=0) as response:
                assert response.status_code == 200

    trio.run(_run)

    assert unrestrict_call_count == 1
    assert stream.target_url.value == "https://cdn.example.com/url_b"


def test_existing_503_preserved():
    """HTTP 503 triggers one-shot URL refresh on attempt 0 and reconnects successfully."""
    stream = _make_stream(initial_url="https://cdn.example.com/url_a")
    unrestrict_call_count = 0

    def mock_get_entry(original_filename: str, force_resolve: bool = False):
        nonlocal unrestrict_call_count
        if force_resolve:
            unrestrict_call_count += 1
            return GetEntryByOriginalFilenameResult(
                original_filename=original_filename,
                download_url="https://provider.example.com/dl",
                unrestricted_url="https://cdn.example.com/url_b",
                provider="realdebrid",
                provider_download_id="123",
                size=1000,
                created="2026-01-01T00:00:00",
                modified="2026-01-01T00:00:00",
                entry_type="media",
            )
        return GetEntryByOriginalFilenameResult(
            original_filename=original_filename,
            download_url="https://provider.example.com/dl",
            unrestricted_url="https://cdn.example.com/url_a",
            provider="realdebrid",
            provider_download_id="123",
            size=1000,
            created="2026-01-01T00:00:00",
            modified="2026-01-01T00:00:00",
            entry_type="media",
        )

    mock_vfs_db = MagicMock(spec=VFSDatabase)
    mock_vfs_db.get_entry_by_original_filename.side_effect = mock_get_entry

    mock_client = MagicMock()

    def stream_handler(method: str, url: str, **kwargs):
        if url == "https://cdn.example.com/url_a":
            return MockStreamResponse(503, url)
        elif url == "https://cdn.example.com/url_b":
            return MockStreamResponse(200, url)
        return MockStreamResponse(404, url)

    mock_client.stream.side_effect = stream_handler

    async def _run():
        with (
            patch.object(stream, "_resolve_async_client", return_value=mock_client),
            patch.object(stream, "_retry_with_backoff", return_value=True),
            patch(
                "program.services.streaming.media_stream.admit_stream_request",
                new=_noop_admit,
            ),
            patch("program.services.streaming.media_stream.di") as mock_di,
        ):
            mock_di.__getitem__.return_value = mock_vfs_db

            async with stream.establish_connection(start=0) as response:
                assert response.status_code == 200

    trio.run(_run)

    assert unrestrict_call_count == 1
    assert stream.target_url.value == "https://cdn.example.com/url_b"
