"""Zilean scraper module."""

from math import isfinite
from typing import cast

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from program.media.item import Episode, MediaItem, Season, Show
from program.services.scrapers.base import ScraperService
from program.settings import settings_manager
from program.settings.models import ZileanConfig
from program.utils.exceptions import RateLimitError
from program.utils.request import SmartSession, get_hostname_from_url
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
            from requests import HTTPError

            if (
                isinstance(exc, HTTPError)
                and exc.response is not None
                and exc.response.status_code == 429
            ):
                raise RateLimitError(
                    "Zilean rate limit exceeded",
                    retry_after=_parse_retry_after(
                        exc.response.headers.get("Retry-After")
                    ),
                ) from exc
            logger.exception("Zilean exception thrown for {}", item.log_string)
        return {}

    def _build_query_params(self, item: MediaItem) -> Params:
        """Build the query params for the Zilean API"""

        query = item.top_title
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
            logger.log("SCRAPER", f"Found {len(torrents)} streams for {item.log_string}")
        else:
            logger.log("NOT_FOUND", f"No streams found for {item.log_string}")
        return torrents
