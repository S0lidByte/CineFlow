"""Persisted MediaItem.runtime for optional debrid bitrate floors."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from program.media.item import Episode, Movie, Season, Show
from program.services.downloaders.models import (
    BitrateLimitExceededException,
    DebridFile,
)
from program.services.indexers.runtime import coerce_runtime_minutes
from program.services.indexers.tmdb_indexer import TMDBIndexer
from program.services.indexers.tvdb_indexer import TVDBIndexer


def test_coerce_runtime_minutes_rejects_unusable_values():
    assert coerce_runtime_minutes(None) is None
    assert coerce_runtime_minutes(0) is None
    assert coerce_runtime_minutes(-5) is None
    assert coerce_runtime_minutes("abc") is None
    assert coerce_runtime_minutes(float("nan")) is None
    assert coerce_runtime_minutes(42) == 42.0
    assert coerce_runtime_minutes("90.5") == 90.5


def test_movie_init_persists_runtime_from_dict():
    movie = Movie({"title": "Test", "type": "movie", "runtime": 128})
    assert movie.runtime == 128


def test_episode_inherits_show_runtime_when_unset():
    show = Show({"title": "Show", "type": "show", "runtime": 45})
    season = Season({"title": "Season 1", "type": "season", "number": 1})
    season.parent = show
    episode = Episode({"title": "Ep", "type": "episode", "number": 1})
    episode.parent = season

    assert episode.runtime == 45.0


def test_episode_own_runtime_wins_over_show():
    show = Show({"title": "Show", "type": "show", "runtime": 45})
    season = Season({"title": "Season 1", "type": "season", "number": 1})
    season.parent = show
    episode = Episode({"title": "Ep", "type": "episode", "number": 1, "runtime": 52})
    episode.parent = season

    assert episode.runtime == 52.0


def test_tmdb_update_movie_metadata_sets_runtime(monkeypatch):
    indexer = TMDBIndexer.__new__(TMDBIndexer)
    indexer.api = MagicMock()
    indexer.trakt_api = MagicMock()
    indexer.trakt_api.get_aliases.return_value = {}

    movie_details = SimpleNamespace(
        release_date="2020-01-01",
        genres=[],
        production_countries=[],
        vote_average=7.5,
        release_dates=SimpleNamespace(results=[]),
        imdb_id="tt123",
        poster_path="/p.jpg",
        title="Runtime Movie",
        id=99,
        original_language="en",
        runtime=117,
    )
    indexer.api.get_movie_details_with_external_ids_and_release_dates.return_value = (
        movie_details
    )

    movie = Movie({"title": "Runtime Movie", "type": "movie", "tmdb_id": "99"})
    assert indexer._update_movie_metadata(movie) is True
    assert movie.runtime == 117.0


def test_tvdb_update_episode_metadata_sets_runtime():
    indexer = TVDBIndexer.__new__(TVDBIndexer)
    episode = Episode({"title": "Ep", "type": "episode", "number": 3})
    episode_data = SimpleNamespace(
        aired="2021-02-03",
        year="2021",
        image=None,
        id=555,
        name="Pilot",
        absolute_number=3,
        runtime=43,
    )

    indexer._update_episode_metadata(episode, episode_data)
    assert episode.runtime == 43.0


def test_downloader_uses_item_runtime_via_settings_override(monkeypatch):
    from program.settings import settings_manager

    monkeypatch.setattr(
        settings_manager.settings.downloaders,
        "movie_min_avg_bitrate",
        20.0,
    )

    with settings_manager.override(runtime_minutes=120.0):
        with pytest.raises(BitrateLimitExceededException):
            DebridFile.create(
                filesize_bytes=500 * 1024 * 1024,
                filename="movie.mkv",
                filetype="movie",
                limit_filesize=False,
            )
