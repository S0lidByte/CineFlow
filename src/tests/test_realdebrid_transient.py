"""Real-Debrid 429/5xx must cooldown without blacklisting streams."""

from unittest.mock import Mock, patch

import pytest

from program.services.downloaders.realdebrid import (
    RealDebridDownloader,
    RealDebridError,
    RealDebridErrorCode,
    RealDebridTransientError,
)
from program.services.streaming.exceptions import DebridServiceLinkUnavailable
from program.utils.request import CircuitBreakerOpen


@pytest.fixture
def rd_downloader():
    with patch.object(RealDebridDownloader, "__init__", lambda *_: None):
        rd = RealDebridDownloader()
        rd.key = "realdebrid"
        rd.api = Mock()
        rd.api.BASE_URL = "https://api.real-debrid.com/rest/1.0"
        rd.api.session = Mock()
        rd._fair_usage_until = 0.0
        rd._fair_usage_warned = False
        return rd


def test_maybe_backoff_raises_transient_on_429(rd_downloader):
    response = Mock()
    response.status_code = 429
    response.reason = "Too Many Requests"

    with pytest.raises(RealDebridTransientError) as exc_info:
        rd_downloader._maybe_backoff(response)

    assert exc_info.value.status_code == 429
    assert "[429]" in str(exc_info.value)
    assert exc_info.value.retry_after_seconds == 60.0


def test_maybe_backoff_raises_transient_on_503(rd_downloader):
    response = Mock()
    response.status_code = 503
    response.reason = "Service Unavailable"

    with pytest.raises(RealDebridTransientError) as exc_info:
        rd_downloader._maybe_backoff(response)

    assert exc_info.value.status_code == 503


def test_maybe_backoff_noop_on_200(rd_downloader):
    response = Mock()
    response.status_code = 200
    rd_downloader._maybe_backoff(response)  # does not raise


def test_get_instant_availability_429_raises_circuit_breaker(rd_downloader):
    """Pre-OPEN rate limits must not return None (which blacklists the stream)."""
    transient = RealDebridTransientError(
        "[429] Rate Limit Exceeded",
        status_code=429,
        retry_after_seconds=45.0,
    )

    with patch.object(rd_downloader, "add_torrent", side_effect=transient):
        with pytest.raises(CircuitBreakerOpen) as exc_info:
            rd_downloader.get_instant_availability("abc123", "movie")

    assert exc_info.value.name == "api.real-debrid.com"
    assert exc_info.value.retry_after_seconds == 45.0


def test_get_instant_availability_451_returns_none(rd_downloader):
    """Genuine infringing torrents still fail as not-available (blacklist OK)."""
    with patch.object(
        rd_downloader,
        "add_torrent",
        side_effect=RealDebridError("[451] Infringing Torrent"),
    ):
        assert rd_downloader.get_instant_availability("deadbeef", "movie") is None


def _unrestrict_error_response(error: str, code: RealDebridErrorCode) -> Mock:
    response = Mock()
    response.ok = False
    response.status_code = 400
    response.json.return_value = {"error": error, "error_code": code}
    return response


@pytest.mark.parametrize(
    "code,error",
    [
        (
            RealDebridErrorCode.HOSTER_TEMPORARY_UNAVAILABLE,
            "hoster_temporarily_unavailable",
        ),
        (RealDebridErrorCode.HOSTER_LIMIT_REACHED, "hoster_limit_reached"),
        (RealDebridErrorCode.SERVICE_UNAVAILABLE, "service_unavailable"),
        (RealDebridErrorCode.RESOURCE_UNREACHABLE, "resource_unreachable"),
        (RealDebridErrorCode.HOSTER_IN_MAINTENANCE, "hoster_in_maintenance"),
    ],
)
def test_unrestrict_transient_codes_return_none_not_link_unavailable(
    rd_downloader, code, error
):
    """Transient unrestrict codes must not raise LinkUnavailable (no VFS remove)."""
    rd_downloader.api.session.post.return_value = _unrestrict_error_response(
        error, code
    )
    rd_downloader._maybe_backoff = Mock()

    assert rd_downloader.unrestrict_link("https://real-debrid.com/d/abc") is None


@pytest.mark.parametrize(
    "code,error",
    [
        (RealDebridErrorCode.INFRINGING_FILE, "infringing_file"),
        (RealDebridErrorCode.FILE_UNAVAILABLE, "file_unavailable"),
        (RealDebridErrorCode.FILE_NOT_ALLOWED, "file_not_allowed"),
        (RealDebridErrorCode.TORRENT_FILE_INVALID, "torrent_file_invalid"),
        (RealDebridErrorCode.RESOURCE_NOT_FOUND, "resource_not_found"),
        (RealDebridErrorCode.UNSUPPORTED_HOSTER, "unsupported_hoster"),
    ],
)
def test_unrestrict_permanent_codes_raise_link_unavailable(rd_downloader, code, error):
    """Permanent dead links still raise LinkUnavailable → dead-link re-scrape."""
    rd_downloader.api.session.post.return_value = _unrestrict_error_response(
        error, code
    )
    rd_downloader._maybe_backoff = Mock()

    with pytest.raises(DebridServiceLinkUnavailable):
        rd_downloader.unrestrict_link("https://real-debrid.com/d/abc")
