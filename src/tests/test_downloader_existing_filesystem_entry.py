"""Regression: existing filesystem_entry must be noop success, not a blacklist."""

from unittest.mock import Mock, PropertyMock, patch

import pytest
from loguru import logger
from RTN import ParsedData

from program.media.item import Episode, Movie, Season, Show
from program.media.state import States
from program.services.downloaders import Downloader
from program.services.downloaders.models import (
    DebridFile,
    DownloadedTorrent,
    NoMatchingFilesException,
    TorrentContainer,
    TorrentInfo,
)

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


def _black_torch_episode() -> tuple[Show, Episode]:
    show = Show(
        {
            "imdb_id": "tt9467314",
            "requested_by": "user",
            "title": "BLACK TORCH",
        }
    )
    season = Season({"number": 1})
    episode = Episode({"number": 4})
    season.add_episode(episode)
    show.add_season(season)
    episode.parent = season
    season.parent = show
    return show, episode


def _file_data() -> ParsedData:
    return ParsedData(
        raw_title="BLACK.TORCH.S01E04.1080p.mkv",
        parsed_title="BLACK TORCH",
        seasons=[1],
        episodes=[4],
    )


def _debrid_file() -> DebridFile:
    return DebridFile(
        file_id=1,
        filename="BLACK.TORCH.S01E04.1080p.mkv",
        filesize=1_500_000_000,
        download_url="https://example.com/file",
    )


def _download_result(files: list[DebridFile] | None = None) -> DownloadedTorrent:
    container_files = files if files is not None else [_debrid_file()]
    return DownloadedTorrent(
        id=1,
        infohash="abc123deadbeef",
        container=TorrentContainer(infohash="abc123deadbeef", files=container_files),
        info=TorrentInfo(id=1, name="BLACK.TORCH.S01E04.mkv"),
    )


def test_match_file_to_item_existing_filesystem_entry_is_success(downloader):
    """Episode that already has filesystem_entry must return True (noop), not False."""
    show, episode = _black_torch_episode()
    download_result = _download_result()

    with (
        patch.object(
            Episode,
            "filesystem_entry",
            new_callable=PropertyMock,
            return_value=Mock(name="existing_entry"),
        ),
        patch.object(downloader, "_update_attributes") as update_attrs,
    ):
        matched = downloader.match_file_to_item(
            item=episode,
            file_data=_file_data(),
            file=_debrid_file(),
            download_result=download_result,
            show=show,
            service=downloader.service,
        )

    assert matched is True
    update_attrs.assert_not_called()


def test_match_file_to_item_without_filesystem_entry_updates(downloader):
    """Episode without filesystem_entry still gets attributes updated."""
    show, episode = _black_torch_episode()
    download_result = _download_result()

    with (
        patch.object(
            Episode,
            "filesystem_entry",
            new_callable=PropertyMock,
            return_value=None,
        ),
        patch.object(downloader, "_update_attributes") as update_attrs,
    ):
        matched = downloader.match_file_to_item(
            item=episode,
            file_data=_file_data(),
            file=_debrid_file(),
            download_result=download_result,
            show=show,
            service=downloader.service,
        )

    assert matched is True
    update_attrs.assert_called_once()


def test_update_item_attributes_existing_entry_is_success(downloader):
    """
    When the only matching episode already has filesystem_entry, update_item_attributes
    must return True so run() does not raise NoMatchingFilesException / blacklist.
    """
    _show, episode = _black_torch_episode()
    download_result = _download_result()

    with (
        patch.object(
            Episode,
            "filesystem_entry",
            new_callable=PropertyMock,
            return_value=Mock(name="existing_entry"),
        ),
        patch(
            "program.services.downloaders.parse_filename",
            return_value=_file_data(),
        ),
        patch.object(downloader, "_update_attributes") as update_attrs,
    ):
        result = downloader.update_item_attributes(
            episode, download_result, downloader.service
        )

    assert result is True
    update_attrs.assert_not_called()


