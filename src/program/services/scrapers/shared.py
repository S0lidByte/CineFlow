"""Shared functions for scrapers."""

from datetime import datetime
from typing import cast

from loguru import logger
from RTN import (
    RTN,
    BaseRankingModel,
    DefaultRanking,
    ParsedData,
    Torrent,
    parse,
    sort_torrents,
)
from RTN.exceptions import GarbageTorrent
from RTN.models import SettingsModel

from program.media.item import Episode, MediaItem, Movie, Season, Show
from program.media.stream import Stream
from program.settings import settings_manager
from program.settings.models import RTNSettingsModel, ScraperModel

scraping_settings: ScraperModel = settings_manager.settings.scraping
ranking_settings: RTNSettingsModel = settings_manager.settings.ranking
ranking_model: BaseRankingModel = DefaultRanking()
rtn = RTN(ranking_settings, ranking_model)

RTN_LANGUAGE_GROUPS = {"anime", "non_anime", "common", "all"}
RTN_LANGUAGE_ALIASES = {
    "eng": "en",
    "english": "en",
    "jpn": "ja",
    "japanese": "ja",
    "jp": "ja",
    "chi": "zh",
    "zho": "zh",
    "chinese": "zh",
    "kor": "ko",
    "korean": "ko",
    "fre": "fr",
    "fra": "fr",
    "french": "fr",
    "ger": "de",
    "deu": "de",
    "german": "de",
    "spa": "es",
    "spanish": "es",
    "por": "pt",
    "portuguese": "pt",
    "ita": "it",
    "italian": "it",
    "rus": "ru",
    "russian": "ru",
}


def _normalize_rtn_language(language: str) -> str:
    normalized = language.strip().lower().replace("_", "-")
    if not normalized:
        return normalized
    if normalized in RTN_LANGUAGE_GROUPS:
        return normalized
    if "-" in normalized:
        normalized = normalized.split("-", 1)[0]
    if normalized in RTN_LANGUAGE_ALIASES:
        return RTN_LANGUAGE_ALIASES[normalized]
    return normalized


def _normalize_rtn_language_list(languages: list[str]) -> list[str]:
    normalized_languages = list[str]()
    seen = set[str]()

    for language in languages:
        normalized = _normalize_rtn_language(language)
        if normalized and normalized not in seen:
            normalized_languages.append(normalized)
            seen.add(normalized)

    return normalized_languages


def _normalize_rtn_language_settings(settings: SettingsModel) -> None:
    settings.languages.required = _normalize_rtn_language_list(
        settings.languages.required
    )
    settings.languages.allowed = _normalize_rtn_language_list(
        settings.languages.allowed
    )
    settings.languages.exclude = _normalize_rtn_language_list(
        settings.languages.exclude
    )
    settings.languages.preferred = _normalize_rtn_language_list(
        settings.languages.preferred
    )


def _should_retry_as_untagged_english(
    error: GarbageTorrent, settings: SettingsModel, raw_title: str
) -> bool:
    if "missing_required_language" not in str(error):
        return False

    if not settings.options.get("allow_english_in_languages", True):
        return False

    if "en" not in set(_normalize_rtn_language_list(settings.languages.required)):
        return False

    try:
        return not parse(raw_title).languages
    except Exception:
        return False


def _rank_with_language_compat(
    rtn_instance: RTN,
    settings: SettingsModel,
    *,
    raw_title: str,
    infohash: str,
    correct_title: str,
    remove_trash: bool,
    aliases: dict[str, list[str]],
) -> Torrent:
    try:
        return rtn_instance.rank(
            raw_title=raw_title,
            infohash=infohash,
            correct_title=correct_title,
            remove_trash=remove_trash,
            aliases=aliases,
        )
    except GarbageTorrent as e:
        if not _should_retry_as_untagged_english(e, settings, raw_title):
            raise

        relaxed_settings = settings.model_copy(deep=True)
        relaxed_settings.languages.required = []
        relaxed_rtn = RTN(relaxed_settings, ranking_model)
        logger.trace(
            f"Treating untagged release as English for language-required ranking: {raw_title}"
        )
        return relaxed_rtn.rank(
            raw_title=raw_title,
            infohash=infohash,
            correct_title=correct_title,
            remove_trash=remove_trash,
            aliases=aliases,
        )


