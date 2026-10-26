"""MediaFusion scraper unit tests (mocked HTTP)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from program.media.item import Episode, Movie, Season, Show
from program.services.scrapers.mediafusion import Mediafusion
from program.settings import settings_manager
from program.settings.models import MediafusionConfig


def _scraper_without_network(monkeypatch, **cfg_overrides) -> Mediafusion:
    cfg = MediafusionConfig(
        enabled=True,
        url="https://mediafusion.example",
        timeout=10,
        **cfg_overrides,
    )
    monkeypatch.setattr(settings_manager.settings.scraping, "mediafusion", cfg)
    monkeypatch.setattr(Mediafusion, "validate", lambda self: True)
    # Custom loguru levels (SCRAPER / NOT_FOUND) are registered at app boot.
    monkeypatch.setattr(
        "program.services.scrapers.mediafusion.logger.log",
        lambda *_a, **_k: None,
    )
    scraper = Mediafusion()
    scraper.encrypted_string = "dummy_encrypted_token"
    return scraper


def _response(status_code=200, payload=None):
    response = MagicMock()
    response.status_code = status_code
    response.ok = status_code < 400
    response.json.return_value = payload or {}
    return response


def test_mediafusion_retains_valid_streams_when_mixed_with_hashless_stream(monkeypatch):
    scraper = _scraper_without_network(monkeypatch)
    movie = Movie({"title": "Inception", "imdb_id": "tt1375666"})

    payload = {
        "streams": [
            {
                "name": "MediaFusion 1080p",
                "description": "📂 Inception.2010.1080p.BluRay.x264\n💾 8.5 GB",
                "infoHash": "00112233445566778899aabbccddeeff00112233",
            },
            {
                "name": "MediaFusion Info",
                "description": "🚫 Streams Found\n⚙️ Filtered by your configuration preferences\nSize limit exceeded",
                "infoHash": None,
            },
            {
                "name": "MediaFusion 4K",
                "description": "📂 Inception.2010.2160p.UHD.Remux\n💾 45 GB",
                "infoHash": "aabbccddeeff00112233445566778899aabbccdd",
            },
        ]
    }

    scraper.session.get = MagicMock(return_value=_response(payload=payload))

    results = scraper.scrape(movie)

    assert len(results) == 2
    assert "00112233445566778899aabbccddeeff00112233" in results
    assert "aabbccddeeff00112233445566778899aabbccdd" in results
    assert (
        results["00112233445566778899aabbccddeeff00112233"]
        == "Inception.2010.1080p.BluRay.x264"
    )
    assert (
        results["aabbccddeeff00112233445566778899aabbccdd"]
        == "Inception.2010.2160p.UHD.Remux"
    )


def test_mediafusion_entirely_hashless_response_returns_empty(monkeypatch):
    scraper = _scraper_without_network(monkeypatch)
    movie = Movie({"title": "Inception", "imdb_id": "tt1375666"})

    payload = {
        "streams": [
            {
                "name": "MediaFusion Info Card 1",
                "description": "🚫 Streams Found\n⚙️ Filtered by your configuration preferences\nResolution filtered",
                "infoHash": None,
            },
            {
                "name": "MediaFusion Info Card 2",
                "description": "No torrent data",
                "infoHash": "",
            },
        ]
    }

    scraper.session.get = MagicMock(return_value=_response(payload=payload))

    results = scraper.scrape(movie)
    assert results == {}


def test_mediafusion_rate_limit_exceeded_stream_raises(monkeypatch):
    scraper = _scraper_without_network(monkeypatch)
    movie = Movie({"title": "Inception", "imdb_id": "tt1375666"})

    payload = {
        "streams": [
            {
                "name": "rate-limit exceeded",
                "description": "Rate limit reached",
                "infoHash": None,
            }
        ]
    }

    scraper.session.get = MagicMock(return_value=_response(payload=payload))

    with pytest.raises(Exception, match="rate-limit exceeded"):
        scraper.scrape(movie)
