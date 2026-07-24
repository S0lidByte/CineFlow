"""Real-Debrid 429/5xx must cooldown without blacklisting streams."""

from unittest.mock import Mock, patch

import pytest

from program.services.downloaders.realdebrid import (
    RealDebridDownloader,
    RealDebridError,
    RealDebridTransientError,
)
from program.utils.request import CircuitBreakerOpen


@pytest.fixture
def rd_downloader():
    with patch.object(RealDebridDownloader, "__init__", lambda *_: None):
        rd = RealDebridDownloader()
        rd.key = "realdebrid"
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
