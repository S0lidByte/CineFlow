"""Tests for matching debrid files to media items."""

from unittest.mock import Mock, patch

import pytest
from RTN import ParsedData

from program.media.item import Episode, Movie, Season, Show
from program.services.downloaders import Downloader
from program.services.downloaders.models import (
    DebridFile,
    DownloadedTorrent,
    TorrentContainer,
    TorrentInfo,
)


@pytest.fixture
def downloader():
    """Create a downloader with a mocked service and no initialization side effects."""
    with patch.object(Downloader, "__init__", lambda *_: None):
        instance = Downloader()
        instance.service = Mock(name="service")
        instance._service_cooldowns = {}
        return instance


def _download_result(filename: str = "file.mkv") -> tuple[DebridFile, DownloadedTorrent]:
    file = DebridFile(
        file_id=1,
        filename=filename,
        filesize=800_000_000,
        download_url="https://example.com/file",
    )
    result = DownloadedTorrent(
        id=1,
        infohash="abc123",
        container=TorrentContainer(infohash="abc123", files=[file]),
        info=TorrentInfo(id=1, name=filename),
    )
    return file, result


def _parsed(filename: str, *, seasons: list[int], episodes: list[int], type: str) -> ParsedData:
    return ParsedData(
        raw_title=filename,
        parsed_title="title",
        seasons=seasons,
        episodes=episodes,
        type=type,
    )


def _show_with_episodes(*numbers: int) -> tuple[Show, Season]:
    show = Show({"imdb_id": "tt1405406", "requested_by": "user", "title": "Test Show"})
    season = Season({"number": 1})
    for number in numbers:
        season.add_episode(Episode({"number": number}))
    show.add_season(season)
    return show, season


def test_match_file_to_item_movie(downloader):
    file, result = _download_result("Inception.mkv")
    item = Movie({"imdb_id": "tt1375666", "requested_by": "user", "title": "Inception"})

    with patch.object(downloader, "_update_attributes") as update:
        matched = downloader.match_file_to_item(
            item, _parsed(file.filename, seasons=[], episodes=[], type="movie"), file, result
        )

    assert matched is True
    update.assert_called_once()


def test_match_file_to_item_episode(downloader):
    show, season = _show_with_episodes(1)
    episode = season.episodes[0]
    file, result = _download_result("Test.Show.S01E01.mkv")

    with patch.object(downloader, "_update_attributes"):
        matched = downloader.match_file_to_item(
            episode, _parsed(file.filename, seasons=[1], episodes=[1], type="episode"),
            file, result, show=show,
        )

    assert matched is True


def test_match_file_to_item_season_matches_all_files(downloader):
    show, season = _show_with_episodes(1, 2)
    file, result = _download_result("Test.Show.S01E01-E02.mkv")

    with patch.object(downloader, "_update_attributes"):
        matched = downloader.match_file_to_item(
            season, _parsed(file.filename, seasons=[1], episodes=[1, 2], type="episode"),
            file, result, show=show,
        )

    assert matched is True
    assert season.active_stream is not None


def test_match_file_to_item_partial_season_is_match(downloader):
    show, season = _show_with_episodes(1, 2)
    file, result = _download_result("Test.Show.S01E01.mkv")

    with patch.object(downloader, "_update_attributes"):
        matched = downloader.match_file_to_item(
            season, _parsed(file.filename, seasons=[1], episodes=[1], type="episode"),
            file, result, show=show,
        )

    assert matched is True


def test_update_item_attributes_returns_false_for_empty_container(downloader):
    item = Movie({"imdb_id": "tt1375666", "requested_by": "user", "title": "Inception"})
    result = DownloadedTorrent(
        id=1, infohash="abc123", container=TorrentContainer(infohash="abc123"),
        info=TorrentInfo(id=1, name="Inception.mkv"),
    )

    # The current production contract represents only selected provider files
    # in `TorrentContainer.files`; an empty container has no match.
    assert downloader.update_item_attributes(item, result) is False


def test_update_item_attributes_returns_false_for_no_selected_files(downloader):
    file, result = _download_result("movie.mkv")
    item = Movie({"imdb_id": "tt1375666", "requested_by": "user", "title": "Inception"})
    result.container.files = []

    assert downloader.update_item_attributes(item, result) is False
