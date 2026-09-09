"""Zilean DMM scraper unit tests (mocked HTTP)."""

from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from requests import HTTPError

from program.media.item import Episode, Movie, Season, Show
from program.services.scrapers.zilean import Zilean
from program.settings import settings_manager
from program.settings.models import AppModel, ZileanConfig
from program.utils.exceptions import RateLimitError

HEX_HASH = "00112233445566778899aabbccddeeff00112233"
BASE32_HASH = base64.b32encode(bytes.fromhex(HEX_HASH)).decode("ascii")


def _scraper_without_network(monkeypatch, **cfg_overrides) -> Zilean:
    config = ZileanConfig(
        enabled=True,
        url="https://zilean.example/",
        timeout=10,
        **cfg_overrides,
    )
    monkeypatch.setattr(settings_manager.settings.scraping, "zilean", config)
    monkeypatch.setattr(Zilean, "validate", lambda self: True)
    monkeypatch.setattr("program.services.scrapers.zilean.logger.log", lambda *_a, **_k: None)
    return Zilean()


def _response(status_code=200, payload=None, json_error=None, retry_after=None):
    response = MagicMock()
    response.status_code = status_code
    response.ok = status_code < 400
    response.headers = {} if retry_after is None else {"Retry-After": retry_after}
    response.json.side_effect = json_error
    if json_error is None:
        response.json.return_value = payload
    return response


def test_zilean_defaults_and_persistence_fields_are_stable():
    config = AppModel.model_validate({}).scraping.zilean

    assert config.model_dump() == {
        "enabled": False,
        "url": "http://localhost:8181",
        "timeout": 30,
        "retries": 1,
        "ratelimit": True,
    }


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        (Movie({"title": "Film"}), {"Query": "Film"}),
        (Show({"title": "Series"}), {"Query": "Series", "Season": 1}),
    ],
)
def test_scrape_serializes_official_aliases(monkeypatch, item, expected):
    scraper = _scraper_without_network(monkeypatch)
    scraper.session.get = MagicMock(return_value=_response(payload=[]))

    assert scraper.scrape(item) == {}
    params = scraper.session.get.call_args.kwargs["params"]
    assert params == expected


def test_episode_serializes_parent_season_and_episode(monkeypatch):
    scraper = _scraper_without_network(monkeypatch)
    show = Show({"title": "Series"})
    season = Season({"number": 2})
    season.parent = show
    episode = Episode({"number": 3})
    episode.parent = season
    scraper.session.get = MagicMock(return_value=_response(payload=[]))

    scraper.scrape(episode)
    assert scraper.session.get.call_args.kwargs["params"] == {
        "Query": "Series",
        "Season": 2,
        "Episode": 3,
    }


def test_scrape_canonicalizes_and_preserves_valid_siblings(monkeypatch):
    scraper = _scraper_without_network(monkeypatch)
    scraper.session.get = MagicMock(
        return_value=_response(
            payload=[
                {"raw_title": "Hex", "info_hash": HEX_HASH.upper()},
                {"raw_title": "Base32 wins", "info_hash": BASE32_HASH},
                {"raw_title": "Bad", "info_hash": "bad"},
                {"info_hash": HEX_HASH},
                "not-an-object",
            ]
        )
    )

    assert scraper.scrape(Movie({"title": "Film"})) == {HEX_HASH: "Base32 wins"}


@pytest.mark.parametrize("payload", [{"data": []}, "not a list"])
def test_scrape_non_list_payload_returns_empty(monkeypatch, payload):
    scraper = _scraper_without_network(monkeypatch)
    scraper.session.get = MagicMock(return_value=_response(payload=payload))
    assert scraper.scrape(Movie({"title": "Film"})) == {}


def test_scrape_invalid_json_returns_empty(monkeypatch):
    scraper = _scraper_without_network(monkeypatch)
    scraper.session.get = MagicMock(
        return_value=_response(json_error=ValueError("invalid JSON"))
    )
    assert scraper.scrape(Movie({"title": "Film"})) == {}


def test_scrape_non_ok_does_not_log_response_body(monkeypatch):
    scraper = _scraper_without_network(monkeypatch)
    response = _response(status_code=502)
    response.text = "api_key=leaked"
    scraper.session.get = MagicMock(return_value=response)
    warning = MagicMock()
    monkeypatch.setattr("program.services.scrapers.zilean.logger.warning", warning)

    assert scraper.scrape(Movie({"title": "Film"})) == {}
    assert "leaked" not in str(warning.call_args)


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [("30", 30.0), ("later", None), (None, None)],
)
def test_scrape_received_429_raises_rate_limit(monkeypatch, retry_after, expected):
    scraper = _scraper_without_network(monkeypatch)
    scraper.session.get = MagicMock(return_value=_response(429, retry_after=retry_after))

    with pytest.raises(RateLimitError) as error:
        scraper.scrape(Movie({"title": "Film"}))
    assert error.value.retry_after == expected


