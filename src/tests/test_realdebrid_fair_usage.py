"""Real-Debrid fair-usage cooldown must skip API calls and rate-limit warnings."""

from unittest.mock import MagicMock, Mock, patch

import pytest

from program.services.downloaders.realdebrid import (
    RealDebridDownloader,
    RealDebridErrorCode,
)
from program.services.streaming.exceptions import DebridServiceFairUsageLimitException
from program.utils.debrid_cdn_url import DebridCDNUrl


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


def test_unrestrict_sets_cooldown_and_retry_after(rd_downloader):
    response = Mock()
    response.ok = False
    response.status_code = 503
    response.json.return_value = {
        "error": "fair_usage_limit",
        "error_code": RealDebridErrorCode.FAIR_USAGE_LIMIT,
    }
    rd_downloader.api.session.post.return_value = response
    rd_downloader._maybe_backoff = Mock()

    with pytest.raises(DebridServiceFairUsageLimitException) as exc_info:
        rd_downloader.unrestrict_link("https://real-debrid.com/d/abc")

    assert rd_downloader._fair_usage_until > 0
    assert exc_info.value.retry_after_seconds is not None
    assert exc_info.value.retry_after_seconds > 0
    assert "retry after" in str(exc_info.value)


def test_unrestrict_skips_api_during_cooldown(rd_downloader):
    import time

    rd_downloader._fair_usage_until = time.time() + 120
    rd_downloader._fair_usage_warned = True

    with pytest.raises(DebridServiceFairUsageLimitException) as exc_info:
        rd_downloader.unrestrict_link("https://real-debrid.com/d/abc")

    rd_downloader.api.session.post.assert_not_called()
    assert exc_info.value.retry_after_seconds is not None
    assert exc_info.value.retry_after_seconds > 0


def test_fair_usage_warning_logged_once_per_cooldown(rd_downloader):
    import time

    rd_downloader._fair_usage_until = time.time() + 120
    rd_downloader._fair_usage_warned = False

    with patch(
        "program.services.downloaders.realdebrid.logger"
    ) as mock_logger:
        with pytest.raises(DebridServiceFairUsageLimitException):
            rd_downloader.unrestrict_link("https://real-debrid.com/d/one")

        with pytest.raises(DebridServiceFairUsageLimitException):
            rd_downloader.unrestrict_link("https://real-debrid.com/d/two")

        with pytest.raises(DebridServiceFairUsageLimitException):
            rd_downloader.unrestrict_link("https://real-debrid.com/d/three")

    warning_calls = [
        call
        for call in mock_logger.warning.call_args_list
        if call.args and "Fair usage" in call.args[0]
    ]
    debug_calls = [
        call
        for call in mock_logger.debug.call_args_list
        if call.args and "Fair usage" in call.args[0]
    ]

    assert len(warning_calls) == 1
    assert len(debug_calls) == 0
    rd_downloader.api.session.post.assert_not_called()


def test_cdn_refresh_propagates_fair_usage_even_with_retry_after():
    """Fair usage must not be swallowed as a soft None by circuit-breaker cooldown logic."""

    cdn = DebridCDNUrl.__new__(DebridCDNUrl)
    cdn.filename = "ep.mkv"
    cdn.entry = MagicMock()
    cdn.max_validation_attempts = 3
    cdn.url = None
    cdn.provider = "realdebrid"
    cdn._refresh_cooldown_until = None

    fair_usage = DebridServiceFairUsageLimitException(
        provider="realdebrid",
        retry_after_seconds=90.0,
    )

    with patch.object(cdn, "_refresh", side_effect=fair_usage):
        with pytest.raises(DebridServiceFairUsageLimitException) as exc_info:
            cdn._refresh_with_cooldown()

    assert exc_info.value.retry_after_seconds == 90.0
    assert cdn._get_refresh_cooldown_remaining() > 0