def test_run_does_not_blacklist_when_attributes_match(downloader):
    """
    run() must not blacklist when update_item_attributes succeeds (including
    filesystem_entry noop). Uses a Movie mock to avoid SQLAlchemy stream relations.
    """
    item = Mock(spec=Movie)
    item.id = "ep_s01e04"
    item.type = "episode"
    item.log_string = "BLACK TORCH S01E04"
    item.active_stream = None
    item.scraped_at = None
    item.scraped_times = 1
    item.store_state = Mock()

    stream = Mock()
    stream.infohash = "abc123deadbeef"
    stream.raw_title = "BLACK.TORCH.S01E04.1080p"
    stream.rank = 100
    stream.resolution = "1080p"
    item.streams = [stream]
    item.blacklisted_streams = []
    item.blacklist_stream = Mock()

    download_result = _download_result()

    with (
        patch.object(
            downloader,
            "validate_stream_on_service",
            return_value=download_result.container,
        ),
        patch.object(
            downloader,
            "download_cached_stream_on_service",
            return_value=download_result,
        ),
        # Simulate the fixed path: existing filesystem_entry → True
        patch.object(downloader, "update_item_attributes", return_value=True),
        patch(
            "program.services.downloaders.sort_streams_by_quality",
            return_value=[stream],
        ),
    ):
        results = list(downloader.run(item))

    item.blacklist_stream.assert_not_called()
    assert len(results) == 1
    assert results[0].media_items == [item]
    assert getattr(results[0], "run_at", None) is None


def test_run_raises_no_matching_files_when_attributes_fail(downloader):
    """Genuine no-match still raises NoMatchingFilesException → blacklist path."""
    item = Mock(spec=Movie)
    item.id = "ep_s01e04"
    item.type = "episode"
    item.log_string = "BLACK TORCH S01E04"
    item.active_stream = None
    item.scraped_at = None
    item.scraped_times = 1
    item.store_state = Mock()

    stream = Mock()
    stream.infohash = "abc123deadbeef"
    stream.raw_title = "BLACK.TORCH.S01E04.1080p"
    stream.rank = 100
    stream.resolution = "1080p"
    item.streams = [stream]
    item.blacklisted_streams = []
    item.blacklist_stream = Mock()

    download_result = _download_result()

    with (
        patch.object(
            downloader,
            "validate_stream_on_service",
            return_value=download_result.container,
        ),
        patch.object(
            downloader,
            "download_cached_stream_on_service",
            return_value=download_result,
        ),
        patch.object(downloader, "update_item_attributes", return_value=False),
        patch(
            "program.services.downloaders.sort_streams_by_quality",
            return_value=[stream],
        ),
    ):
        list(downloader.run(item))

    item.blacklist_stream.assert_called_once_with(stream)


def test_update_item_attributes_no_episode_match_returns_false(downloader):
    """Parsed file that resolves to no show episode must return False."""
    _show, episode = _black_torch_episode()
    wrong_file = DebridFile(
        file_id=1,
        filename="BLACK.TORCH.S99E99.mkv",
        filesize=1_500_000_000,
        download_url="https://example.com/file",
    )
    download_result = _download_result(files=[wrong_file])
    wrong_parsed = ParsedData(
        raw_title="BLACK.TORCH.S99E99.mkv",
        parsed_title="BLACK TORCH",
        seasons=[99],
        episodes=[99],
    )

    with (
        patch.object(
            Episode,
            "filesystem_entry",
            new_callable=PropertyMock,
            return_value=None,
        ),
        patch(
            "program.services.downloaders.parse_filename",
            return_value=wrong_parsed,
        ),
        patch.object(downloader, "_update_attributes") as update_attrs,
    ):
        result = downloader.update_item_attributes(
            episode, download_result, downloader.service
        )

    assert result is False
    update_attrs.assert_not_called()

    with pytest.raises(NoMatchingFilesException):
        if not result:
            raise NoMatchingFilesException(
                f"No valid files found for {episode.log_string}"
            )


def test_completed_state_without_entry_still_skips_update(downloader):
    """Completed without filesystem_entry remains a non-update skip (pre-existing)."""
    show, episode = _black_torch_episode()
    download_result = _download_result()

    with (
        patch.object(
            Episode,
            "filesystem_entry",
            new_callable=PropertyMock,
            return_value=None,
        ),
        patch.object(
            Episode,
            "state",
            new_callable=PropertyMock,
            return_value=States.Completed,
        ),
        patch.object(downloader, "_update_attributes") as update_attrs,
    ):
        matched = downloader.match_file_to_item(
            item=episode,
            file_data=_file_data(),
            file=_debrid_file(),
            download_result=download_result,
            show=show,
            service=downloader.service,
        )

    assert matched is False
    update_attrs.assert_not_called()
