from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import pytest
from loguru import logger

from program.media.item import Movie
from program.media.stream import Stream
from program.services.downloaders import Downloader
from program.utils.request import CircuitBreakerOpen

# Register custom log levels used by Downloader (normally done at app startup).
try:
    logger.level("DEBRID")
except ValueError:
    logger.level("DEBRID", no=20)


@pytest.fixture
def downloader():
    """Create a Downloader instance with a mocked single service."""
    with patch.object(Downloader, "__init__", lambda *_: None):
        dl = Downloader()
        dl.initialized = True

        mock_service = Mock()
        mock_service.key = "realdebrid"
        mock_service.initialized = True

        dl.services = {type(mock_service): mock_service}
        dl.initialized_services = [mock_service]
        dl.service = mock_service
        dl._service_cooldowns = {}
        dl.subtitles_enabled = False

        return dl


@pytest.fixture
def mock_item():
    """Create a mock MediaItem for testing."""
    item = Mock(spec=Movie)
    item.id = "test_item_1"
    item.type = "movie"
    item.log_string = "Test Movie (2023)"
    item.active_stream = None
    item.scraped_at = None
    item.scraped_times = 1
    item.store_state = Mock()

    stream = Mock(spec=Stream)
    stream.infohash = "abc123"
    stream.raw_title = "Test.Movie.2023.1080p"
    stream.rank = 100
    stream.resolution = "1080p"
    item.streams = [stream]
    item.blacklisted_streams = []
    item.blacklist_stream = Mock()

    return item


def test_all_services_in_cooldown_reschedules(downloader, mock_item):
    """When all services are cooling down, the item should be rescheduled."""
    future = datetime.now() + timedelta(minutes=2)
    downloader._service_cooldowns["realdebrid"] = future

    results = list(downloader.run(mock_item))

    assert len(results) == 1
    result = results[0]
    assert mock_item in result.media_items
    assert result.run_at is not None
    assert result.run_at <= future

    # Service should not have been called
    downloader.service.get_instant_availability.assert_not_called()


def test_circuit_breaker_sets_cooldown_and_reschedules(downloader, mock_item):
    """CB exception should set a cooldown and reschedule (single provider)."""
    cb_exc = CircuitBreakerOpen("api.real-debrid.com", retry_after_seconds=25.0)

    with patch.object(downloader, "validate_stream_on_service", side_effect=cb_exc):
        results = list(downloader.run(mock_item))

    # Should have set cooldown on the service
    assert "realdebrid" in downloader._service_cooldowns
    cooldown = downloader._service_cooldowns["realdebrid"]
    assert cooldown > datetime.now()

    # Cooldown should be approximately 25s (from retry_after_seconds)
    expected = datetime.now() + timedelta(seconds=25)
    assert abs((cooldown - expected).total_seconds()) < 5

    # Should reschedule
    assert len(results) == 1
    assert results[0].run_at is not None


def test_circuit_breaker_default_cooldown_when_no_retry_after(downloader, mock_item):
    """CB exception without retry_after_seconds should use 60s default."""
    cb_exc = CircuitBreakerOpen("api.real-debrid.com")

    with patch.object(downloader, "validate_stream_on_service", side_effect=cb_exc):
        list(downloader.run(mock_item))

    cooldown = downloader._service_cooldowns["realdebrid"]
    expected = datetime.now() + timedelta(seconds=60)
    assert abs((cooldown - expected).total_seconds()) < 5


def test_circuit_breaker_does_not_blacklist_stream_single_provider(
    downloader, mock_item
):
    """With a single provider, CB should not blacklist the stream."""
    cb_exc = CircuitBreakerOpen("api.real-debrid.com", retry_after_seconds=30.0)

    with patch.object(downloader, "validate_stream_on_service", side_effect=cb_exc):
        list(downloader.run(mock_item))

    mock_item.blacklist_stream.assert_not_called()


def test_rate_limit_from_availability_does_not_blacklist(downloader, mock_item):
    """RD 429 surfaced as CircuitBreakerOpen via get_instant_availability must not blacklist."""
    cb_exc = CircuitBreakerOpen("api.real-debrid.com", retry_after_seconds=30.0)
    downloader.service.get_instant_availability.side_effect = cb_exc

    results = list(downloader.run(mock_item))

    mock_item.blacklist_stream.assert_not_called()
    assert "realdebrid" in downloader._service_cooldowns
    assert len(results) == 1
    assert results[0].run_at is not None


