"""Zilean scraper module."""

import asyncio
import time
from math import isfinite
from typing import Any, cast

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from program.contracts import (
    ProviderCapability,
    ProviderCategory,
    ProviderHealthResult,
    ProviderHealthStatus,
    ProviderManifest,
    ProviderRateLimitError,
    ScrapeCandidate,
    ScrapeRequest,
    normalize_provider_error,
)
from program.media.item import Episode, MediaItem, Season, Show
from program.services.scrapers.base import ScraperService
from program.settings import settings_manager
from program.settings.models import ZileanConfig
from program.utils.exceptions import RateLimitError
from program.utils.request import SmartSession, get_hostname_from_url
from program.utils.title_normalizer import sanitize_search_query_title
from program.utils.torrent import canonical_infohash


def _parse_retry_after(value: str | None) -> float | None:
    """Return a safe non-negative numeric Retry-After value, if supplied."""
    if value is None:
        return None
    try:
        retry_after = float(value)
    except (TypeError, ValueError):
        return None
    return retry_after if isfinite(retry_after) and retry_after >= 0 else None


class Params(BaseModel):
    model_config = ConfigDict(serialize_by_alias=True)

    query: str = Field(serialization_alias="Query")
    season: int | None = Field(default=None, serialization_alias="Season")
    episode: int | None = Field(default=None, serialization_alias="Episode")


class ZileanScrapeResponse(BaseModel):
    class ResultItem(BaseModel):
        raw_title: str | None = None
        info_hash: str | None = None

    data: list[ResultItem]


