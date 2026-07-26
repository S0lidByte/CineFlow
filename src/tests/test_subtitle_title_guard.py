"""Regression tests for OpenSubtitles / SubDL wrong-title rejection."""

from __future__ import annotations

from unittest.mock import MagicMock

from program.services.post_processing.subtitles.providers.base import SubtitleItem
from program.services.post_processing.subtitles.providers.opensubtitles import (
    OpenSubtitlesProvider,
)
from program.services.post_processing.subtitles.utils import (
    subtitle_result_title_ok,
    subtitle_title_matches,
)


def test_atlantis_rejects_game_of_thrones_fulltext_filename():
    """Stargate Atlantis must not accept a Game of Thrones fulltext hit."""
    expected = "Stargate Atlantis"
    wrong = "Game.of.Thrones.S05E10.HDTV.x264-KILLERS.srt"

    assert not subtitle_title_matches(expected, None, wrong)
    assert not subtitle_result_title_ok(
        expected,
        matched_by="fulltext",
        movie_name="Game of Thrones",
        filename=wrong,
    )


def test_atlantis_accepts_matching_filename():
    expected = "Stargate Atlantis"
    right = "Stargate.Atlantis.S05E10.720p.HDTV.x264-DIMENSION.srt"

    assert subtitle_title_matches(expected, "Stargate Atlantis", right)
    assert subtitle_result_title_ok(
        expected,
        matched_by="fulltext",
        movie_name="Stargate Atlantis",
        filename=right,
    )


def test_strong_hash_match_bypasses_title_guard():
    """Hash matches are trusted even if filename metadata is odd."""
    assert subtitle_result_title_ok(
        "Stargate Atlantis",
        matched_by="moviehash",
        movie_name="Game of Thrones",
        filename="Game.of.Thrones.S05E10.HDTV.x264-KILLERS.srt",
    )


def test_strong_tag_match_bypasses_title_guard():
    assert subtitle_result_title_ok(
        "Stargate Atlantis",
        matched_by="tag",
        movie_name=None,
        filename="Game.of.Thrones.S05E10.HDTV.x264-KILLERS.srt",
    )


def test_imdb_and_subdl_matches_still_require_title():
    wrong = "Game.of.Thrones.S05E10.HDTV.x264-KILLERS.srt"
    for matched_by in ("imdbid", "imdb", "tmdb", "fulltext"):
        assert not subtitle_result_title_ok(
            "Stargate Atlantis",
            matched_by=matched_by,
            movie_name="Game of Thrones",
            filename=wrong,
        )


def test_opensubtitles_imdb_search_without_search_tags():
    """IMDB+season+episode must be queried even when release tags are missing."""
    provider = OpenSubtitlesProvider(
        username="u",
        password="p",
        allow_anonymous=False,
    )
    provider.token = "token"
    provider.login_time = 10**12  # far future so auth is skipped

    captured: list[list[dict[str, str]]] = []

    def fake_search(_token, criteria):
        captured.append(criteria)
        return {"status": "200 OK", "data": []}

    provider.server = MagicMock()
    provider.server.SearchSubtitles.side_effect = fake_search

    results = provider.search_subtitles(
        imdb_id="tt0374455",
        video_hash=None,
        file_size=None,
        filename=None,
        search_tags=None,
        season=5,
        episode=10,
        language="por",
    )

    assert results == []
    assert len(captured) == 1
    assert len(captured[0]) == 1
    assert captured[0][0] == {
        "sublanguageid": "por",
        "imdbid": "0374455",
        "season": "5",
        "episode": "10",
    }
    assert "tags" not in captured[0][0]


def test_opensubtitles_imdb_search_includes_optional_tags():
    provider = OpenSubtitlesProvider(username="u", password="p")
    provider.token = "token"
    provider.login_time = 10**12

    captured: list[list[dict[str, str]]] = []

    def fake_search(_token, criteria):
        captured.append(criteria)
        return {"status": "200 OK", "data": []}

    provider.server = MagicMock()
    provider.server.SearchSubtitles.side_effect = fake_search

    provider.search_subtitles(
        imdb_id="tt0374455",
        search_tags="hdtv,killers",
        season=5,
        episode=10,
        language="eng",
    )

    assert captured[0][0]["tags"] == "hdtv,killers"
    assert captured[0][0]["imdbid"] == "0374455"


def test_subtitle_service_filters_wrong_title_before_download(monkeypatch):
    """Accept path must drop GoT fulltext before attempting download."""
    from program.services.post_processing.subtitles.subtitle import SubtitleService

    service = SubtitleService.__new__(SubtitleService)
    service.providers = []
    service.languages = ["por"]
    service.settings = MagicMock()
    service.initialized = True

    wrong = SubtitleItem(
        id="1",
        language="por",
        filename="Game.of.Thrones.S05E10.HDTV.x264-KILLERS.srt",
        download_count=100,
        rating=9.0,
        matched_by="fulltext",
        movie_hash=None,
        movie_name="Game of Thrones",
        provider="opensubtitles",
        score=1100.0,
    )
    right = SubtitleItem(
        id="2",
        language="por",
        filename="Stargate.Atlantis.S05E10.720p.HDTV.x264-DIMENSION.srt",
        download_count=10,
        rating=8.0,
        matched_by="fulltext",
        movie_hash=None,
        movie_name="Stargate Atlantis",
        provider="opensubtitles",
        score=1080.0,
    )

    provider = MagicMock()
    provider.name = "opensubtitles"
    provider.search_subtitles.return_value = [wrong, right]
    provider.download_subtitle.return_value = "1\n00:00:01,000 --> 00:00:02,000\nok\n"
    service.providers = [provider]

    item = MagicMock()
    item.id = 42
    item.log_string = "Stargate Atlantis S05E10"
    item.title = "First Contact"
    item.top_title = "Stargate Atlantis"
    media_entry = MagicMock()
    media_entry.original_filename = "Stargate.Atlantis.S05E10.mkv"
    media_entry.file_size = 1234
    item.media_entry = media_entry

    monkeypatch.setattr(service, "_get_existing_subtitle", lambda *_a, **_k: None)

    created: list[object] = []

    class FakeSubtitleEntry:
        media_item_id = None
        available_in_vfs = False

        @classmethod
        def create_subtitle_entry(cls, **kwargs):
            created.append(kwargs)
            return cls()

    monkeypatch.setattr(
        "program.services.post_processing.subtitles.subtitle.SubtitleEntry",
        FakeSubtitleEntry,
    )

    session = MagicMock()
    monkeypatch.setattr(
        "program.services.post_processing.subtitles.subtitle.object_session",
        lambda _item: session,
    )
    monkeypatch.setattr(
        "program.program.riven",
        MagicMock(services=MagicMock(filesystem=None)),
    )

    service._fetch_subtitle_for_language(
        item=item,
        language="por",
        video_path="/Shows/Atlantis/S05E10.mkv",
        video_hash=None,
        file_size=1234,
        original_filename="Stargate.Atlantis.S05E10.mkv",
        search_tags=None,
        imdb_id="tt0374455",
        season=5,
        episode=10,
    )

    provider.download_subtitle.assert_called_once()
    downloaded = provider.download_subtitle.call_args[0][0]
    assert downloaded.id == "2"
    assert "Stargate" in downloaded.filename
    assert created
