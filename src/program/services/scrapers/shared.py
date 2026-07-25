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
from program.services.scrapers.funnel import ScrapeFunnelStats
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


def normalize_rtn_language_settings(settings: SettingsModel) -> None:
    """Public wrapper for RTN language code normalization."""
    _normalize_rtn_language_settings(settings)


def _item_is_anime(item: MediaItem | object) -> bool:
    return bool(getattr(item, "is_anime", False))


def _scraping_settings() -> ScraperModel:
    """Live scraping settings (avoid stale import-time snapshot)."""

    return settings_manager.settings.scraping


def _title_looks_multi_or_dual_audio(raw_title: str) -> bool:
    """Heuristic for MULTI / dual-audio releases (anime soft-opt-in)."""

    lowered = raw_title.lower()
    tokens = (
        "multi",
        "dual-audio",
        "dual audio",
        "dualaudio",
        "dual.audio",
    )
    if any(token in lowered for token in tokens):
        return True

    try:
        languages = parse(raw_title).languages or []
    except Exception:
        return False

    return len({lang.lower() for lang in languages}) >= 2


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


def _should_retry_as_multi_audio_for_anime(
    error: GarbageTorrent,
    *,
    item: MediaItem | object | None,
    raw_title: str,
) -> bool:
    if item is None or not _item_is_anime(item):
        return False
    if not _scraping_settings().anime_allow_multi_audio:
        return False
    if "missing_required_language" not in str(error):
        return False
    return _title_looks_multi_or_dual_audio(raw_title)


def _apply_anime_extras_dubbed_soft_opt_in(
    item: MediaItem | object, settings: SettingsModel
) -> SettingsModel:
    """Optionally enable extras.dubbed.fetch for anime items only."""

    if not _item_is_anime(item):
        return settings
    if not _scraping_settings().anime_allow_extras_dubbed:
        return settings

    dubbed = settings.custom_ranks.extras.dubbed
    if getattr(dubbed, "fetch", True):
        return settings

    relaxed = settings.model_copy(deep=True)
    relaxed.custom_ranks.extras.dubbed.fetch = True
    logger.debug(
        "Anime ranking soft-opt-in: enabling extras.dubbed.fetch for "
        f"{getattr(item, 'log_string', item)}"
    )
    return relaxed


