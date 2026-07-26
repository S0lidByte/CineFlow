"""StremThru Torznab scraper module.

Mirrors the scrape path from riven-ts ``plugin-stremthru`` (Torznab JSON),
adapted to CineFlow's ``ScraperService`` pattern. Debrid/Torz store APIs are
intentionally out of scope — CineFlow uses its own downloaders.
"""

from loguru import logger
from pydantic import BaseModel, Field

from program.media.item import Episode, MediaItem, Movie, Season, Show
from program.services.scrapers.base import ScraperService
from program.settings import settings_manager
from program.settings.models import StremThruConfig
from program.utils.request import SmartSession, get_hostname_from_url
from program.utils.torrent import normalize_infohash


class TorznabAttr(BaseModel):
    """Single Torznab ``attr`` entry (JSON output mode)."""

    class Attributes(BaseModel):
        name: str
        value: str

    attributes: Attributes = Field(alias="@attributes")


class TorznabItem(BaseModel):
    title: str | None = None
    attr: list[TorznabAttr] = Field(default_factory=list[TorznabAttr])


class TorznabChannel(BaseModel):
    items: list[TorznabItem] = Field(default_factory=list[TorznabItem])


class TorznabResponse(BaseModel):
    channel: TorznabChannel = Field(default_factory=TorznabChannel)


class StremThru(ScraperService[StremThruConfig]):
    """Scraper for StremThru Torznab (``/v0/torznab/api``)."""

    def __init__(self):
        super().__init__()

        self.settings = settings_manager.settings.scraping.stremthru
        self.timeout = self.settings.timeout
        self.session = SmartSession(
            base_url=self.settings.url.rstrip("/") if self.settings.url else None,
            rate_limits=(
                {
                    get_hostname_from_url(self.settings.url): {
                        "rate": 300 / 60,
                        "capacity": 300,
                    }
                }
                if self.settings.ratelimit and self.settings.url
                else None
            ),
            retries=self.settings.retries,
            backoff_factor=0.3,
        )
        self._initialize()

    def validate(self) -> bool:
        """Validate StremThru settings (ping Torznab endpoint)."""

        if not self.settings.enabled:
            return False

        if not self.settings.url:
            logger.error("StremThru URL is not configured and will not be used.")
            return False

        if self.timeout <= 0:
            logger.error("StremThru timeout must be a positive integer.")
            return False

        try:
            # Same probe as riven-ts plugin-stremthru validate().
            response = self.session.get(
                "/v0/torznab/api",
                params={"t": "caps", "o": "json"},
                timeout=self.timeout,
            )
            return response.ok
        except Exception as e:
            logger.error(f"StremThru failed to initialize: {e}")
            return False

    def run(self, item: MediaItem) -> dict[str, str]:
        """Scrape StremThru for the given media item."""

        try:
            return self.scrape(item)
        except Exception as e:
            from requests import HTTPError

            if isinstance(e, HTTPError) and e.response is not None and e.response.status_code == 429:
                from program.utils.exceptions import RateLimitError

                retry_after = e.response.headers.get("Retry-After")
                raise RateLimitError(
                    "StremThru rate limit exceeded",
                    retry_after=int(retry_after) if retry_after else None,
                )
            if "rate limit" in str(e).lower() or "429" in str(e):
                from program.utils.exceptions import RateLimitError

                raise RateLimitError("StremThru rate limit exceeded")
            logger.exception(f"StremThru exception thrown: {e}")

        return {}

    def _build_params(self, item: MediaItem) -> dict[str, str]:
        """Build Torznab query params (aligned with plugin-stremthru scrape)."""

        params: dict[str, str] = {"o": "json"}

        imdb_id = item.get_top_imdb_id()
        if imdb_id:
            # StremThru accepts tt-prefixed or bare numeric; prefer as stored.
            params["imdbid"] = imdb_id
        else:
            params["q"] = item.top_title or item.log_string

        if isinstance(item, Movie):
            params["t"] = "movie"
            params["cat"] = "2000"
        else:
            params["t"] = "tvsearch"
            params["cat"] = "5000"

            if isinstance(item, Season):
                params["season"] = str(item.number)
            elif isinstance(item, Episode):
                params["season"] = str(item.parent.number)
                params["ep"] = str(item.number)
            elif isinstance(item, Show):
                params["season"] = "1"

        return params

    def scrape(self, item: MediaItem) -> dict[str, str]:
        """Query StremThru Torznab and return infohash → title map."""

        params = self._build_params(item)
        logger.debug(
            f"Searching StremThru for '{item.log_string}' "
            f"(imdb={params.get('imdbid') or 'n/a'}, t={params.get('t')})"
        )

        response = self.session.get(
            "/v0/torznab/api",
            params=params,
            timeout=self.timeout,
        )

        if not response.ok:
            logger.error(
                f"StremThru responded with status {response.status_code} "
                f"for {item.log_string}"
            )
            return {}

        try:
            data = TorznabResponse.model_validate(response.json())
        except Exception as e:
            logger.warning(
                f"Invalid StremThru Torznab response for {item.log_string}: {e}"
            )
            return {}

        torrents: dict[str, str] = {}
        for result in data.channel.items:
            if not result.title:
                continue

            infohash = None
            for attr in result.attr:
                if attr.attributes.name.lower() == "infohash":
                    infohash = normalize_infohash(attr.attributes.value)
                    break

            if infohash:
                torrents[infohash] = result.title

        if torrents:
            logger.log(
                "SCRAPER", f"Found {len(torrents)} streams for {item.log_string}"
            )
        else:
            logger.log("NOT_FOUND", f"No streams found for {item.log_string}")

        return torrents
