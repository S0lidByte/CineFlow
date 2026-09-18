"""Test suite for Phase 4.3 TASK-001: Core PEP 544 Provider Protocols & Pydantic Models.

Covers:
- Structural subtyping and runtime checkability of provider protocols
- Pydantic v2 model validation, constraints, and immutability
- Recursive sensitive-data redaction in health results
- Canonical infohash normalization (40-char hex and 32-char Base32)
- Mandatory Dual-Layer RTN Non-Regression Guard:
    * Layer A: Static contract & call-path integrity check
    * Layer B: Live functional execution of parse_results() with RTN ranking
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

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
from program.media.stream import Stream
from program.services.scrapers.base import ScraperService
from program.services.scrapers.shared import parse_results
from program.settings import settings_manager

# ---------------------------------------------------------------------------
# Fixtures & Dummy Implementations for Protocol Testing
# ---------------------------------------------------------------------------

SAMPLE_MANIFEST = ProviderManifest(
    id="test_scraper",
    name="Test Scraper",
    version="1.0.0",
    category=ProviderCategory.SCRAPER,
    capabilities=frozenset({ProviderCapability.TORRENT_SCRAPE}),
    description="A test scraper implementation",
    website="https://example.com",
)


class ConformingBaseProvider:
    def __init__(self, manifest: ProviderManifest = SAMPLE_MANIFEST) -> None:
        self._manifest = manifest
        self._is_enabled = True

    @property
    def manifest(self) -> ProviderManifest:
        return self._manifest

    @property
    def is_enabled(self) -> bool:
        return self._is_enabled

    async def probe_health(self) -> ProviderHealthResult:
        return ProviderHealthResult(
            status=ProviderHealthStatus.HEALTHY,
            latency_ms=12.5,
            message="OK",
        )


class ConformingScraper(ConformingBaseProvider):
    async def search(self, request: ScrapeRequest) -> list[ScrapeCandidate]:
        return [
            ScrapeCandidate(
                raw_title=f"{request.query} 1080p BluRay",
                info_hash="a" * 40,
                indexer="test_indexer",
                size_bytes=1024 * 1024 * 1024,
                seeders=42,
            )
        ]


class ConformingDownloader(ConformingBaseProvider):
    async def check_availability(self, info_hashes: list[str]) -> dict[str, bool]:
        return {h: True for h in info_hashes}


class IncompleteProvider:
    """Missing manifest and probe_health."""

    @property
    def is_enabled(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# Test Category & Capability Enums
# ---------------------------------------------------------------------------


def test_provider_category_values() -> None:
    assert ProviderCategory.SCRAPER == "scraper"
    assert ProviderCategory.DOWNLOADER == "downloader"
    assert ProviderCategory.INDEXER == "indexer"
    assert ProviderCategory.NOTIFIER == "notifier"
    assert ProviderCategory.SUBTITLE == "subtitle"
    assert len(ProviderCategory) == 5


def test_provider_capability_values() -> None:
    assert ProviderCapability.TORRENT_SCRAPE == "torrent_scrape"
    assert ProviderCapability.STREAM_SCRAPE == "stream_scrape"
    assert ProviderCapability.DIRECT_DOWNLOAD == "direct_download"
    assert ProviderCapability.DEBRID_STREAM == "debrid_stream"
    assert ProviderCapability.METADATA_FETCH == "metadata_fetch"
    assert ProviderCapability.SUBTITLE_FETCH == "subtitle_fetch"
    assert ProviderCapability.NOTIFICATION_SEND == "notification_send"
    assert ProviderCapability.RAW_SEARCH == "raw_search"
    assert ProviderCapability.CATEGORIES_SEARCH == "categories_search"
    assert len(ProviderCapability) == 9


def test_provider_health_status_values() -> None:
    assert ProviderHealthStatus.HEALTHY == "healthy"
    assert ProviderHealthStatus.DEGRADED == "degraded"
    assert ProviderHealthStatus.UNHEALTHY == "unhealthy"
    assert len(ProviderHealthStatus) == 3


# ---------------------------------------------------------------------------
# Test ProviderManifest
# ---------------------------------------------------------------------------


def test_provider_manifest_valid() -> None:
    manifest = ProviderManifest(
        id="torrentio",
        name="Torrentio",
        version="1.2.3",
        category=ProviderCategory.SCRAPER,
        capabilities=frozenset(
            {ProviderCapability.TORRENT_SCRAPE, ProviderCapability.STREAM_SCRAPE}
        ),
        description="Torrentio scraper",
        website="https://torrentio.strem.fun",
    )
    assert manifest.id == "torrentio"
    assert manifest.category == ProviderCategory.SCRAPER
    assert ProviderCapability.TORRENT_SCRAPE in manifest.capabilities


def test_provider_manifest_frozen_immutability() -> None:
    manifest = SAMPLE_MANIFEST
    with pytest.raises(ValidationError):
        manifest.name = "Mutated Name"  # type: ignore[misc]


def test_provider_manifest_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError) as exc_info:
        ProviderManifest(
            id="test",
            name="Test",
            version="1.0.0",
            category=ProviderCategory.SCRAPER,
            capabilities=frozenset({ProviderCapability.TORRENT_SCRAPE}),
            api_key="secret_key_that_should_not_be_in_manifest",  # type: ignore[call-arg]
        )
    assert "extra_forbidden" in str(exc_info.value)


def test_provider_manifest_empty_capabilities_rejected() -> None:
    with pytest.raises(ValidationError):
        ProviderManifest(
            id="test",
            name="Test",
            version="1.0.0",
            category=ProviderCategory.SCRAPER,
            capabilities=frozenset(),
        )


# ---------------------------------------------------------------------------
# Test ProviderHealthResult & Automatic Redaction
# ---------------------------------------------------------------------------


def test_provider_health_result_basic() -> None:
    res = ProviderHealthResult(
        status=ProviderHealthStatus.HEALTHY,
        latency_ms=45.2,
        message="Service reachable",
        details={"ping": "pong", "count": 10},
    )
    assert res.status == ProviderHealthStatus.HEALTHY
    assert res.latency_ms == 45.2
    assert res.details == {"ping": "pong", "count": 10}


def test_provider_health_result_negative_latency_rejected() -> None:
    with pytest.raises(ValidationError):
        ProviderHealthResult(
            status=ProviderHealthStatus.HEALTHY,
            latency_ms=-1.0,
        )


def test_provider_health_result_redacts_sensitive_details() -> None:
    raw_details: dict[str, Any] = {
        "api_key": "super_secret_token_12345",
        "nested": {
            "password": "mypassword",
            "token": "bearer_xyz",
            "safe_metric": 42,
        },
        "safe_key": "safe_value",
    }
    res = ProviderHealthResult(
        status=ProviderHealthStatus.HEALTHY,
        latency_ms=10.0,
        details=raw_details,
    )
    assert res.details["api_key"] == REDACTED_SUBSTITUTE
    assert res.details["nested"]["password"] == REDACTED_SUBSTITUTE
    assert res.details["nested"]["token"] == REDACTED_SUBSTITUTE
    assert res.details["nested"]["safe_metric"] == 42
    assert res.details["safe_key"] == "safe_value"


# ---------------------------------------------------------------------------
# Test ScrapeRequest
# ---------------------------------------------------------------------------


def test_scrape_request_valid() -> None:
    req = ScrapeRequest(
        query="Inception",
        year=2010,
        imdb_id="tt1375666",
        is_anime=False,
    )
    assert req.query == "Inception"
    assert req.year == 2010
    assert req.imdb_id == "tt1375666"
    assert req.is_anime is False


def test_scrape_request_frozen_and_forbids_extra() -> None:
    req = ScrapeRequest(query="Movie")
    with pytest.raises(ValidationError):
        req.query = "Changed"  # type: ignore[misc]

    with pytest.raises(ValidationError):
        ScrapeRequest(query="Movie", extra_field=123)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Test ScrapeCandidate & Canonical Infohash Validation
# ---------------------------------------------------------------------------


def test_scrape_candidate_hex_infohash() -> None:
    candidate = ScrapeCandidate(
        raw_title="Inception 2010 1080p BluRay x264",
        info_hash="A1B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5F6A1B2",
        indexer="torrentio",
        size_bytes=2_000_000_000,
        seeders=15,
        source="p2p",
    )
    # Canonicalized to lowercase 40-char hex
    assert candidate.info_hash == "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
    assert candidate.size_bytes == 2_000_000_000
    assert candidate.seeders == 15


def test_scrape_candidate_base32_infohash_canonicalized() -> None:
    # 32-char RFC 4648 Base32 representing 20 bytes
    b32_hash = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"  # 32 chars
    candidate = ScrapeCandidate(
        raw_title="Test Movie 2024",
        info_hash=b32_hash,
    )
    # Must be canonicalized to exact 40-char hex
    assert len(candidate.info_hash) == 40
    assert all(c in "0123456789abcdef" for c in candidate.info_hash)


def test_scrape_candidate_invalid_infohash_rejected() -> None:
    # Invalid length
    with pytest.raises(ValidationError):
        ScrapeCandidate(raw_title="Test", info_hash="tooshort")

    # Invalid characters
    with pytest.raises(ValidationError):
        ScrapeCandidate(raw_title="Test", info_hash="Z" * 40)

    # Empty raw_title
    with pytest.raises(ValidationError):
        ScrapeCandidate(raw_title="", info_hash="a" * 40)


# ---------------------------------------------------------------------------
# Test Structural Protocol Conformance (PEP 544 @runtime_checkable)
# ---------------------------------------------------------------------------


def test_provider_protocol_isinstance() -> None:
    provider = ConformingBaseProvider()
    assert isinstance(provider, ProviderProtocol)
    assert not isinstance(provider, ScraperProviderProtocol)
    assert not isinstance(provider, DownloaderProviderProtocol)


def test_scraper_provider_protocol_isinstance() -> None:
    scraper = ConformingScraper()
    assert isinstance(scraper, ProviderProtocol)
    assert isinstance(scraper, ScraperProviderProtocol)
    assert not isinstance(scraper, DownloaderProviderProtocol)


def test_downloader_provider_protocol_isinstance() -> None:
    downloader = ConformingDownloader()
    assert isinstance(downloader, ProviderProtocol)
    assert isinstance(downloader, DownloaderProviderProtocol)
    assert not isinstance(downloader, ScraperProviderProtocol)


def test_non_conforming_class_rejected() -> None:
    incomplete = IncompleteProvider()
    assert not isinstance(incomplete, ProviderProtocol)
    assert not isinstance(incomplete, ScraperProviderProtocol)
    assert not isinstance(incomplete, DownloaderProviderProtocol)


# ---------------------------------------------------------------------------
# Mandatory Dual-Layer RTN Non-Regression Guard
# ---------------------------------------------------------------------------


def test_rtn_layer_a_static_contract_integrity() -> None:
    """Layer A: Verify legacy ScraperService contract and parse_results signature."""
    # 1. ScraperService.scrape must accept (self, item: MediaItem) and return dict[str, str]
    scrape_sig = inspect.signature(ScraperService.scrape)
    assert "item" in scrape_sig.parameters
    assert ScraperService.requires_imdb_id is False

    # 2. parse_results signature must remain intact
    parse_sig = inspect.signature(parse_results)
    params = list(parse_sig.parameters.keys())
    assert params[:2] == ["item", "results"]
    assert "manual" in params
    assert "funnel" in params

    # 3. Verify parse_results returns dict[str, Stream]
    return_annotation = parse_sig.return_annotation
    # In stringified or direct type form
    assert "Stream" in str(return_annotation) or return_annotation == dict[str, Stream]


def test_rtn_layer_b_functional_execution_ranking_and_trash_rejection() -> None:
    """Layer B: Real functional execution of parse_results() with RTN ranking.

    Verifies:
    1. RTN parses release metadata from raw release titles
    2. Valid candidates survive and are ranked
    3. Garbage/mismatched titles are rejected
    4. Valid Stream objects are produced with rank, lev_ratio, resolution
    """
    item = SimpleNamespace(
        top_title="Inception",
        log_string="Inception (2010)",
        country=None,
        is_anime=False,
        aired_at=None,
        get_aliases=dict,
    )

    valid_hash_1 = "1111111111111111111111111111111111111111"
    valid_hash_2 = "2222222222222222222222222222222222222222"
    garbage_hash = "3333333333333333333333333333333333333333"

    results = {
        # Valid 1080p BluRay matching Inception
        valid_hash_1: "Inception 2010 1080p BluRay x264-SPARKS",
        # Valid 720p WEB-DL matching Inception
        valid_hash_2: "Inception.2010.720p.WEB-DL.H264-GROUP",
        # Garbage torrent: completely wrong title and year, should be rejected
        garbage_hash: "Completely Wrong Movie 1999 480p CAM-TRASH",
    }

    with settings_manager.override(languages={"required": []}):
        streams = parse_results(item, results, manual=False)  # type: ignore[arg-type]

    # Valid releases must be present
    assert valid_hash_1 in streams
    assert valid_hash_2 in streams

    # Garbage release must be rejected by RTN
    assert garbage_hash not in streams

    # Verify Stream model structure on parsed results
    stream_1 = streams[valid_hash_1]
    assert isinstance(stream_1, Stream)
    assert stream_1.infohash == valid_hash_1
    assert stream_1.resolution == "1080p"
    assert stream_1.parsed_title.lower() == "inception"
    assert stream_1.rank is not None
    assert stream_1.lev_ratio > 0.8
