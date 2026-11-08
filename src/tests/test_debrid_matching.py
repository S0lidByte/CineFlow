"""Tests for matching debrid files to media items."""

from unittest.mock import Mock, patch

import pytest
from RTN import ParsedData, Torrent

from program.media.item import Episode, Movie, Season, Show
from program.media.stream import Stream
from program.services.downloaders import Downloader
from program.services.downloaders.models import (
    DebridFile,
    DownloadedTorrent,
    TorrentContainer,
    TorrentContentMismatchException,
    TorrentInfo,
)
from program.services.downloaders.shared import parse_filename


@pytest.fixture
def downloader():
    """Create a downloader with a mocked service and no initialization side effects."""
    with patch.object(Downloader, "__init__", lambda *_: None):
        instance = Downloader()
        instance.service = Mock(name="service")
        instance._service_cooldowns = {}
        return instance


def _download_result(
    filename: str = "file.mkv",
) -> tuple[DebridFile, DownloadedTorrent]:
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


def _parsed(
    filename: str, *, seasons: list[int], episodes: list[int], type: str
) -> ParsedData:
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
            item,
            _parsed(file.filename, seasons=[], episodes=[], type="movie"),
            file,
            result,
        )

    assert matched is True
    update.assert_called_once()


def test_match_file_to_item_episode(downloader):
    show, season = _show_with_episodes(1)
    episode = season.episodes[0]
    file, result = _download_result("Test.Show.S01E01.mkv")

    with patch.object(downloader, "_update_attributes"):
        matched = downloader.match_file_to_item(
            episode,
            _parsed(file.filename, seasons=[1], episodes=[1], type="episode"),
            file,
            result,
            show=show,
        )

    assert matched is True


def test_update_item_attributes_rejects_wrong_movie_sequel_and_year(downloader):
    item = Movie(
        {
            "imdb_id": "tt1119646",
            "requested_by": "user",
            "title": "The Hangover",
            "year": 2009,
        }
    )
    wrong_file, result = _download_result("The Hangover 3 (2013) 720p BluRay x265.mkv")
    result.container.files = [wrong_file]

    with pytest.raises(TorrentContentMismatchException):
        downloader.update_item_attributes(item, result)
    assert item.filesystem_entries == []


def test_update_item_attributes_accepts_matching_movie_file(downloader):
    item = Movie(
        {
            "imdb_id": "tt1119646",
            "requested_by": "user",
            "title": "The Hangover",
            "year": 2009,
        }
    )
    matching_file, result = _download_result(
        "The Hangover (2009) EXTENDED 720p BluRay x265.mkv"
    )
    result.container.files = [matching_file]

    with patch.object(downloader, "_update_attributes") as update:
        assert downloader.update_item_attributes(item, result) is True

    update.assert_called_once()


def test_update_item_attributes_accepts_matching_movie_without_leading_article(
    downloader,
):
    item = Movie(
        {
            "imdb_id": "tt1119646",
            "requested_by": "user",
            "title": "The Hangover",
            "year": 2009,
            "aliases": {"xx": ["Una notte da leoni"]},
        }
    )
    matching_file, result = _download_result("Hangover 2009 1080p BluRay.mkv")
    result.container.files = [matching_file]

    with patch.object(downloader, "_update_attributes") as update:
        assert downloader.update_item_attributes(item, result) is True

    update.assert_called_once()


def test_update_item_attributes_accepts_movie_title_alias(downloader):
    item = Movie(
        {
            "imdb_id": "tt1119646",
            "requested_by": "user",
            "title": "The Hangover",
            "year": 2009,
            "aliases": {"xx": ["Una notte da leoni"]},
        }
    )
    alias_file, result = _download_result("Una notte da leoni (2009) 720p.mkv")
    result.container.files = [alias_file]

    with patch.object(downloader, "_update_attributes") as update:
        assert downloader.update_item_attributes(item, result) is True

    update.assert_called_once()


def test_update_item_attributes_accepts_release_year_within_one_year(downloader):
    item = Movie(
        {
            "imdb_id": "tt1119646",
            "requested_by": "user",
            "title": "The Hangover",
            "year": 2009,
        }
    )
    file, result = _download_result("The Hangover (2008) 720p BluRay.mkv")
    result.container.files = [file]

    with patch.object(downloader, "_update_attributes") as update:
        assert downloader.update_item_attributes(item, result) is True

    update.assert_called_once()


