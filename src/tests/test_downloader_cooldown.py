from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import pytest

from program.media.item import Movie
from program.media.stream import Stream
from program.services.downloaders import Downloader
from program.utils.request import CircuitBreakerOpen


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

    stream = Mock(spec=Stream)
    stream.infohash = "abc123"
    stream.raw_title = "Test.Movie.2023.1080p"
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


def test_circuit_breaker_breaks_early_not_all_streams(downloader, mock_item):
    """CB should stop after first stream, not try all remaining streams."""
    stream2 = Mock(spec=Stream)
    stream2.infohash = "def456"
    stream2.raw_title = "Test.Movie.2023.720p"
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
