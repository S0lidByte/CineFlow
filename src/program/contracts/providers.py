"""PEP 544 provider protocols and Pydantic v2 schemas for CineFlow.

This module defines the strongly typed provider contract layer as an additive
architecture component.  It does **not** replace or intercept the existing
scraper pipeline (``ScraperService.scrape`` → ``parse_results`` → RTN ranking).

Protocols are structural (``@runtime_checkable``) so existing provider classes
can satisfy them without inheritance changes.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from program.contracts.telemetry import redact_sensitive_data
from program.utils.torrent import canonical_infohash

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class ProviderCategory(StrEnum):
    """Normalised provider categories matching CineFlow service directories."""

    SCRAPER = "scraper"
    DOWNLOADER = "downloader"
    INDEXER = "indexer"
    NOTIFIER = "notifier"
    SUBTITLE = "subtitle"


class ProviderCapability(StrEnum):
    """Discrete capabilities a provider may advertise.

    Values are grounded 1:1 against existing CineFlow service features:
    - ``torrent_scrape`` / ``stream_scrape``: scraper services
    - ``direct_download`` / ``debrid_stream``: downloader services
    - ``metadata_fetch``: indexer services (Overseerr, Mdblist, Trakt, TVDB)
    - ``subtitle_fetch``: subtitle services (OpenSubtitles, Subdl)
    - ``notification_send``: notification services (Apprise)
    - ``raw_search`` / ``categories_search``: Prowlarr indexer capabilities
    """

    TORRENT_SCRAPE = "torrent_scrape"
    STREAM_SCRAPE = "stream_scrape"
    DIRECT_DOWNLOAD = "direct_download"
    DEBRID_STREAM = "debrid_stream"
    METADATA_FETCH = "metadata_fetch"
    SUBTITLE_FETCH = "subtitle_fetch"
    NOTIFICATION_SEND = "notification_send"
    RAW_SEARCH = "raw_search"
    CATEGORIES_SEARCH = "categories_search"


class ProviderHealthStatus(StrEnum):
    """Tri-state health classification for provider probes."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ProviderManifest(BaseModel):
    """Immutable provider identity and capability declaration.

    Must never contain credentials or secret configuration.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, description="Unique provider identifier")
    name: str = Field(min_length=1, description="Human-readable provider name")
    version: str = Field(min_length=1, description="Semantic version string")
    category: ProviderCategory
    capabilities: frozenset[ProviderCapability] = Field(min_length=1)
    description: str = ""
    website: str = ""


class ProviderHealthResult(BaseModel):
    """Result of a provider health probe.

    The ``details`` mapping is automatically redacted through
    :func:`program.contracts.telemetry.redact_sensitive_data` during
    validation so that secrets never leak into health-check diagnostics.
    """

    model_config = ConfigDict(frozen=True)

    status: ProviderHealthStatus
    latency_ms: float = Field(ge=0.0)
    message: str = ""
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("details", mode="before")
    @classmethod
    def _redact_details(cls, v: Any) -> Any:
        if isinstance(v, dict):
            return redact_sensitive_data(v)
        return v


class ScrapeRequest(BaseModel):
    """Typed scrape request parameters (additive; does not replace legacy API)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str = Field(min_length=1)
    year: int | None = None
    imdb_id: str | None = None
    tmdb_id: str | None = None
    tvdb_id: str | None = None
    season: int | None = None
    episode: int | None = None
    is_anime: bool = False


class ScrapeCandidate(BaseModel):
    """A single scrape result with a canonicalised infohash.

    The ``info_hash`` field is validated and normalised to a 40-character
    lowercase hexadecimal string via
    :func:`program.utils.torrent.canonical_infohash`.  Both 40-char hex and
    32-char RFC 4648 Base32 inputs are accepted.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    raw_title: str = Field(min_length=1)
    info_hash: str = Field(min_length=40, max_length=40)
    indexer: str = ""
    size_bytes: int = Field(default=0, ge=0)
    seeders: int = Field(default=0, ge=0)
    source: str = ""

    @field_validator("info_hash", mode="before")
    @classmethod
    def _canonicalize_infohash(cls, v: Any) -> Any:
        if isinstance(v, str):
            result = canonical_infohash(v)
            if result is None:
                msg = f"Invalid infohash: cannot canonicalize '{v}'"
                raise ValueError(msg)
            return result
        return v


# ---------------------------------------------------------------------------
# PEP 544 structural protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class ProviderProtocol(Protocol):
    """Base structural protocol for all CineFlow providers."""

    @property
    def manifest(self) -> ProviderManifest: ...

    @property
    def is_enabled(self) -> bool: ...

    async def probe_health(self) -> ProviderHealthResult: ...


@runtime_checkable
class ScraperProviderProtocol(ProviderProtocol, Protocol):
    """Structural protocol for scraper providers."""

    async def search(self, request: ScrapeRequest) -> list[ScrapeCandidate]: ...


@runtime_checkable
class DownloaderProviderProtocol(ProviderProtocol, Protocol):
    """Structural protocol for downloader providers."""

    async def check_availability(self, info_hashes: list[str]) -> dict[str, bool]: ...