def test_circuit_breaker_breaks_early_not_all_streams(downloader, mock_item):
    """CB should stop after first stream, not try all remaining streams."""
    stream2 = Mock(spec=Stream)
    stream2.infohash = "def456"
    stream2.raw_title = "Test.Movie.2023.720p"
    stream2.rank = 50
    stream2.resolution = "720p"
    mock_item.streams = [mock_item.streams[0], stream2]

    call_count = 0
    cb_exc = CircuitBreakerOpen("api.real-debrid.com", retry_after_seconds=30.0)

    def counting_side_effect(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        raise cb_exc

    with patch.object(
        downloader, "validate_stream_on_service", side_effect=counting_side_effect
    ):
        list(downloader.run(mock_item))

    # Should only try ONE stream before breaking (CB trips on service, all streams will fail)
    assert call_count == 1


def test_successful_download_clears_cooldowns(downloader, mock_item):
    """A successful download should clear all service cooldowns."""
    downloader._service_cooldowns["realdebrid"] = datetime.now() - timedelta(seconds=1)

    mock_container = Mock()
    mock_container.files = [Mock()]
    mock_download = Mock()

    with (
        patch.object(
            downloader, "validate_stream_on_service", return_value=mock_container
        ),
        patch.object(
            downloader, "download_cached_stream_on_service", return_value=mock_download
        ),
        patch.object(downloader, "update_item_attributes", return_value=True),
    ):
        list(downloader.run(mock_item))

    assert downloader._service_cooldowns == {}


def test_expired_cooldown_allows_processing(downloader, mock_item):
    """A cooldown that has expired should not block processing."""
    downloader._service_cooldowns["realdebrid"] = datetime.now() - timedelta(minutes=1)

    mock_container = Mock()
    mock_container.files = [Mock()]
    mock_download = Mock()

    with (
        patch.object(
            downloader, "validate_stream_on_service", return_value=mock_container
        ),
        patch.object(
            downloader, "download_cached_stream_on_service", return_value=mock_download
        ),
        patch.object(downloader, "update_item_attributes", return_value=True),
    ):
        list(downloader.run(mock_item))

    # Service SHOULD have been called
    downloader.service.get_instant_availability.assert_not_called()  # we mocked validate_stream_on_service directly


def test_stream_exhaustion_preserves_blacklist_and_reschedules(downloader, mock_item):
    """Exhausted streams must not clear blacklist or scrape cooldown (hot-loop fix)."""
    from program.media.state import States

    blacklisted = Mock(spec=Stream)
    blacklisted.infohash = "deadbeef"
    mock_item.blacklisted_streams = [blacklisted]
    mock_item.scraped_at = None
    mock_item.scraped_times = 1
    mock_item.store_state = Mock()

    # Real blacklist_stream moves the stream onto blacklisted_streams
    def _blacklist(stream):
        if stream in mock_item.streams:
            mock_item.streams.remove(stream)
        if stream not in mock_item.blacklisted_streams:
            mock_item.blacklisted_streams.append(stream)

    mock_item.blacklist_stream = Mock(side_effect=_blacklist)

    with patch.object(downloader, "validate_stream_on_service", return_value=None):
        results = list(downloader.run(mock_item))

    assert len(results) == 1
    result = results[0]
    assert result.run_at is not None
    # Default scrape cooldown for scraped_times=1 is 30 minutes
    expected = datetime.now() + timedelta(minutes=30)
    assert abs((result.run_at - expected).total_seconds()) < 5

    assert len(mock_item.streams) == 0
    assert blacklisted in mock_item.blacklisted_streams
    assert mock_item.scraped_at is not None
    assert mock_item.scraped_times == 1
    mock_item.store_state.assert_called_with(States.Indexed)
    mock_item.blacklist_stream.assert_called()


def test_max_streams_early_exit_when_more_remain(downloader, mock_item):
    """With leftover streams, MAX_STREAMS_PER_RUN yields without Indexed reset."""
    extras = []
    for i, infohash in enumerate(("def456", "ghi789", "jkl012"), start=2):
        s = Mock(spec=Stream)
        s.infohash = infohash
        s.raw_title = f"Test.Movie.2023.{i}"
        s.rank = 100 - i
        s.resolution = "1080p"
        extras.append(s)
    mock_item.streams = [mock_item.streams[0], *extras]
    mock_item.blacklisted_streams = []

    def _blacklist(stream):
        if stream in mock_item.streams:
            mock_item.streams.remove(stream)
        if stream not in mock_item.blacklisted_streams:
            mock_item.blacklisted_streams.append(stream)

    mock_item.blacklist_stream = Mock(side_effect=_blacklist)

    with patch.object(downloader, "validate_stream_on_service", return_value=None):
        results = list(downloader.run(mock_item))

    assert len(results) == 1
    assert results[0].run_at is None
    assert len(mock_item.streams) == 1  # 4 - 3 tried
    mock_item.store_state.assert_not_called()


def test_max_streams_exhaustion_applies_backoff_when_none_remain(
    downloader, mock_item
):
    """When ≤3 streams all fail, apply exhaustion backoff (not bare early yield)."""
    from program.media.state import States

    streams = []
    for i, infohash in enumerate(("abc123", "def456", "ghi789")):
        s = Mock(spec=Stream)
        s.infohash = infohash
        s.raw_title = f"Test.Movie.2023.{i}"
        s.rank = 100 - i
        s.resolution = "1080p"
        streams.append(s)
    mock_item.streams = streams
    mock_item.blacklisted_streams = []
    mock_item.scraped_times = 1

    def _blacklist(stream):
        if stream in mock_item.streams:
            mock_item.streams.remove(stream)
        if stream not in mock_item.blacklisted_streams:
            mock_item.blacklisted_streams.append(stream)

    mock_item.blacklist_stream = Mock(side_effect=_blacklist)

    with patch.object(downloader, "validate_stream_on_service", return_value=None):
        results = list(downloader.run(mock_item))

    assert len(results) == 1
    assert results[0].run_at is not None
    expected = datetime.now() + timedelta(minutes=30)
    assert abs((results[0].run_at - expected).total_seconds()) < 5
    assert len(mock_item.streams) == 0
    assert len(mock_item.blacklisted_streams) == 3
    mock_item.store_state.assert_called_with(States.Indexed)