def test_yearless_movie_file_is_not_misclassified_as_wrong_content(downloader):
    item = Movie(
        {
            "imdb_id": "tt1119646",
            "requested_by": "user",
            "title": "The Hangover",
            "year": 2009,
        }
    )
    yearless_file, result = _download_result("The Hangover 720p BluRay.mkv")
    wrong_year_file, _ = _download_result("The Hangover (2013) 720p BluRay.mkv")
    result.container.files = [yearless_file, wrong_year_file]

    with patch.object(downloader, "_update_attributes") as update:
        assert downloader.update_item_attributes(item, result) is True

    update.assert_called_once()
    assert update.call_args.args[1] is yearless_file


def test_update_item_attributes_rejects_different_movie_title(downloader):
    item = Movie(
        {
            "imdb_id": "tt1119646",
            "requested_by": "user",
            "title": "The Hangover",
            "year": 2009,
        }
    )
    file, result = _download_result("The Terminal (2004) 720p BluRay.mkv")
    result.container.files = [file]

    with pytest.raises(TorrentContentMismatchException):
        downloader.update_item_attributes(item, result)


def test_manual_download_returns_false_for_wrong_movie_content(downloader):
    item = Movie(
        {
            "imdb_id": "tt1119646",
            "requested_by": "user",
            "title": "The Hangover",
            "year": 2009,
        }
    )
    stream = Stream(
        Torrent(
            raw_title="The Terminal (2004) 720p BluRay",
            infohash="a" * 40,
            data=parse_filename("The Terminal (2004) 720p BluRay.mkv"),
            rank=0,
            lev_ratio=1.0,
        )
    )
    wrong_file, _ = _download_result("The Terminal (2004) 720p BluRay.mkv")
    container = TorrentContainer(infohash=stream.infohash, files=[wrong_file])
    download = DownloadedTorrent(
        id=1,
        info=TorrentInfo(id=1, name="The Terminal (2004)"),
        infohash=stream.infohash,
        container=container,
    )
    downloader.validate_stream_on_service = Mock(return_value=container)
    downloader.download_cached_stream_on_service = Mock(return_value=download)
    downloader.update_item_attributes = Mock(
        side_effect=TorrentContentMismatchException("wrong movie")
    )
    item.store_state = Mock()

    assert downloader.start_manual_download(item, stream, downloader.service) is False
    item.store_state.assert_not_called()


def test_movie_pack_prefers_exact_year_when_matching_requested_title(downloader):
    item = Movie(
        {
            "imdb_id": "tt1119646",
            "requested_by": "user",
            "title": "King Kong",
            "year": 2005,
        }
    )
    remaster, result = _download_result("King Kong (2006) 1080p BluRay.mkv")
    theatrical, _ = _download_result("King Kong (2005) 1080p BluRay.mkv")
    result.container.files = [remaster, theatrical]

    with patch.object(downloader, "_update_attributes") as update:
        assert downloader.update_item_attributes(item, result) is True

    update.assert_called_once()
    assert update.call_args.args[1] is theatrical


def test_update_item_attributes_keeps_requested_movie_from_trilogy_pack(downloader):
    item = Movie(
        {
            "imdb_id": "tt1119646",
            "requested_by": "user",
            "title": "The Hangover",
            "year": 2009,
        }
    )
    matching_file, result = _download_result("The Hangover (2009) 720p BluRay.mkv")
    sequel_file, _ = _download_result("The Hangover 2 (2011) 720p BluRay.mkv")
    third_film_file, _ = _download_result("The Hangover 3 (2013) 720p BluRay.mkv")
    result.container.files = [matching_file, sequel_file, third_film_file]

    with patch.object(downloader, "_update_attributes") as update:
        assert downloader.update_item_attributes(item, result) is True

    update.assert_called_once()
    assert update.call_args.args[1] is matching_file


def test_match_file_to_item_season_matches_all_files(downloader):
    show, season = _show_with_episodes(1, 2)
    file, result = _download_result("Test.Show.S01E01-E02.mkv")

    with patch.object(downloader, "_update_attributes"):
        matched = downloader.match_file_to_item(
            season,
            _parsed(file.filename, seasons=[1], episodes=[1, 2], type="episode"),
            file,
            result,
            show=show,
        )

    assert matched is True
    assert season.active_stream is not None


def test_match_file_to_item_partial_season_is_match(downloader):
    show, season = _show_with_episodes(1, 2)
    file, result = _download_result("Test.Show.S01E01.mkv")

    with patch.object(downloader, "_update_attributes"):
        matched = downloader.match_file_to_item(
            season,
            _parsed(file.filename, seasons=[1], episodes=[1], type="episode"),
            file,
            result,
            show=show,
        )

    assert matched is True


def test_update_item_attributes_returns_false_for_empty_container(downloader):
    item = Movie({"imdb_id": "tt1375666", "requested_by": "user", "title": "Inception"})
    result = DownloadedTorrent(
        id=1,
        infohash="abc123",
        container=TorrentContainer(infohash="abc123"),
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