def test_run_preserves_received_rate_limit_error(monkeypatch):
    scraper = _scraper_without_network(monkeypatch)
    limited = RateLimitError("limited", retry_after=12)
    monkeypatch.setattr(scraper, "scrape", lambda _item: (_ for _ in ()).throw(limited))

    with pytest.raises(RateLimitError) as error:
        scraper.run(Movie({"title": "Film"}))
    assert error.value is limited


def test_run_safely_translates_raised_429(monkeypatch):
    scraper = _scraper_without_network(monkeypatch)
    raised = HTTPError("429")
    raised.response = SimpleNamespace(status_code=429, headers={"Retry-After": "later"})
    monkeypatch.setattr(scraper, "scrape", lambda _item: (_ for _ in ()).throw(raised))

    with pytest.raises(RateLimitError) as error:
        scraper.run(Movie({"title": "Film"}))
    assert error.value.retry_after is None


@pytest.mark.parametrize(
    "url",
    [
        "https://zilean.example",
        "https://zilean.example/",
        "https://zilean.example/sub/path/",
    ],
)
def test_scrape_url_normalization_single_slash(monkeypatch, url):
    """Trailing slashes in the configured URL must not produce double-slash paths."""
    config = ZileanConfig(enabled=True, url=url, timeout=10)
    monkeypatch.setattr(settings_manager.settings.scraping, "zilean", config)
    monkeypatch.setattr(Zilean, "validate", lambda self: True)
    monkeypatch.setattr("program.services.scrapers.zilean.logger.log", lambda *_a, **_k: None)

    scraper = Zilean()
    scraper.session.get = MagicMock(return_value=_response(payload=[]))

    scraper.scrape(Movie({"title": "Film"}))
    called_url = scraper.session.get.call_args.args[0]
    assert "//" not in called_url.split("://", 1)[1]
    assert called_url.endswith("/dmm/filtered")


@pytest.mark.parametrize(
    "url",
    [
        "https://zilean.example",
        "https://zilean.example/",
    ],
)
def test_validate_url_normalization_single_slash(monkeypatch, url):
    """Trailing slashes in the configured URL must not produce double-slash in validate."""
    config = ZileanConfig(enabled=True, url=url, timeout=10)
    monkeypatch.setattr(settings_manager.settings.scraping, "zilean", config)
    monkeypatch.setattr("program.services.scrapers.zilean.logger.log", lambda *_a, **_k: None)
    monkeypatch.setattr("program.services.scrapers.zilean.logger.error", lambda *_a, **_k: None)

    scraper = Zilean.__new__(Zilean)
    scraper.settings = config
    scraper.timeout = config.timeout
    scraper.session = MagicMock()
    scraper.session.get.return_value = _response(payload="OK")

    scraper.validate()
    called_url = scraper.session.get.call_args.args[0]
    assert "//" not in called_url.split("://", 1)[1]
    assert called_url.endswith("/healthchecks/ping")


@pytest.mark.parametrize(
    "exc",
    [
        Exception("rate limit exceeded"),
        Exception("HTTP 429 Too Many Requests"),
        Exception("status 429 in unrelated payload"),
    ],
)
def test_run_generic_exception_with_rate_limit_text_does_not_raise(monkeypatch, exc):
    """Generic exceptions mentioning '429' or 'rate limit' must NOT become RateLimitError."""
    scraper = _scraper_without_network(monkeypatch)
    monkeypatch.setattr(scraper, "scrape", lambda _item: (_ for _ in ()).throw(exc))
    monkeypatch.setattr("program.services.scrapers.zilean.logger.exception", lambda *_a, **_k: None)

    result = scraper.run(Movie({"title": "Film"}))
    assert result == {}


def test_run_typed_http_error_non_429_does_not_raise_rate_limit(monkeypatch):
    """A typed HTTPError with a non-429 status must not become RateLimitError."""
    scraper = _scraper_without_network(monkeypatch)
    raised = HTTPError("500")
    raised.response = SimpleNamespace(status_code=500, headers={})
    monkeypatch.setattr(scraper, "scrape", lambda _item: (_ for _ in ()).throw(raised))
    monkeypatch.setattr("program.services.scrapers.zilean.logger.exception", lambda *_a, **_k: None)

    result = scraper.run(Movie({"title": "Film"}))
    assert result == {}
