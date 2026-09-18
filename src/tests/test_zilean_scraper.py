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
    cfg_dict = {
        "enabled": True,
        "url": "https://zilean.example/",
        "timeout": 10,
    }
    cfg_dict.update(cfg_overrides)
    config = ZileanConfig(**cfg_dict)
    monkeypatch.setattr(settings_manager.settings.scraping, "zilean", config)
    monkeypatch.setattr(Zilean, "validate", lambda self: True)
    monkeypatch.setattr(
        "program.services.scrapers.zilean.logger.log", lambda *_a, **_k: None
    )
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
    scraper.session.get = MagicMock(
        return_value=_response(429, retry_after=retry_after)
    )

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
    monkeypatch.setattr(
        "program.services.scrapers.zilean.logger.log", lambda *_a, **_k: None
    )

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
    monkeypatch.setattr(
        "program.services.scrapers.zilean.logger.log", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "program.services.scrapers.zilean.logger.error", lambda *_a, **_k: None
    )

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
    monkeypatch.setattr(
        "program.services.scrapers.zilean.logger.exception", lambda *_a, **_k: None
    )

    result = scraper.run(Movie({"title": "Film"}))
    assert result == {}


def test_run_typed_http_error_non_429_does_not_raise_rate_limit(monkeypatch):
    """A typed HTTPError with a non-429 status must not become RateLimitError."""
    scraper = _scraper_without_network(monkeypatch)
    raised = HTTPError("500")
    raised.response = SimpleNamespace(status_code=500, headers={})
    monkeypatch.setattr(scraper, "scrape", lambda _item: (_ for _ in ()).throw(raised))
    monkeypatch.setattr(
        "program.services.scrapers.zilean.logger.exception", lambda *_a, **_k: None
    )

    result = scraper.run(Movie({"title": "Film"}))
    assert result == {}


# ---------------------------------------------------------------------------
# Provider Protocol & Conformance Tests (Phase 4.3 TASK-003 Pilot)
# ---------------------------------------------------------------------------


def test_zilean_conforms_to_scraper_protocol_via_harness(monkeypatch):
    """Verify that the real Zilean scraper instance satisfies ScraperProviderProtocol."""
    from program.contracts import (
        ProviderCapability,
        ProviderCategory,
        ProviderProtocol,
        ScraperProviderProtocol,
    )
    from tests.test_provider_conformance import (
        assert_conforms_to_provider_protocol,
        assert_conforms_to_scraper_protocol,
    )

    scraper = _scraper_without_network(monkeypatch)
    assert isinstance(scraper, ProviderProtocol)
    assert isinstance(scraper, ScraperProviderProtocol)
    assert_conforms_to_provider_protocol(scraper)
    assert_conforms_to_scraper_protocol(scraper)

    manifest = scraper.manifest
    assert manifest.id == "zilean"
    assert manifest.name == "Zilean"
    assert manifest.category == ProviderCategory.SCRAPER
    assert ProviderCapability.TORRENT_SCRAPE in manifest.capabilities
    assert manifest.website == "https://github.com/iParr/zilean"

    # Confirm no credentials leak into manifest
    assert "token" not in manifest.model_dump()
    assert "api_key" not in manifest.model_dump()


def test_zilean_is_enabled_reflects_config(monkeypatch):
    """is_enabled property must accurately reflect settings.scraping.zilean.enabled."""
    scraper = _scraper_without_network(monkeypatch, enabled=True)
    assert scraper.is_enabled is True

    scraper.settings.enabled = False
    assert scraper.is_enabled is False


@pytest.mark.asyncio
async def test_zilean_probe_health_success(monkeypatch):
    """probe_health must return HEALTHY when /healthchecks/ping returns 200."""
    from program.contracts import ProviderHealthStatus

    scraper = _scraper_without_network(monkeypatch)
    scraper.session.get = MagicMock(
        return_value=_response(status_code=200, payload="OK")
    )

    health = await scraper.probe_health()
    assert health.status == ProviderHealthStatus.HEALTHY
    assert health.latency_ms >= 0.0
    assert "Connected" in health.message
    assert health.details.get("status_code") == 200


@pytest.mark.asyncio
async def test_zilean_probe_health_unconfigured_url(monkeypatch):
    """probe_health must return UNHEALTHY immediately if URL is empty."""
    from program.contracts import ProviderHealthStatus

    scraper = _scraper_without_network(monkeypatch, url="")
    health = await scraper.probe_health()
    assert health.status == ProviderHealthStatus.UNHEALTHY
    assert health.latency_ms == 0.0
    assert "not configured" in health.message


@pytest.mark.asyncio
async def test_zilean_probe_health_server_error_degraded(monkeypatch):
    """probe_health must return DEGRADED when server responds with 500+."""
    from program.contracts import ProviderHealthStatus

    scraper = _scraper_without_network(monkeypatch)
    scraper.session.get = MagicMock(return_value=_response(status_code=503))

    health = await scraper.probe_health()
    assert health.status == ProviderHealthStatus.DEGRADED
    assert "HTTP 503" in health.message


