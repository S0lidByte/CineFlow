"""Provider Conformance Test Suite (Phase 4.7 TASK-002).

This module provides a reusable conformance verification harness and test suites
for CineFlow providers adhering to PEP 544 structural protocols:
- ProviderProtocol (base)
- ScraperProviderProtocol
- DownloaderProviderProtocol

It validates:
1. Reusable contract validation helpers and assertions (assert_conforms_to_provider_protocol, etc.)
2. Positive and negative structural subtyping conformance across all protocols
3. Complete behavior verification of search(), probe_health(), and check_availability()
4. Error propagation and normalization under provider failure states
5. Robust mock implementations modeled after real provider fixtures (Zilean, RealDebrid)
6. Secret non-leakage in health result diagnostics and payloads
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from program.contracts.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderUnavailableError,
)
from program.contracts.providers import (
    DownloaderProviderProtocol,
    ProviderCapability,
    ProviderCategory,
    ProviderHealthResult,
    ProviderHealthStatus,
    ProviderManifest,
    ProviderProtocol,
    ScrapeCandidate,
    ScrapeRequest,
    ScraperProviderProtocol,
)
from program.contracts.telemetry import REDACTED_SUBSTITUTE

# ---------------------------------------------------------------------------
# Reusable Conformance Assertion Helpers
# ---------------------------------------------------------------------------


def assert_conforms_to_provider_protocol(provider: Any) -> None:
    """Assert that a provider object conforms structurally to ProviderProtocol."""
    assert isinstance(provider, ProviderProtocol), (
        f"{type(provider).__name__} does not satisfy ProviderProtocol structural typing"
    )
    # Validate property types
    manifest = provider.manifest
    assert isinstance(manifest, ProviderManifest), (
        f"Expected ProviderManifest instance, got {type(manifest).__name__}"
    )
    assert isinstance(provider.is_enabled, bool), (
        f"Expected is_enabled to be bool, got {type(provider.is_enabled).__name__}"
    )
    assert callable(getattr(provider, "probe_health", None)), (
        f"{type(provider).__name__} is missing callable probe_health method"
    )


def assert_conforms_to_scraper_protocol(provider: Any) -> None:
    """Assert that a provider object conforms structurally to ScraperProviderProtocol."""
    assert_conforms_to_provider_protocol(provider)
    assert isinstance(provider, ScraperProviderProtocol), (
        f"{type(provider).__name__} does not satisfy ScraperProviderProtocol structural typing"
    )
    assert ProviderCapability.TORRENT_SCRAPE in provider.manifest.capabilities or (
        ProviderCapability.STREAM_SCRAPE in provider.manifest.capabilities
    ), (
        f"Scraper manifest {provider.manifest.id} must advertise TORRENT_SCRAPE or STREAM_SCRAPE capability"
    )
    assert callable(getattr(provider, "search", None)), (
        f"{type(provider).__name__} is missing callable search method"
    )


def assert_conforms_to_downloader_protocol(provider: Any) -> None:
    """Assert that a provider object conforms structurally to DownloaderProviderProtocol."""
    assert_conforms_to_provider_protocol(provider)
    assert isinstance(provider, DownloaderProviderProtocol), (
        f"{type(provider).__name__} does not satisfy DownloaderProviderProtocol structural typing"
    )
    assert (
        ProviderCapability.DIRECT_DOWNLOAD in provider.manifest.capabilities
        or ProviderCapability.DEBRID_STREAM in provider.manifest.capabilities
    ), (
        f"Downloader manifest {provider.manifest.id} must advertise DIRECT_DOWNLOAD or DEBRID_STREAM capability"
    )
    assert callable(getattr(provider, "check_availability", None)), (
        f"{type(provider).__name__} is missing callable check_availability method"
    )


# ---------------------------------------------------------------------------
# Conforming Mock Implementations
# ---------------------------------------------------------------------------


class MockZileanScraper:
    """Mock conforming scraper provider modeled after Zilean."""

    def __init__(self, api_key: str = "test-zilean-key") -> None:
        self._api_key = api_key
        self._is_enabled = True
        self._manifest = ProviderManifest(
            id="zilean_mock",
            name="Zilean DMM Scraper",
            version="1.0.0",
            category=ProviderCategory.SCRAPER,
            capabilities=frozenset({ProviderCapability.TORRENT_SCRAPE}),
            description="Mock Zilean Scraper",
            website="https://github.com/iPromKnight/zilean",
        )

    @property
    def manifest(self) -> ProviderManifest:
        return self._manifest

    @property
    def is_enabled(self) -> bool:
        return self._is_enabled

    async def probe_health(self) -> ProviderHealthResult:
        # Intentionally passing sensitive token in details to verify automatic redaction
        return ProviderHealthResult(
            status=ProviderHealthStatus.HEALTHY,
            latency_ms=15.4,
            message="Zilean service is operational",
            details={
                "endpoint": "http://127.0.0.1:8181/healthchecks/ping",
                "api_key": self._api_key,
                "version": "1.0.0",
            },
        )

    async def search(self, request: ScrapeRequest) -> list[ScrapeCandidate]:
        if not self._is_enabled:
            return []
        if request.query == "TRIGGER_RATE_LIMIT":
            raise ProviderRateLimitError(
                "Zilean rate limit exceeded",
                status_code=429,
                provider_name="zilean_mock",
                retry_after_seconds=60.0,
            )
        if request.query == "TRIGGER_SERVER_ERROR":
            raise ProviderUnavailableError(
                "Zilean internal server error",
                status_code=500,
                provider_name="zilean_mock",
            )
        return [
            ScrapeCandidate(
                raw_title=f"{request.query} 1080p Web-DL",
                info_hash="0123456789abcdef0123456789abcdef01234567",
                indexer="dmm",
                size_bytes=2147483648,
                seeders=15,
                source="zilean",
            )
        ]


class MockRealDebridDownloader:
    """Mock conforming downloader provider modeled after Real-Debrid."""

    def __init__(self, api_token: str = "test-rd-token") -> None:  # noqa: S107
        self._api_token = api_token
        self._is_enabled = True
        self._manifest = ProviderManifest(
            id="realdebrid_mock",
            name="Real-Debrid Downloader",
            version="1.0.0",
            category=ProviderCategory.DOWNLOADER,
            capabilities=frozenset(
                {ProviderCapability.DIRECT_DOWNLOAD, ProviderCapability.DEBRID_STREAM}
            ),
            description="Mock Real-Debrid Downloader",
            website="https://real-debrid.com",
        )

    @property
    def manifest(self) -> ProviderManifest:
        return self._manifest

    @property
    def is_enabled(self) -> bool:
        return self._is_enabled

    async def probe_health(self) -> ProviderHealthResult:
        return ProviderHealthResult(
            status=ProviderHealthStatus.HEALTHY,
            latency_ms=120.0,
            message="Real-Debrid account premium",
            details={
                "user": "tester",
                "type": "premium",
                "token": self._api_token,
            },
        )

    async def check_availability(self, info_hashes: list[str]) -> dict[str, bool]:
        if not self._is_enabled:
            return {h: False for h in info_hashes}
        if "BAD_AUTH_HASH" in info_hashes:
            raise ProviderAuthError(
                "Invalid API token",
                status_code=401,
                provider_name="realdebrid_mock",
            )
        return {h: True for h in info_hashes}


# ---------------------------------------------------------------------------
# Non-Conforming Class Definitions for Negative Structural Tests
# ---------------------------------------------------------------------------


class NonConformingMissingManifest:
    @property
    def is_enabled(self) -> bool:
        return True

    async def probe_health(self) -> ProviderHealthResult:
        return ProviderHealthResult(status=ProviderHealthStatus.HEALTHY, latency_ms=1.0)


class NonConformingMissingIsEnabled:
    @property
    def manifest(self) -> ProviderManifest:
        return ProviderManifest(
            id="bad",
            name="Bad",
            version="1.0.0",
            category=ProviderCategory.SCRAPER,
            capabilities=frozenset({ProviderCapability.TORRENT_SCRAPE}),
        )

    async def probe_health(self) -> ProviderHealthResult:
        return ProviderHealthResult(status=ProviderHealthStatus.HEALTHY, latency_ms=1.0)


class NonConformingMissingProbeHealth:
    @property
    def manifest(self) -> ProviderManifest:
        return ProviderManifest(
            id="bad",
            name="Bad",
            version="1.0.0",
            category=ProviderCategory.SCRAPER,
            capabilities=frozenset({ProviderCapability.TORRENT_SCRAPE}),
        )

    @property
    def is_enabled(self) -> bool:
        return True


class NonConformingScraperMissingSearch:
    @property
    def manifest(self) -> ProviderManifest:
        return ProviderManifest(
            id="bad_scraper",
            name="Bad Scraper",
            version="1.0.0",
            category=ProviderCategory.SCRAPER,
            capabilities=frozenset({ProviderCapability.TORRENT_SCRAPE}),
        )

    @property
    def is_enabled(self) -> bool:
        return True

    async def probe_health(self) -> ProviderHealthResult:
        return ProviderHealthResult(status=ProviderHealthStatus.HEALTHY, latency_ms=1.0)


class NonConformingDownloaderMissingCheckAvailability:
    @property
    def manifest(self) -> ProviderManifest:
        return ProviderManifest(
            id="bad_downloader",
            name="Bad Downloader",
            version="1.0.0",
            category=ProviderCategory.DOWNLOADER,
            capabilities=frozenset({ProviderCapability.DIRECT_DOWNLOAD}),
        )

    @property
    def is_enabled(self) -> bool:
        return True

    async def probe_health(self) -> ProviderHealthResult:
        return ProviderHealthResult(status=ProviderHealthStatus.HEALTHY, latency_ms=1.0)


# ---------------------------------------------------------------------------
# Test Suites
# ---------------------------------------------------------------------------


class TestProviderConformanceHarness:
    """Test suite for validating the reusable conformance assertion helpers."""

    def test_scraper_conforms_to_protocols(self) -> None:
        scraper = MockZileanScraper()
        assert_conforms_to_provider_protocol(scraper)
        assert_conforms_to_scraper_protocol(scraper)

    def test_downloader_conforms_to_protocols(self) -> None:
        downloader = MockRealDebridDownloader()
        assert_conforms_to_provider_protocol(downloader)
        assert_conforms_to_downloader_protocol(downloader)

    def test_scraper_does_not_conform_to_downloader(self) -> None:
        scraper = MockZileanScraper()
        assert not isinstance(scraper, DownloaderProviderProtocol)
        with pytest.raises(AssertionError):
            assert_conforms_to_downloader_protocol(scraper)

    def test_downloader_does_not_conform_to_scraper(self) -> None:
        downloader = MockRealDebridDownloader()
        assert not isinstance(downloader, ScraperProviderProtocol)
        with pytest.raises(AssertionError):
            assert_conforms_to_scraper_protocol(downloader)

    @pytest.mark.parametrize(
        "bad_provider_cls",
        [
            NonConformingMissingManifest,
            NonConformingMissingIsEnabled,
            NonConformingMissingProbeHealth,
        ],
    )
    def test_negative_base_provider_conformance(self, bad_provider_cls: type) -> None:
        instance = bad_provider_cls()
        assert not isinstance(instance, ProviderProtocol)
        with pytest.raises(AssertionError):
            assert_conforms_to_provider_protocol(instance)

    def test_negative_scraper_conformance(self) -> None:
        instance = NonConformingScraperMissingSearch()
        assert isinstance(instance, ProviderProtocol)
        assert not isinstance(instance, ScraperProviderProtocol)
        with pytest.raises(AssertionError):
            assert_conforms_to_scraper_protocol(instance)

    def test_negative_downloader_conformance(self) -> None:
        instance = NonConformingDownloaderMissingCheckAvailability()
        assert isinstance(instance, ProviderProtocol)
        assert not isinstance(instance, DownloaderProviderProtocol)
        with pytest.raises(AssertionError):
            assert_conforms_to_downloader_protocol(instance)


class TestScraperProviderBehavior:
    """Behavioral and error-handling tests for conforming scraper providers."""

    @pytest.mark.asyncio
    async def test_scraper_search_success(self) -> None:
        scraper = MockZileanScraper()
        req = ScrapeRequest(query="Reacher", year=2022)
        candidates = await scraper.search(req)
        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate.raw_title == "Reacher 1080p Web-DL"
        assert candidate.info_hash == "0123456789abcdef0123456789abcdef01234567"
        assert candidate.indexer == "dmm"
        assert candidate.seeders == 15

    @pytest.mark.asyncio
    async def test_scraper_probe_health_redacts_secrets(self) -> None:
        scraper = MockZileanScraper(api_key="super_secret_zilean_key_12345")
        health = await scraper.probe_health()
        assert health.status == ProviderHealthStatus.HEALTHY
        assert health.latency_ms > 0
        assert health.details["api_key"] == REDACTED_SUBSTITUTE
        assert "super_secret" not in health.model_dump_json()

    @pytest.mark.asyncio
    async def test_scraper_error_propagation(self) -> None:
        scraper = MockZileanScraper()
        req_rate_limit = ScrapeRequest(query="TRIGGER_RATE_LIMIT")
        with pytest.raises(ProviderRateLimitError) as exc_info:
            await scraper.search(req_rate_limit)
        assert exc_info.value.status_code == 429
        assert exc_info.value.retry_after_seconds == 60.0
        assert exc_info.value.is_transient is True

        req_server_err = ScrapeRequest(query="TRIGGER_SERVER_ERROR")
        with pytest.raises(ProviderUnavailableError) as exc_info_srv:
            await scraper.search(req_server_err)
        assert exc_info_srv.value.status_code == 500
        assert exc_info_srv.value.is_transient is True


class TestDownloaderProviderBehavior:
    """Behavioral and error-handling tests for conforming downloader providers."""

    @pytest.mark.asyncio
    async def test_downloader_check_availability_success(self) -> None:
        downloader = MockRealDebridDownloader()
        hashes = [
            "0123456789abcdef0123456789abcdef01234567",
            "abcdef0123456789abcdef0123456789abcdef01",
        ]
        availability = await downloader.check_availability(hashes)
        assert len(availability) == 2
        assert availability[hashes[0]] is True
        assert availability[hashes[1]] is True

    @pytest.mark.asyncio
    async def test_downloader_probe_health_redacts_secrets(self) -> None:
        downloader = MockRealDebridDownloader(api_token="super_secret_rd_token_abcdef")
        health = await downloader.probe_health()
        assert health.status == ProviderHealthStatus.HEALTHY
        assert health.latency_ms > 0
        assert health.details["token"] == REDACTED_SUBSTITUTE
        assert "super_secret" not in health.model_dump_json()

    @pytest.mark.asyncio
    async def test_downloader_error_propagation(self) -> None:
        downloader = MockRealDebridDownloader()
        with pytest.raises(ProviderAuthError) as exc_info:
            await downloader.check_availability(["BAD_AUTH_HASH"])
        assert exc_info.value.status_code == 401
        assert exc_info.value.provider_name == "realdebrid_mock"
        assert exc_info.value.is_transient is False


class TestFixtureBackedProviderAdapters:
    """Tests that prove real fixture payloads map cleanly to conforming providers."""

    @pytest.mark.asyncio
    async def test_zilean_fixture_adapter_conformance(self) -> None:
        fixtures_dir = Path(__file__).parent / "fixtures"
        zilean_data = json.loads(
            (fixtures_dir / "zilean_fixtures.json").read_text(encoding="utf-8")
        )

        class ZileanFixtureAdapter:
            @property
            def manifest(self) -> ProviderManifest:
                return ProviderManifest(
                    id="zilean_fixture",
                    name="Zilean Fixture Scraper",
                    version="1.0.0",
                    category=ProviderCategory.SCRAPER,
                    capabilities=frozenset({ProviderCapability.TORRENT_SCRAPE}),
                )

            @property
            def is_enabled(self) -> bool:
                return True

            async def probe_health(self) -> ProviderHealthResult:
                return ProviderHealthResult(
                    status=ProviderHealthStatus.HEALTHY,
                    latency_ms=8.5,
                    message="OK",
                )

            async def search(self, request: ScrapeRequest) -> list[ScrapeCandidate]:
                raw_items = zilean_data["search_success"]
                return [
                    ScrapeCandidate(
                        raw_title=item["raw_title"],
                        info_hash=item["info_hash"],
                        size_bytes=item.get("size", 0),
                        seeders=10,
                        source="zilean",
                    )
                    for item in raw_items
                ]

            async def check_availability(self, hashes: list[str]) -> dict[str, bool]:
                raise NotImplementedError

        adapter = ZileanFixtureAdapter()
        assert_conforms_to_scraper_protocol(adapter)
        results = await adapter.search(ScrapeRequest(query="Movie"))
        assert len(results) == len(zilean_data["search_success"])
        assert results[0].info_hash == "0123456789abcdef0123456789abcdef01234567"

    @pytest.mark.asyncio
    async def test_realdebrid_fixture_adapter_conformance(self) -> None:
        fixtures_dir = Path(__file__).parent / "fixtures"
        rd_data = json.loads(
            (fixtures_dir / "realdebrid_fixtures.json").read_text(encoding="utf-8")
        )

        class RealDebridFixtureAdapter:
            @property
            def manifest(self) -> ProviderManifest:
                return ProviderManifest(
                    id="realdebrid_fixture",
                    name="RealDebrid Fixture Downloader",
                    version="1.0.0",
                    category=ProviderCategory.DOWNLOADER,
                    capabilities=frozenset({ProviderCapability.DIRECT_DOWNLOAD}),
                )

            @property
            def is_enabled(self) -> bool:
                return True

            async def probe_health(self) -> ProviderHealthResult:
                profile = rd_data["user_profile_success"]
                return ProviderHealthResult(
                    status=ProviderHealthStatus.HEALTHY,
                    latency_ms=115.0,
                    message=f"User {profile['username']} ({profile['type']})",
                )

            async def check_availability(
                self, info_hashes: list[str]
            ) -> dict[str, bool]:
                avail = rd_data["instant_availability_success"]
                return {
                    h: (h in avail and bool(avail[h].get("rd"))) for h in info_hashes
                }

        adapter = RealDebridFixtureAdapter()
        assert_conforms_to_downloader_protocol(adapter)
        test_hash = "0123456789abcdef0123456789abcdef01234567"
        availability = await adapter.check_availability(
            [test_hash, "non_existent_hash"]
        )
        assert availability[test_hash] is True
        assert availability["non_existent_hash"] is False
