"""Tests for stream validation outcome classification and false-positive prevention."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

from program.media.models import ActiveStream
from program.media.state import States
from program.services.downloaders import Downloader
from program.services.downloaders.models import (
    DebridFile,
    DownloadedTorrent,
    NoMatchingFilesException,
    TorrentContainer,
    TorrentContentMismatchException,
    TorrentInfo,
)
from program.services.downloaders.shared import (
    DebridInfringingError,
    TorrentDeadOrCorruptError,
)
from program.utils.request import CircuitBreakerOpen


@pytest.fixture
def mock_downloader():
    """Create a Downloader instance with mocked services."""
    with patch.object(Downloader, "__init__", lambda *_: None):
        dl = Downloader()
        dl.initialized = True

        service1 = Mock()
        service1.key = "realdebrid"
        service1.initialized = True

        dl.services = {type(service1): service1}
        dl.initialized_services = [service1]
        dl.service = service1
        dl._service_cooldowns = {}
        dl.subtitles_enabled = False

        return dl


class FakeStream:
    def __init__(self, infohash: str = "deadbeef1234", rank: int = 100):
        self.id = 1
        self.infohash = infohash
        self.raw_title = f"Test.Movie.2024.{infohash[:6]}.1080p.mkv"
        self.rank = rank
        self.resolution = "1080p"

    def __hash__(self) -> int:
        return hash(self.infohash)

    def __eq__(self, other: object) -> bool:
        return getattr(other, "infohash", None) == self.infohash


def _make_stream(infohash: str = "deadbeef1234", rank: int = 100) -> FakeStream:
    return FakeStream(infohash, rank)


def _make_movie() -> SimpleNamespace:
    return SimpleNamespace(
        id=123,
        imdb_id="tt1234567",
        title="Test Movie",
        year=2024,
        log_string="Movie Test",
        type="movie",
        parent=None,
        streams=[],
        blacklisted_streams=[],
        active_stream=None,
        filesystem_entries=[],
        subtitles=[],
        scraped_at=None,
        scraped_times=0,
        failed_attempts=0,
        store_state=Mock(),
        get_stream_owner=lambda: None,
    )


def test_uncached_stream_is_not_blacklisted(mock_downloader):
    """Uncached torrents (validate returns None) must be non-permanent and not blacklisted."""
    movie = _make_movie()
    stream = _make_stream("uncached111")
    movie.streams = [stream]
    movie.blacklist_stream = Mock()

    with patch.object(mock_downloader, "validate_stream_on_service", return_value=None):
        results = list(mock_downloader.run(movie))

    # Stream should be removed from active evaluation list
    assert stream not in movie.streams
    # Stream must NOT be blacklisted
    mock_downloader.service.delete_torrent.assert_not_called()
    movie.blacklist_stream.assert_not_called()
    assert stream not in movie.blacklisted_streams


def test_no_matching_files_is_not_blacklisted(mock_downloader):
    """Torrents with NoMatchingFilesException must not be blacklisted."""
    movie = _make_movie()
    stream = _make_stream("nomatch222")
    movie.streams = [stream]
    movie.blacklist_stream = Mock()

    download_result = DownloadedTorrent(
        id=1,
        infohash=stream.infohash,
        info=TorrentInfo(id=1, name="Some.Other.Movie.mkv"),
        container=TorrentContainer(
            infohash=stream.infohash,
            files=[
                DebridFile(
                    file_id=1,
                    filename="Some.Other.Movie.mkv",
                    filesize=1_000_000_000,
                    download_url="https://example.com/other",
                )
            ],
        ),
    )

    with (
        patch.object(
            mock_downloader,
            "validate_stream_on_service",
            return_value=download_result.container,
        ),
        patch.object(
            mock_downloader,
            "download_cached_stream_on_service",
            return_value=download_result,
        ),
        patch.object(mock_downloader, "update_item_attributes", return_value=False),
        patch(
            "program.services.downloaders.sort_streams_by_quality",
            return_value=[stream],
        ),
    ):
        results = list(mock_downloader.run(movie))

    assert stream not in movie.streams
    movie.blacklist_stream.assert_not_called()
    assert stream not in movie.blacklisted_streams


def test_dead_or_corrupt_torrent_is_blacklisted(mock_downloader):
    """Dead or corrupt torrent error must trigger permanent blacklisting."""
    movie = _make_movie()
    stream = _make_stream("dead333")
    movie.streams = [stream]

    def _blacklist(s):
        if s in movie.streams:
            movie.streams.remove(s)
        if s not in movie.blacklisted_streams:
            movie.blacklisted_streams.append(s)

    movie.blacklist_stream = Mock(side_effect=_blacklist)

    with patch.object(
        mock_downloader,
        "validate_stream_on_service",
        side_effect=TorrentDeadOrCorruptError("Torrent is dead (0 seeders)"),
    ):
        results = list(mock_downloader.run(movie))

    movie.blacklist_stream.assert_called_once_with(stream)
    assert stream in movie.blacklisted_streams
    assert stream not in movie.streams


def test_content_mismatch_is_permanent_bad_candidate(mock_downloader):
    """A cached torrent serving another movie is not valid for this media item."""
    movie = _make_movie()
    stream = _make_stream("wrongcontent555")
    movie.streams = [stream]
    movie.blacklist_stream = Mock()

    container = SimpleNamespace(files=[])
    download_result = SimpleNamespace(id="torrent-1", container=container)
    with (
        patch.object(
            mock_downloader,
            "validate_stream_on_service",
            return_value=container,
        ),
        patch.object(
            mock_downloader,
            "download_cached_stream_on_service",
            return_value=download_result,
        ),
        patch.object(
            mock_downloader,
            "update_item_attributes",
            side_effect=TorrentContentMismatchException("wrong movie"),
        ),
        patch(
            "program.services.downloaders.sort_streams_by_quality",
            return_value=[stream],
        ),
    ):
        list(mock_downloader.run(movie))

    movie.blacklist_stream.assert_called_once_with(stream)


def test_infringing_torrent_is_not_blacklisted(mock_downloader):
    """Infringing or DMCA removed content is provider-specific and must NOT trigger permanent blacklisting."""
    movie = _make_movie()
    stream = _make_stream("dmca444")
    movie.streams = [stream]
    movie.blacklist_stream = Mock()

    with patch.object(
        mock_downloader,
        "validate_stream_on_service",
        side_effect=DebridInfringingError("Infringing file removed"),
    ):
        results = list(mock_downloader.run(movie))

    movie.blacklist_stream.assert_not_called()
    assert stream not in movie.blacklisted_streams


def test_single_provider_infringing_torrent_excluded_locally_without_mutating_relationship(
    mock_downloader,
):
    """When single provider is initialized and returns DebridInfringingError, exclude locally without DB blacklist."""
    movie = _make_movie()
    stream = _make_stream("dmcaSingle444")
    movie.streams = [stream]
    movie.blacklist_stream = Mock()

    with patch.object(
        mock_downloader,
        "validate_stream_on_service",
        side_effect=DebridInfringingError("Real-Debrid 451 Infringing Torrent"),
    ):
        results = list(mock_downloader.run(movie))

    movie.blacklist_stream.assert_not_called()
    assert stream not in movie.blacklisted_streams


@pytest.mark.parametrize(
    "should_submit,analysis_result",
    [(False, False), (True, False)],
)
def test_successful_candidate_is_not_overwritten_when_analysis_skips_or_fails(
    mock_downloader, should_submit, analysis_result
):
    movie = _make_movie()
    first = _make_stream("subtitlefirst111")
    second = _make_stream("subtitlesecond222")
    movie.streams = [first, second]
    mock_downloader.subtitles_enabled = True
    container = SimpleNamespace(files=[])
    download = SimpleNamespace(id="download", container=container)
    with (
        patch.object(
            mock_downloader, "validate_stream_on_service", return_value=container
        ) as validate,
        patch.object(
            mock_downloader, "download_cached_stream_on_service", return_value=download
        ),
        patch.object(mock_downloader, "update_item_attributes", return_value=True),
        patch(
            "program.services.downloaders.sort_streams_by_quality",
            return_value=[first, second],
        ),
        patch(
            "program.services.media_analysis.media_analysis_service.should_submit",
            return_value=should_submit,
        ),
        patch(
            "program.services.media_analysis.media_analysis_service.run",
            return_value=analysis_result,
        ) as analyze,
    ):
        list(mock_downloader.run(movie))

    assert validate.call_count == 1
    assert validate.call_args.args[0] is first
    if should_submit:
        analyze.assert_called_once_with(movie)
    else:
        analyze.assert_not_called()


def test_multi_service_circuit_breaker_fails_over_to_healthy_provider():
    with patch.object(Downloader, "__init__", lambda *_: None):
        dl = Downloader()
        dl.initialized = True
        blocked = Mock()
        blocked.key = "realdebrid"
        blocked.initialized = True
        healthy = Mock()
        healthy.key = "alldebrid"
        healthy.initialized = True
        dl.services = {type(blocked): blocked, type(healthy): healthy}
        dl.initialized_services = [blocked, healthy]
        dl.service = blocked
        dl._service_cooldowns = {}
        dl._excluded_stream_hashes = {}
        dl.subtitles_enabled = False

        movie = _make_movie()
        stream = _make_stream("failover888")
        movie.streams = [stream]
        movie.blacklist_stream = Mock()
        container = SimpleNamespace(files=[])
        download = SimpleNamespace(id="ok", container=container)

        def validate(_stream, _item, service):
            if service.key == "realdebrid":
                raise CircuitBreakerOpen("rd", retry_after_seconds=60)
            return container

        with (
            patch.object(
                dl, "validate_stream_on_service", side_effect=validate
            ) as validate_mock,
            patch.object(
                dl, "download_cached_stream_on_service", return_value=download
            ),
            patch.object(dl, "update_item_attributes", return_value=True),
            patch(
                "program.services.downloaders.sort_streams_by_quality",
                return_value=[stream],
            ),
        ):
            list(dl.run(movie))

        assert validate_mock.call_count == 2
        assert validate_mock.call_args_list[0].args[2] is blocked
        assert validate_mock.call_args_list[1].args[2] is healthy
        movie.blacklist_stream.assert_not_called()


def test_multi_service_requires_all_services_permanent_rejection():
    """Stream must NOT be blacklisted unless ALL available services confirm permanent failure."""
    with patch.object(Downloader, "__init__", lambda *_: None):
        dl = Downloader()
        dl.initialized = True

        service1 = Mock()
        service1.key = "realdebrid"
        service1.initialized = True

        service2 = Mock()
        service2.key = "alldebrid"
        service2.initialized = True

        dl.services = {type(service1): service1, type(service2): service2}
        dl.initialized_services = [service1, service2]
        dl.service = service1
        dl._service_cooldowns = {}
        dl.subtitles_enabled = False

        movie = _make_movie()
        stream = _make_stream("partial555")
        movie.streams = [stream]
        movie.blacklist_stream = Mock()

        # Service 1: permanent error; Service 2: uncached (returns None)
        def mock_validate(s, item, srv):
            if srv.key == "realdebrid":
                raise TorrentDeadOrCorruptError("dead on RD")
            # uncached on AD implicitly returns None

        with patch.object(dl, "validate_stream_on_service", side_effect=mock_validate):
            list(dl.run(movie))

        # Because AD only reported uncached (non-permanent), stream must NOT be blacklisted
        movie.blacklist_stream.assert_not_called()
        assert stream not in movie.streams


def test_multi_service_blacklists_when_both_confirm_permanent():
    """When all services report permanent errors (dead/corrupt), stream is blacklisted."""
    with patch.object(Downloader, "__init__", lambda *_: None):
        dl = Downloader()
        dl.initialized = True

        service1 = Mock()
        service1.key = "realdebrid"
        service1.initialized = True

        service2 = Mock()
        service2.key = "alldebrid"
        service2.initialized = True

        dl.services = {type(service1): service1, type(service2): service2}
        dl.initialized_services = [service1, service2]
        dl.service = service1
        dl._service_cooldowns = {}
        dl.subtitles_enabled = False

        movie = _make_movie()
        stream = _make_stream("bothdead666")
        movie.streams = [stream]

        def _blacklist(s):
            if s in movie.streams:
                movie.streams.remove(s)
            if s not in movie.blacklisted_streams:
                movie.blacklisted_streams.append(s)

        movie.blacklist_stream = Mock(side_effect=_blacklist)

        def mock_validate(s, item, srv):
            if srv.key == "realdebrid":
                raise TorrentDeadOrCorruptError("dead on RD")
            raise TorrentDeadOrCorruptError("dead on AD")

        with patch.object(dl, "validate_stream_on_service", side_effect=mock_validate):
            list(dl.run(movie))

        movie.blacklist_stream.assert_called_once_with(stream)
        assert stream in movie.blacklisted_streams


def test_multi_service_mixed_infringing_and_dead_does_not_blacklist():
    """When one service reports dead and another reports infringing, do NOT blacklist globally."""
    with patch.object(Downloader, "__init__", lambda *_: None):
        dl = Downloader()
        dl.initialized = True

        service1 = Mock()
        service1.key = "realdebrid"
        service1.initialized = True

        service2 = Mock()
        service2.key = "alldebrid"
        service2.initialized = True

        dl.services = {type(service1): service1, type(service2): service2}
        dl.initialized_services = [service1, service2]
        dl.service = service1
        dl._service_cooldowns = {}
        dl.subtitles_enabled = False

        movie = _make_movie()
        stream = _make_stream("mixed777")
        movie.streams = [stream]
        movie.blacklist_stream = Mock()

        def mock_validate(s, item, srv):
            if srv.key == "realdebrid":
                raise TorrentDeadOrCorruptError("dead on RD")
            raise DebridInfringingError("DMCA on AD")

        with patch.object(dl, "validate_stream_on_service", side_effect=mock_validate):
            list(dl.run(movie))

        movie.blacklist_stream.assert_not_called()
        assert stream not in movie.blacklisted_streams