@pytest.mark.asyncio
async def test_zilean_probe_health_connection_error_unhealthy(monkeypatch):
    """probe_health must return UNHEALTHY with normalized error on connection drop."""
    import requests

    from program.contracts import ProviderHealthStatus

    scraper = _scraper_without_network(monkeypatch)
    scraper.session.get = MagicMock(
        side_effect=requests.ConnectionError("Connection refused")
    )

    health = await scraper.probe_health()
    assert health.status == ProviderHealthStatus.UNHEALTHY
    assert health.latency_ms == 0.0
    assert (
        "Connection refused" in health.message
        or "ProviderNetworkError" in health.message
    )


@pytest.mark.asyncio
async def test_zilean_search_maps_to_scrape_candidates(monkeypatch):
    """search() must map Zilean DMM response into canonical ScrapeCandidate objects."""
    from program.contracts import ScrapeRequest

    scraper = _scraper_without_network(monkeypatch)
    payload = [
        {
            "raw_title": "Reacher.S01E01.1080p.Web-DL",
            "info_hash": BASE32_HASH,  # Base32 input
            "size": 1500000000,
            "seeders": 42,
            "indexer": "dmm",
        },
        {
            "raw_title": "Reacher.S01E01.2160p.HDR",
            "info_hash": HEX_HASH.upper(),  # Uppercase hex input
            "size_bytes": 4500000000,
            "seeds": 100,
        },
        {"raw_title": "Invalid.No.Hash", "info_hash": "bad-hash"},
        {"raw_title": "", "info_hash": HEX_HASH},
        "not-a-dict",
    ]
    scraper.session.get = MagicMock(return_value=_response(payload=payload))

    req = ScrapeRequest(query="Reacher", season=1, episode=1)
    candidates = await scraper.search(req)

    assert len(candidates) == 2
    c1 = candidates[0]
    assert c1.raw_title == "Reacher.S01E01.1080p.Web-DL"
    assert c1.info_hash == HEX_HASH.lower()
    assert c1.indexer == "dmm"
    assert c1.size_bytes == 1500000000
    assert c1.seeders == 42
    assert c1.source == "zilean"

    c2 = candidates[1]
    assert c2.raw_title == "Reacher.S01E01.2160p.HDR"
    assert c2.info_hash == HEX_HASH.lower()
    assert c2.indexer == "dmm"
    assert c2.size_bytes == 4500000000
    assert c2.seeders == 100
    assert c2.source == "zilean"


@pytest.mark.asyncio
async def test_zilean_search_error_propagation_429(monkeypatch):
    """search() must raise ProviderRateLimitError on 429 status."""
    from program.contracts import ProviderRateLimitError, ScrapeRequest

    scraper = _scraper_without_network(monkeypatch)
    scraper.session.get = MagicMock(
        return_value=_response(status_code=429, retry_after="45")
    )

    req = ScrapeRequest(query="Reacher")
    with pytest.raises(ProviderRateLimitError) as exc_info:
        await scraper.search(req)

    assert exc_info.value.status_code == 429
    assert exc_info.value.retry_after_seconds == 45.0
    assert exc_info.value.is_transient is True


@pytest.mark.asyncio
async def test_zilean_search_error_propagation_500(monkeypatch):
    """search() must raise ProviderUnavailableError on 500 status."""
    from program.contracts import ProviderUnavailableError, ScrapeRequest

    scraper = _scraper_without_network(monkeypatch)
    scraper.session.get = MagicMock(return_value=_response(status_code=500))

    req = ScrapeRequest(query="Reacher")
    with pytest.raises(ProviderUnavailableError) as exc_info:
        await scraper.search(req)

    assert exc_info.value.status_code == 500
    assert exc_info.value.is_transient is True


# ---------------------------------------------------------------------------
# Phase 8: End-to-End Pipeline & RTN Non-Regression Tests
# ---------------------------------------------------------------------------


def test_zilean_legacy_pipeline_rtn_parsing_and_ranking_flow(monkeypatch):
    """Verify Zilean raw output feeds parse_results -> RTN -> ranked Streams."""
    from program.media.item import Movie
    from program.services.scrapers.shared import parse_results

    scraper = _scraper_without_network(monkeypatch)
    raw_payload = [
        {
            "raw_title": "Fight.Club.1999.1080p.BluRay.x264-SPARKS",
            "info_hash": "0123456789abcdef0123456789abcdef01234567",
        },
        {
            "raw_title": "Fight.Club.1999.CAM.XviD-LOWQUALITY",
            "info_hash": "1123456789abcdef0123456789abcdef01234567",
        },
    ]
    scraper.session.get = MagicMock(return_value=_response(payload=raw_payload))

    movie = Movie({"title": "Fight Club", "year": 1999})

    # Step 1: Legacy scrape() returns dict[str, str] mapping infohash -> raw_title
    raw_results = scraper.scrape(movie)
    assert len(raw_results) == 2
    assert "0123456789abcdef0123456789abcdef01234567" in raw_results
    assert "1123456789abcdef0123456789abcdef01234567" in raw_results

    # Step 2: parse_results passes to RTN for parsing, resolution, and ranking
    parsed_streams = parse_results(movie, raw_results, log_msg=False)

    # Step 3: Verify streams are created and ranked by RTN
    assert len(parsed_streams) > 0
    # BluRay 1080p release must rank higher / produce a valid parsed stream
    top_stream = next(iter(parsed_streams.values()))
    assert top_stream.parsed_data is not None
    assert top_stream.raw_title == "Fight.Club.1999.1080p.BluRay.x264-SPARKS"
    assert top_stream.infohash == "0123456789abcdef0123456789abcdef01234567"
