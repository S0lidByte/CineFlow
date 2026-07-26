"""Shared functions for scrapers."""

from datetime import datetime
from typing import Literal, cast

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


def resolve_ranking_pack(
    item: MediaItem | object,
) -> Literal["ranking", "ranking_anime"]:
    """Resolve which ranking pack applies for scrape ranking.

    Prefer the first matching library profile (settings order) that sets
    ``ranking_pack``. Otherwise fall back to ``item.is_anime`` routing.
    """
    from program.services.library_profile_matcher import LibraryProfileMatcher
    from program.settings.models import RankingPackKey

    profiles = settings_manager.settings.filesystem.library_profiles or {}
    try:
        matching = LibraryProfileMatcher().get_matching_profiles(item)  # type: ignore[arg-type]
    except Exception:
        matching = []

    for profile_key in matching:
        profile = profiles.get(profile_key)
        pack: RankingPackKey | None = (
            profile.ranking_pack if profile is not None else None
        )
        if pack is not None:
            return pack

    return "ranking_anime" if _item_is_anime(item) else "ranking"


def item_uses_anime_ranking(item: MediaItem | object) -> bool:
    """True when scrape ranking should use the anime pack for this item."""
    return resolve_ranking_pack(item) == "ranking_anime"


def _scraping_settings() -> ScraperModel:
    """Live scraping settings (avoid stale import-time snapshot)."""

    return settings_manager.settings.scraping


def _normalize_alias_title(title: str) -> str:
    """Casefold + collapse whitespace for remake group membership checks."""

    return " ".join((title or "").casefold().split())


def _collect_item_alias_names(
    correct_title: str,
    aliases: dict[str, list[str]],
) -> set[str]:
    """All known titles for an item (canonical + alias values), normalized."""

    names: set[str] = set()
    if correct_title:
        names.add(_normalize_alias_title(correct_title))
    for values in aliases.values():
        for name in values:
            if name.strip():
                names.add(_normalize_alias_title(name))
    names.discard("")
    return names


def _merge_remake_aliases(
    correct_title: str,
    aliases: dict[str, list[str]],
) -> dict[str, list[str]]:
    """Merge settings remake_alias_groups into aliases when opted in.

    Only injects alternate titles from groups that intersect the item's known
    titles. Default (enable_remake_aliases=False) returns aliases unchanged.
    """

    scraping = _scraping_settings()
    if not scraping.enable_remake_aliases:
        return aliases

    groups: list[list[str]] = list(scraping.remake_alias_groups)
    if not groups:
        return aliases

    known = _collect_item_alias_names(correct_title, aliases)
    if not known:
        return aliases

    extras: list[str] = []
    seen_extra: set[str] = {
        _normalize_alias_title(n) for values in aliases.values() for n in values
    }
    if correct_title:
        seen_extra.add(_normalize_alias_title(correct_title))

    for group in groups:
        if len(group) < 2:
            continue
        members = [m.strip() for m in group if m.strip()]
        if len(members) < 2:
            continue
        member_norms = {_normalize_alias_title(m) for m in members}
        if not member_norms.intersection(known):
            continue
        for member in members:
            norm = _normalize_alias_title(member)
            if norm in seen_extra:
                continue
            seen_extra.add(norm)
            extras.append(member)

    if not extras:
        return aliases

    merged = {k: list(v) for k, v in aliases.items()}
    existing = list(merged.get("xx") or [])
    # Preserve operator-configured aliases; append remake extras without dupes.
    existing_norms = {_normalize_alias_title(n) for n in existing}
    for name in extras:
        if _normalize_alias_title(name) not in existing_norms:
            existing.append(name)
            existing_norms.add(_normalize_alias_title(name))
    merged["xx"] = existing
    logger.debug(
        f"Remake alias soft-opt-in: injected {len(extras)} alias(es) for "
        f"{correct_title!r}"
    )
    return merged


