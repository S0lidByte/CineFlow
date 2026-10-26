"""Orionoid scraper unit tests."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from program.media.item import Episode, Movie, Season, Show
from program.services.scrapers.orionoid import Orionoid
from program.settings import settings_manager
from program.settings.models import OrionoidConfig


def _scraper_without_network(monkeypatch, **cfg_overrides) -> Orionoid:
    cfg = OrionoidConfig(
        enabled=True,
        api_key="12345678901234567890123456789012",
        timeout=10,
        **cfg_overrides,
    )
    monkeypatch.setattr(settings_manager.settings.scraping, "orionoid", cfg)
    monkeypatch.setattr(Orionoid, "validate", lambda self: True)
    scraper = Orionoid()
    return scraper


def test_orionoid_build_query_params_movie(monkeypatch):
    scraper = _scraper_without_network(monkeypatch)
    movie = Movie({"title": "Inception", "tmdb_id": 27205})

    params = scraper._build_query_params(movie)

    assert params.type == "movie"
    assert params.idtmdb == 27205
    assert params.idtvdb is None
    assert params.numberseason is None
    assert params.numberepisode is None


def test_orionoid_build_query_params_show_direct_tvdb_id(monkeypatch):
    scraper = _scraper_without_network(monkeypatch)
    show = Show({"title": "Breaking Bad", "tvdb_id": 81189})

    params = scraper._build_query_params(show)

    assert params.type == "show"
    assert params.idtvdb == 81189
    assert params.idtmdb is None


def test_orionoid_build_query_params_season_resolves_parent_tvdb_id(monkeypatch):
    scraper = _scraper_without_network(monkeypatch)
    show = Show({"title": "Breaking Bad", "tvdb_id": 81189})
    season = Season({"number": 1})
    season.parent = show

    params = scraper._build_query_params(season)

    assert params.type == "show"
    assert params.idtvdb == 81189
    assert params.numberseason == 1
    assert params.numberepisode is None


def test_orionoid_build_query_params_episode_resolves_parent_tvdb_id(monkeypatch):
    scraper = _scraper_without_network(monkeypatch)
    show = Show({"title": "Breaking Bad", "tvdb_id": 81189})
    season = Season({"number": 2})
    season.parent = show
    episode = Episode({"number": 5})
    episode.parent = season

    params = scraper._build_query_params(episode)

    assert params.type == "show"
    assert params.idtvdb == 81189
    assert params.numberseason == 2
    assert params.numberepisode == 5


def test_orionoid_build_query_params_season_preserves_own_tvdb_id_if_present(
    monkeypatch,
):
    scraper = _scraper_without_network(monkeypatch)
    show = Show({"title": "Breaking Bad", "tvdb_id": 81189})
    season = Season({"number": 1})
    season.parent = show
    season.tvdb_id = 99999

    params = scraper._build_query_params(season)

    assert params.idtvdb == 99999
    assert params.numberseason == 1