def _rank_with_language_compat(
    rtn_instance: RTN,
    settings: SettingsModel,
    *,
    raw_title: str,
    infohash: str,
    correct_title: str,
    remove_trash: bool,
    aliases: dict[str, list[str]],
    item: MediaItem | object | None = None,
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
        retry_untagged = _should_retry_as_untagged_english(e, settings, raw_title)
        retry_multi = _should_retry_as_multi_audio_for_anime(
            e, item=item, raw_title=raw_title
        )
        if not retry_untagged and not retry_multi:
            raise

        relaxed_settings = settings.model_copy(deep=True)
        relaxed_settings.languages.required = []
        relaxed_rtn = RTN(relaxed_settings, ranking_model)
        if retry_multi:
            logger.trace(
                "Anime ranking soft-opt-in: treating MULTI/dual-audio as "
                f"language-compatible: {raw_title}"
            )
        else:
            logger.trace(
                "Treating untagged release as English for language-required "
                f"ranking: {raw_title}"
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


def episode_release_matches(
    *,
    episode_number: int,
    absolute_number: int | None,
    season_number: int,
    parsed_episodes: list[int] | None,
    parsed_seasons: list[int] | None,
) -> bool:
    """Return True when a parsed release matches this episode's identity.

    Relative episode numbers (E14) require a matching parent season tag so
    S08E14 cannot match S06E14. Absolute-number matches (anime) may omit
    season tags. Season packs without episode lists are allowed when they
    contain the parent season.
    """

    episodes = parsed_episodes or []
    seasons = parsed_seasons or []

    if episodes:
        # Relative E## requires an explicit matching season tag.
        # Absolute match is evaluated independently so E1/abs=1 without a
        # season tag can still match anime-style absolute numbering.
        relative_ok = (
            episode_number in episodes and bool(seasons) and season_number in seasons
        )
        absolute_ok = (
            absolute_number is not None
            and absolute_number in episodes
            and (not seasons or season_number in seasons)
        )
        return relative_ok or absolute_ok

    if seasons:
        return season_number in seasons

    return False


def _prepare_rtn_ranking_context(
    item: MediaItem,
) -> tuple[RTN, SettingsModel, str, dict[str, list[str]]]:
    """Build RTN instance, settings, title, and aliases for ranking."""

    correct_title = item.top_title
    active_settings = settings_manager.get_effective_rtn_model()
    _normalize_rtn_language_settings(active_settings)
    active_settings = _apply_anime_extras_dubbed_soft_opt_in(item, active_settings)

    is_default_settings = active_settings.model_dump() == ranking_settings.model_dump()
    rtn_instance = rtn if is_default_settings else RTN(active_settings, ranking_model)

    aliases = (
        {k: v for k, v in a.items() if k not in active_settings.languages.exclude}
        if scraping_settings.enable_aliases and (a := item.get_aliases())
        else {}
    )

    return rtn_instance, active_settings, correct_title, aliases


def _streams_from_torrents(
    item: MediaItem,
    torrents: set[Torrent],
    *,
    manual: bool = False,
    log_msg: bool = True,
) -> dict[str, Stream]:
    """Sort accumulated torrents and map them to Stream objects."""

    if not torrents:
        return {}

    if log_msg:
        logger.debug(f"Found {len(torrents)} streams for {item.log_string}")

    sorted_torrents = sort_torrents(
        torrents,
        bucket_limit=scraping_settings.bucket_limit if not manual else 0,
    )

    torrent_stream_map = {
        torrent.infohash.lower(): Stream(torrent)
        for torrent in sorted_torrents.values()
    }

    if log_msg:
        logger.debug(
            f"Kept {len(torrent_stream_map)} streams for {item.log_string} "
            f"after processing bucket limit"
        )

    return torrent_stream_map


def _accumulate_ranked_torrents(
    item: MediaItem,
    results: dict[str, str],
    torrents: set[Torrent],
    processed_infohashes: set[str],
    *,
    manual: bool = False,
    log_msg: bool = True,
    funnel: ScrapeFunnelStats | None = None,
) -> None:
    """Rank and filter scraper results into ``torrents`` (mutates in place)."""

    if not results:
        return

    rtn_instance, active_settings, correct_title, aliases = (
        _prepare_rtn_ranking_context(item)
    )

    if log_msg:
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
                item=item,
            )
        except Exception as e:
            logger.debug(f"RTN rejected '{raw_title[:60]}': {type(e).__name__}: {e}")
            if funnel is not None:
                funnel.record_rtn_reject(e)
            processed_infohashes.add(infohash)
            continue

        # If movie item, disregard torrents with seasons and episodes
        if (
            isinstance(item, Movie)
            and not manual
            and (torrent.data.episodes or torrent.data.seasons)
        ):
            logger.trace(
                f"Skipping show torrent for movie {item.log_string}: {raw_title}"
            )
            if funnel is not None:
                funnel.record_content_filter()
            continue

        if isinstance(item, Show):
            # make sure the torrent has at least 2 episodes (should weed out most junk)
            if not manual and torrent.data.episodes and len(torrent.data.episodes) <= 2:
                logger.trace(
                    f"Skipping torrent with too few episodes for {item.log_string}: {raw_title}"
                )
                if funnel is not None:
                    funnel.record_content_filter()
                continue

            # make sure all of the item seasons are present in the torrent
            if not manual and not all(
                season.number in torrent.data.seasons for season in item.seasons
            ):
                logger.trace(
                    f"Skipping torrent with incorrect number of seasons for {item.log_string}: {raw_title}"
                )
                if funnel is not None:
                    funnel.record_content_filter()
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
                if funnel is not None:
                    funnel.record_content_filter()
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
                if funnel is not None:
                    funnel.record_content_filter()
                continue

            # make sure the torrent has at least 2 episodes (should weed out most junk)
            if not manual and torrent.data.episodes and len(torrent.data.episodes) <= 2:
                logger.trace(
                    f"Skipping torrent with too few episodes for {item.log_string}: {raw_title}"
                )
                if funnel is not None:
                    funnel.record_content_filter()
                continue

            # disregard torrents with incorrect season number
            if not manual and item.number not in torrent.data.seasons:
                logger.trace(
                    f"Skipping incorrect season torrent for {item.log_string}: {raw_title}"
                )
                if funnel is not None:
                    funnel.record_content_filter()
                continue

            if (
                not manual
                and torrent.data.episodes
                and not all(
                    episode.number in torrent.data.episodes for episode in item.episodes
                )
            ):
                logger.trace(
                    f"Skipping incorrect season torrent for not having all episodes {item.log_string}: {raw_title}"
                )
                if funnel is not None:
                    funnel.record_content_filter()
                continue

        if isinstance(item, Episode) and not manual:
            # Disregard torrents with incorrect episode/season identity.
            # Episode number alone is not enough: S08E14 must not match S06E14.
            parent_season = cast(Season, item.parent)
            if not episode_release_matches(
                episode_number=item.number,
                absolute_number=item.absolute_number,
                season_number=parent_season.number,
                parsed_episodes=torrent.data.episodes,
                parsed_seasons=torrent.data.seasons,
            ):
                logger.trace(
                    f"Skipping incorrect episode torrent for {item.log_string}: {raw_title}"
                )
                if funnel is not None:
                    funnel.record_content_filter()
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
            if funnel is not None:
                funnel.record_content_filter()
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
            if funnel is not None:
                funnel.record_content_filter()
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
            if funnel is not None:
                funnel.record_content_filter()
            continue

        torrents.add(torrent)
        processed_infohashes.add(infohash)


def parse_results(
    item: MediaItem,
    results: dict[str, str],
    log_msg: bool = True,
    manual: bool = False,
    funnel: ScrapeFunnelStats | None = None,
) -> dict[str, Stream]:
    """Parse the results from the scrapers into Torrent objects.

    Args:
        item: The media item to parse results for.
        results: Dict mapping infohash to raw title.
        log_msg: If False, suppress debug progress logs during ranking/sort.
        manual: If True, bypass content filters (for manual scraping).
        funnel: Optional scrape funnel counters (log-only telemetry).
    """

    torrents = set[Torrent]()
    processed_infohashes = set[str]()
    _accumulate_ranked_torrents(
        item,
        results,
        torrents,
        processed_infohashes,
        manual=manual,
        log_msg=log_msg,
        funnel=funnel,
    )
    return _streams_from_torrents(item, torrents, manual=manual, log_msg=log_msg)


def merge_parse_results(
    item: MediaItem,
    delta_results: dict[str, str],
    torrents: set[Torrent],
    processed_infohashes: set[str],
    *,
    manual: bool = False,
    log_msg: bool = True,
    funnel: ScrapeFunnelStats | None = None,
) -> dict[str, Stream]:
    """Parse only newly seen scraper results and return the full ranked stream map.

    Mutates ``torrents`` and ``processed_infohashes`` so callers can reuse them
    across streaming scrape completions without re-ranking prior hashes.
    """

    _accumulate_ranked_torrents(
        item,
        delta_results,
        torrents,
        processed_infohashes,
        manual=manual,
        log_msg=log_msg,
        funnel=funnel,
    )
    return _streams_from_torrents(item, torrents, manual=manual, log_msg=log_msg)


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