def _resolve_scrape_aliases(
    item: MediaItem,
    active_settings: SettingsModel,
) -> dict[str, list[str]]:
    """Build aliases for live scrape ranking (indexer + optional remake groups)."""

    scraping = _scraping_settings()
    if not scraping.enable_aliases:
        return {}

    raw = item.get_aliases() or {}
    aliases = {
        k: list(v) for k, v in raw.items() if k not in active_settings.languages.exclude
    }
    return _merge_remake_aliases(item.top_title or "", aliases)


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


def _should_retry_with_title_alias(
    error: GarbageTorrent,
    *,
    item: MediaItem | object | None,
    correct_title: str,
    aliases: dict[str, list[str]],
    raw_title: str,
) -> str | None:
    """Return a candidate title alias when a title mismatch is caused by arc subtitles.

    For anime and series releases with arc/sub-season titles in release names
    (e.g., 'Super Dragon Ball Heroes Big Bang Mission'), RTN's parser extracts
    the arc subtitle as part of parsed_title. When parsed_title contains or
    is contained by correct_title (or any known alias), return parsed_title so
    ranking can retry with the arc subtitle accepted.
    """

    if "does not match the correct title" not in str(error):
        return None

    try:
        parsed_title = parse(raw_title).parsed_title
    except Exception:
        return None

    if not parsed_title or not correct_title:
        return None

    known = _collect_item_alias_names(correct_title, aliases)
    norm_parsed = _normalize_alias_title(parsed_title)

    if not norm_parsed:
        return None

    for name in known:
        if len(name) >= 4:
            if name in norm_parsed or norm_parsed in name:
                return parsed_title

    return None


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
    current_aliases = aliases
    title_alias = None

    try:
        return rtn_instance.rank(
            raw_title=raw_title,
            infohash=infohash,
            correct_title=correct_title,
            remove_trash=remove_trash,
            aliases=current_aliases,
        )
    except GarbageTorrent as e:
        title_alias = _should_retry_with_title_alias(
            e,
            item=item,
            correct_title=correct_title,
            aliases=current_aliases,
            raw_title=raw_title,
        )
        if title_alias:
            current_aliases = {k: list(v) for k, v in current_aliases.items()}
            xx_list = current_aliases.setdefault("xx", [])
            if title_alias not in xx_list:
                xx_list.append(title_alias)
            logger.trace(
                f"Retrying ranking with title arc alias '{title_alias}' for {correct_title!r}"
            )
            try:
                return rtn_instance.rank(
                    raw_title=raw_title,
                    infohash=infohash,
                    correct_title=correct_title,
                    remove_trash=remove_trash,
                    aliases=current_aliases,
                )
            except GarbageTorrent as inner_e:
                e = inner_e

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
            aliases=current_aliases,
        )


def get_ranking_overrides(
    ranking_overrides: dict[str, list[str]] | None,
    *,
    for_anime: bool = False,
) -> SettingsModel | None:
    """Apply category→attribute fetch overrides onto the effective ranking pack.

    Bases on live ``ranking`` or ``ranking_anime`` (not the module-level movies
    snapshot) so anime scrapes do not silently mutate the movies pack.
    """
    if not ranking_overrides:
        return None

    try:
        base = settings_manager.get_effective_rtn_model(for_anime=for_anime)
        settings_model = RTNSettingsModel(**base.model_dump())

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
    for_anime = item_uses_anime_ranking(item)
    active_settings = settings_manager.get_effective_rtn_model(for_anime=for_anime)
    _normalize_rtn_language_settings(active_settings)
    active_settings = _apply_anime_extras_dubbed_soft_opt_in(item, active_settings)

    # Module-level ``rtn`` is built from movie/show ranking only — never reuse it
    # for anime packs even when active_settings match ranking_anime defaults.
    is_default_settings = (
        not for_anime and active_settings.model_dump() == ranking_settings.model_dump()
    )
    rtn_instance = rtn if is_default_settings else RTN(active_settings, ranking_model)

    aliases = _resolve_scrape_aliases(item, active_settings)

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