def get_ranking_overrides(
    ranking_overrides: dict[str, list[str]] | None,
) -> SettingsModel | None:
    if not ranking_overrides:
        return None

    try:
        # Create a deep copy of current settings
        settings_model = RTNSettingsModel(**ranking_settings.model_dump())

        # Collect groups: resolutions + all custom rank categories
        groups = [("resolutions", settings_model.resolutions)]
        if hasattr(settings_model.custom_ranks, "__class__"):
            groups.extend(
                (cat, val)
                for cat in settings_model.custom_ranks.__class__.model_fields
                if (val := getattr(settings_model.custom_ranks, cat)) is not None
            )

        for category, obj in groups:
            if category not in ranking_overrides:
                continue

            if not obj.__class__.model_fields:
                continue

            targets = set(ranking_overrides[category])

            # Iterate fields (assuming Pydantic model)
            for key in obj.__class__.model_fields:
                if key == "unknown":
                    continue

                should_enable = key in targets
                val = getattr(obj, key)

                if isinstance(val, bool):
                    setattr(obj, key, should_enable)
                elif hasattr(val, "fetch"):
                    val.fetch = should_enable

        return settings_model
    except Exception as e:
        logger.error(f"Failed to apply ranking overrides: {e}")
        return None


def parse_results(
    item: MediaItem,
    results: dict[str, str],
    log_msg: bool = True,
    manual: bool = False,
) -> dict[str, Stream]:
    """Parse the results from the scrapers into Torrent objects.

    Args:
        item: The media item to parse results for.
        results: Dict mapping infohash to raw title.
        manual: If True, bypass content filters (for manual scraping).
    """

    _ = log_msg
    torrents = set[Torrent]()
    processed_infohashes = set[str]()
    correct_title = item.top_title

    # Use effective RTN settings (handles explicit overrides/context implicitly)
    active_settings = settings_manager.get_effective_rtn_model()
    _normalize_rtn_language_settings(active_settings)

    # Check if we are diverging from the global singleton `rtn` instance
    is_default_settings = active_settings.model_dump() == ranking_settings.model_dump()
    rtn_instance = rtn if is_default_settings else RTN(active_settings, ranking_model)

    aliases = (
        {k: v for k, v in a.items() if k not in active_settings.languages.exclude}
        if scraping_settings.enable_aliases and (a := item.get_aliases())
        else {}
    )

    logger.debug(f"Processing {len(results)} results for {item.log_string}")

    for infohash, raw_title in results.items():
        if infohash in processed_infohashes:
            continue

        try:
            torrent = _rank_with_language_compat(
                rtn_instance,
                active_settings,
                raw_title=raw_title,
                infohash=infohash,
                correct_title=correct_title,
                remove_trash=(
                    active_settings.options["remove_all_trash"] if not manual else False
                ),
                aliases=aliases,
            )

            # If movie item, disregard torrents with seasons and episodes
            if (
                isinstance(item, Movie)
                and not manual
                and (torrent.data.episodes or torrent.data.seasons)
            ):
                logger.trace(
                    f"Skipping show torrent for movie {item.log_string}: {raw_title}"
                )
                continue

            if isinstance(item, Show):
                # make sure the torrent has at least 2 episodes (should weed out most junk)
                if (
                    not manual
                    and torrent.data.episodes
                    and len(torrent.data.episodes) <= 2
                ):
                    logger.trace(
                        f"Skipping torrent with too few episodes for {item.log_string}: {raw_title}"
                    )
                    continue

                # make sure all of the item seasons are present in the torrent
                if not manual and not all(
                    season.number in torrent.data.seasons for season in item.seasons
                ):
                    logger.trace(
                        f"Skipping torrent with incorrect number of seasons for {item.log_string}: {raw_title}"
                    )
                    continue

                if (
                    not manual
                    and torrent.data.episodes
                    and not torrent.data.seasons
                    and len(item.seasons) == 1
                    and not all(
                        episode.number in torrent.data.episodes
                        for episode in item.seasons[0].episodes
                    )
                ):
                    logger.trace(
                        f"Skipping torrent with incorrect number of episodes for {item.log_string}: {raw_title}"
                    )
                    continue

            if isinstance(item, Season):
                if (
                    not manual
                    and torrent.data.seasons
                    and item.number not in torrent.data.seasons
                ):
                    logger.trace(
                        f"Skipping torrent with no seasons or incorrect season number for {item.log_string}: {raw_title}"
                    )
                    continue

                # make sure the torrent has at least 2 episodes (should weed out most junk)
                if (
                    not manual
                    and torrent.data.episodes
                    and len(torrent.data.episodes) <= 2
                ):
                    logger.trace(
                        f"Skipping torrent with too few episodes for {item.log_string}: {raw_title}"
                    )
                    continue

                # disregard torrents with incorrect season number
                if not manual and item.number not in torrent.data.seasons:
                    logger.trace(
                        f"Skipping incorrect season torrent for {item.log_string}: {raw_title}"
                    )
                    continue

                if (
                    not manual
                    and torrent.data.episodes
                    and not all(
                        episode.number in torrent.data.episodes
                        for episode in item.episodes
                    )
                ):
                    logger.trace(
                        f"Skipping incorrect season torrent for not having all episodes {item.log_string}: {raw_title}"
                    )
                    continue

            if isinstance(item, Episode) and not manual:
                # Disregard torrents with incorrect episode number logic:
                skip = False

                # If the torrent has episodes, but the episode number is not present
                if torrent.data.episodes:
                    if (
                        item.number not in torrent.data.episodes
                        and item.absolute_number not in torrent.data.episodes
                    ):
                        skip = True

                # If the torrent does not have episodes, but has seasons, and the parent season is not present
                elif torrent.data.seasons:
                    # item is confirmed to be Episode at line 197
                    # Episode.parent is a Season, and Season has a 'number' attribute
                    parent_season = cast(Season, item.parent)
                    if parent_season.number not in torrent.data.seasons:
                        skip = True

                # If the torrent has neither episodes nor seasons, skip (junk)
                else:
                    skip = True

                if skip:
                    logger.trace(
                        f"Skipping incorrect episode torrent for {item.log_string}: {raw_title}"
                    )
                    continue

            # If country is present, then check to make sure it's correct. (Covers: US, UK, NZ, AU)
            if (
                not manual
                and torrent.data.country
                and not item.is_anime
                and (item_country := _get_item_country(item))
                and torrent.data.country not in item_country
            ):
                logger.trace(
                    f"Skipping torrent for incorrect country with {item.log_string}: {raw_title}"
                )
                continue

            if (
                not manual
                and torrent.data.year
                and item.aired_at
                and not _check_item_year(item.aired_at, torrent.data)
            ):
                # If year is present, then check to make sure it's correct
                logger.trace(
                    f"Skipping torrent for incorrect year with {item.log_string}: {raw_title}"
                )
                continue

            # If anime and user wants dubbed only, then check to make sure it's dubbed
            if (
                not manual
                and item.is_anime
                and scraping_settings.dubbed_anime_only
                and not torrent.data.dubbed
            ):
                logger.trace(
                    f"Skipping non-dubbed anime torrent for {item.log_string}: {raw_title}"
                )
                continue

            torrents.add(torrent)
            processed_infohashes.add(infohash)
        except Exception as e:
            logger.debug(f"RTN rejected '{raw_title[:60]}': {type(e).__name__}: {e}")
            processed_infohashes.add(infohash)
            continue

    if torrents:
        logger.debug(f"Found {len(torrents)} streams for {item.log_string}")

        sorted_torrents = sort_torrents(
            torrents,
            bucket_limit=scraping_settings.bucket_limit if not manual else 0,
        )

        torrent_stream_map = {
            torrent.infohash.lower(): Stream(torrent)
            for torrent in sorted_torrents.values()
        }

        logger.debug(
            f"Kept {len(torrent_stream_map)} streams for {item.log_string} after processing bucket limit"
        )

        return torrent_stream_map

    return {}


# helper functions


def _check_item_year(aired_at: datetime, data: ParsedData) -> bool:
    """Check if the year of the torrent is within the range of the item."""

    return data.year in [
        aired_at.year - 1,
        aired_at.year,
        aired_at.year + 1,
    ]


def _get_item_country(item: MediaItem) -> str | None:
    """Get the country code for a country."""

    country = None

    if isinstance(item, Season) and item.parent.country:
        country = item.parent.country.upper()
    elif isinstance(item, Episode) and item.parent.parent.country:
        country = item.parent.parent.country.upper()
    elif item.country:
        country = item.country.upper()

    if not country:
        return None

    # need to normalize
    if country == "USA":
        country = "US"
    elif country == "GB":
        country = "UK"

    return country