class Zilean(ScraperService[ZileanConfig]):
    """Scraper for `Zilean`"""

    def __init__(self):
        super().__init__()

        self.settings = settings_manager.settings.scraping.zilean
        self.timeout = self.settings.timeout

        self.session = SmartSession(
            rate_limits=(
                {
                    get_hostname_from_url(self.settings.url): {
                        "rate": 500 / 60,
                        "capacity": 500,
                    }
                }
                if self.settings.ratelimit
                else None
            ),
            retries=self.settings.retries,
            backoff_factor=0.3,
        )

        self._initialize()

    def validate(self) -> bool:
        """Validate the Zilean settings."""

        if not self.settings.enabled:
            return False

        if not self.settings.url:
            logger.error("Zilean URL is not configured and will not be used.")
            return False

        if self.timeout <= 0:
            logger.error("Zilean timeout must be a positive integer.")
            return False

        try:
            base = (self.settings.url or "").rstrip("/")
            url = f"{base}/healthchecks/ping"
            response = self.session.get(url, timeout=self.timeout)

            return response.ok
        except Exception as e:
            logger.error(f"Zilean failed to initialize: {e}")
            return False

    def run(self, item: MediaItem) -> dict[str, str]:
        """Scrape Zilean and preserve rate-limit scheduling information."""
        try:
            return self.scrape(item)
        except RateLimitError:
            raise
        except Exception as exc:
            norm_err = normalize_provider_error(exc, provider_name="Zilean")
            if isinstance(norm_err, ProviderRateLimitError):
                from requests import HTTPError

                retry_after = None
                if isinstance(exc, HTTPError) and exc.response is not None:
                    retry_after = _parse_retry_after(
                        exc.response.headers.get("Retry-After")
                    )
                raise RateLimitError(
                    "Zilean rate limit exceeded",
                    retry_after=retry_after,
                ) from exc
            logger.warning(f"{norm_err} for {item.log_string}")
        return {}

    def _build_query_params(self, item: MediaItem) -> Params:
        """Build the query params for the Zilean API"""

        query = sanitize_search_query_title(item.top_title)
        season = None
        episode = None

        if isinstance(item, Show):
            season = 1
        elif isinstance(item, Season):
            season = item.number
        elif isinstance(item, Episode):
            season = item.parent.number
            episode = item.number

        return Params(
            query=query,
            season=season,
            episode=episode,
        )

    def scrape(self, item: MediaItem) -> dict[str, str]:
        """Fetch and safely parse filtered Zilean DMM results."""
        base = (self.settings.url or "").rstrip("/")
        url = f"{base}/dmm/filtered"
        params = self._build_query_params(item)
        response = self.session.get(
            url,
            params=params.model_dump(exclude_none=True),
            timeout=self.timeout,
        )

        if response.status_code == 429:
            raise RateLimitError(
                "Zilean rate limit exceeded",
                retry_after=_parse_retry_after(response.headers.get("Retry-After")),
            )
        if not response.ok:
            logger.warning(
                "Zilean returned HTTP {} for {}", response.status_code, item.log_string
            )
            return {}

        try:
            payload: object = response.json()
        except ValueError:
            logger.warning("Zilean returned invalid JSON for {}", item.log_string)
            return {}
        if not isinstance(payload, list):
            logger.warning("Zilean returned a non-list payload for {}", item.log_string)
            return {}

        rows = cast(list[object], payload)
        torrents: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                result = ZileanScrapeResponse.ResultItem.model_validate(row)
            except ValidationError:
                continue
            info_hash = canonical_infohash(result.info_hash)
            if not result.raw_title or info_hash is None:
                continue
            torrents[info_hash] = result.raw_title

        if torrents:
            logger.log(
                "SCRAPER", f"Found {len(torrents)} streams for {item.log_string}"
            )
        else:
            logger.log("NOT_FOUND", f"No streams found for {item.log_string}")
        return torrents

    # -----------------------------------------------------------------------
    # ScraperProviderProtocol Implementation (Pilot Provider Bridge)
    # -----------------------------------------------------------------------

    @property
    def manifest(self) -> ProviderManifest:
        """Return the immutable ProviderManifest for Zilean."""
        return ProviderManifest(
            id="zilean",
            name="Zilean",
            version="1.0.0",
            category=ProviderCategory.SCRAPER,
            capabilities=frozenset({ProviderCapability.TORRENT_SCRAPE}),
            description="Zilean DMM (Debrid Media Manager) torrent scraper",
            website="https://github.com/iParr/zilean",
        )

    @property
    def is_enabled(self) -> bool:
        """Return whether Zilean scraping is enabled in configuration."""
        return bool(self.settings.enabled)

    async def probe_health(self) -> ProviderHealthResult:
        """Probe connectivity to Zilean healthcheck endpoint without blocking."""
        if not self.settings.url:
            return ProviderHealthResult(
                status=ProviderHealthStatus.UNHEALTHY,
                latency_ms=0.0,
                message="Zilean URL is not configured",
            )

        base = (self.settings.url or "").rstrip("/")
        url = f"{base}/healthchecks/ping"

        def _sync_ping() -> tuple[bool, int, float]:
            started = time.perf_counter()
            response = self.session.get(url, timeout=self.timeout)
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            return response.ok, response.status_code, max(0.0, elapsed_ms)

        try:
            ok, status_code, latency_ms = await asyncio.to_thread(_sync_ping)
            if ok:
                return ProviderHealthResult(
                    status=ProviderHealthStatus.HEALTHY,
                    latency_ms=latency_ms,
                    message="Connected to Zilean",
                    details={"status_code": status_code},
                )
            status = (
                ProviderHealthStatus.DEGRADED
                if status_code >= 500
                else ProviderHealthStatus.UNHEALTHY
            )
            return ProviderHealthResult(
                status=status,
                latency_ms=latency_ms,
                message=f"Zilean returned HTTP {status_code}",
                details={"status_code": status_code},
            )
        except Exception as exc:
            norm_err = normalize_provider_error(exc, provider_name="Zilean")
            return ProviderHealthResult(
                status=ProviderHealthStatus.UNHEALTHY,
                latency_ms=0.0,
                message=str(norm_err),
                details={"error": str(exc)},
            )

    async def search(self, request: ScrapeRequest) -> list[ScrapeCandidate]:
        """Typed async search executing against Zilean DMM filtered endpoint."""
        base = (self.settings.url or "").rstrip("/")
        url = f"{base}/dmm/filtered"
        params = Params(
            query=request.query,
            season=request.season,
            episode=request.episode,
        )

        def _sync_search() -> list[dict[str, Any]]:
            response = self.session.get(
                url,
                params=params.model_dump(exclude_none=True),
                timeout=self.timeout,
            )
            if response.status_code == 429:
                from requests import HTTPError

                http_err = HTTPError("429 Too Many Requests", response=response)
                raise normalize_provider_error(http_err, provider_name="Zilean")

            if not response.ok:
                from requests import HTTPError

                http_err = HTTPError(f"HTTP {response.status_code}", response=response)
                raise normalize_provider_error(http_err, provider_name="Zilean")

            try:
                payload: object = response.json()
            except ValueError as exc:
                raise normalize_provider_error(exc, provider_name="Zilean") from exc

            if not isinstance(payload, list):
                return []

            rows: list[dict[str, Any]] = []
            for item_obj in cast(list[object], payload):
                if isinstance(item_obj, dict):
                    rows.append(cast(dict[str, Any], item_obj))
            return rows

        try:
            raw_rows = await asyncio.to_thread(_sync_search)
        except Exception as exc:
            norm_err = normalize_provider_error(exc, provider_name="Zilean")
            raise norm_err from exc

        candidates: list[ScrapeCandidate] = []
        for row in raw_rows:
            try:
                item_model = ZileanScrapeResponse.ResultItem.model_validate(row)
            except ValidationError:
                continue

            if not item_model.raw_title or not item_model.info_hash:
                continue

            normalized_hash = canonical_infohash(item_model.info_hash)
            if normalized_hash is None:
                continue

            size_val = row.get("size") or row.get("size_bytes") or 0
            size_bytes = (
                int(size_val)
                if isinstance(size_val, (int, float)) and size_val > 0
                else 0
            )

            seeders_val = row.get("seeders") or row.get("seeds") or 0
            seeders = (
                int(seeders_val)
                if isinstance(seeders_val, (int, float)) and seeders_val > 0
                else 0
            )

            candidates.append(
                ScrapeCandidate(
                    raw_title=item_model.raw_title,
                    info_hash=normalized_hash,
                    indexer=str(row.get("indexer") or "dmm"),
                    size_bytes=size_bytes,
                    seeders=seeders,
                    source="zilean",
                )
            )

        return candidates
