"""StremThru Torznab scraper unit tests (mocked HTTP)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from program.media.item import Episode, Movie, Season, Show
from program.services.scrapers.stremthru import StremThru, TorznabResponse
from program.settings import settings_manager
from program.settings.models import AppModel, StremThruConfig

TORZNAB_FIXTURE = {
    "@attributes": {"version": "2.0"},
    "channel": {
        "title": "StremThru",
        "items": [
            {
                "title": "Example.Movie.2024.1080p.WEB-DL",
                "attr": [
                    {
                        "@attributes": {
                            "name": "infohash",
                            "value": "00443172c8abc7b0790b0c0b0c7f286f5b6b63c9",
                        }
                    }
                ],
            },
            {
                "title": "MissingHash.Movie",
                "attr": [{"@attributes": {"name": "size", "value": "1"}}],
            },
            {
                "title": None,
                "attr": [
                    {
                        "@attributes": {
                            "name": "infohash",
                            "value": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                        }
                    }
                ],
            },
        ],
    },
}


def _scraper_without_network(monkeypatch, **cfg_overrides) -> StremThru:
    cfg = StremThruConfig(
        enabled=True,
        url="https://stremthru.example",
        timeout=10,
        **cfg_overrides,
    )
    monkeypatch.setattr(settings_manager.settings.scraping, "stremthru", cfg)
    monkeypatch.setattr(StremThru, "validate", lambda self: True)
    # Custom loguru levels (SCRAPER / NOT_FOUND) are registered at app boot.
    monkeypatch.setattr(
        "program.services.scrapers.stremthru.logger.log",
        lambda *_a, **_k: None,
    )
    return StremThru()


def test_stremthru_defaults_disabled():
    validated = AppModel.model_validate({})
    assert validated.scraping.stremthru.enabled is False
    assert "stremthru" in validated.scraping.stremthru.url.lower()


def test_torznab_response_parses_infohash_attrs():
    parsed = TorznabResponse.model_validate(TORZNAB_FIXTURE)
    assert len(parsed.channel.items) == 3
    assert parsed.channel.items[0].attr[0].attributes.name == "infohash"


def test_build_params_movie_uses_imdb(monkeypatch):
    scraper = _scraper_without_network(monkeypatch)
    movie = Movie({"title": "Shawshank", "imdb_id": "tt0111161"})
    params = scraper._build_params(movie)
    assert params["t"] == "movie"
    assert params["cat"] == "2000"
    assert params["imdbid"] == "tt0111161"
    assert params["o"] == "json"
    assert "q" not in params


def test_build_params_episode_season_ep(monkeypatch):
    scraper = _scraper_without_network(monkeypatch)
    show = Show({"title": "Black Torch", "imdb_id": "tt1234567"})
    season = Season({"number": 1})
    season.parent = show
    episode = Episode({"number": 4})
    episode.parent = season

    params = scraper._build_params(episode)
    assert params["t"] == "tvsearch"
    assert params["cat"] == "5000"
    assert params["season"] == "1"
    assert params["ep"] == "4"
    assert params["imdbid"] == "tt1234567"


def test_build_params_falls_back_to_query(monkeypatch):
    scraper = _scraper_without_network(monkeypatch)
    movie = Movie({"title": "No Imdb Title"})
    params = scraper._build_params(movie)
    assert params["q"]
    assert "imdbid" not in params


def test_scrape_maps_infohash_to_title(monkeypatch):
    scraper = _scraper_without_network(monkeypatch)
    response = MagicMock()
    response.ok = True
    response.json.return_value = TORZNAB_FIXTURE
    scraper.session.get = MagicMock(return_value=response)

    movie = Movie({"title": "Example", "imdb_id": "tt0111161"})
    torrents = scraper.scrape(movie)

    assert torrents == {
        "00443172c8abc7b0790b0c0b0c7f286f5b6b63c9": "Example.Movie.2024.1080p.WEB-DL"
    }
    scraper.session.get.assert_called_once()
    call_kwargs = scraper.session.get.call_args
    assert call_kwargs.args[0] == "/v0/torznab/api"
    assert call_kwargs.kwargs["params"]["imdbid"] == "tt0111161"


def test_scrape_invalid_json_returns_empty(monkeypatch):
    scraper = _scraper_without_network(monkeypatch)
    response = MagicMock()
    response.ok = True
    response.json.return_value = {"not": "torznab"}
    scraper.session.get = MagicMock(return_value=response)

    assert scraper.scrape(Movie({"title": "x", "imdb_id": "tt1"})) == {}


def test_validate_disabled_returns_false(monkeypatch):
    monkeypatch.setattr(
        settings_manager.settings.scraping,
        "stremthru",
        StremThruConfig(enabled=False, url="https://stremthru.example"),
    )
    scraper = StremThru()
    assert scraper.initialized is False
    assert scraper.validate() is False


def test_run_raises_rate_limit(monkeypatch):
    from requests import HTTPError

    from program.utils.exceptions import RateLimitError

    scraper = _scraper_without_network(monkeypatch)

    err = HTTPError("429")
    err.response = SimpleNamespace(status_code=429, headers={"Retry-After": "30"})

    def boom(_item):
        raise err

    monkeypatch.setattr(scraper, "scrape", boom)

    with pytest.raises(RateLimitError):
        scraper.run(Movie({"title": "x", "imdb_id": "tt1"}))
