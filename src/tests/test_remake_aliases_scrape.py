"""Scrape-time remake alias soft-opt-in.

Defaults must reject remake titles without an alias. Opt-in merges
remake_alias_groups into RTN aliases; funnel still buckets title_mismatch.
"""

from __future__ import annotations

import pytest
from RTN.exceptions import GarbageTorrent
from RTN.models import SettingsModel

from program.media.item import Movie, Show
from program.services.scrapers.funnel import bucket_rtn_reason
from program.services.scrapers.shared import (
    _merge_remake_aliases,
    _prepare_rtn_ranking_context,
    _rank_with_language_compat,
    parse_results,
)
from program.settings import settings_manager
from program.settings.ranking_presets import matching_mode_by_id

REMAKE_RAW = "Saint.Seiya.2023.1080p.WEB-DL.DDP5.1.H.264-GROUP"
CORRECT_TITLE = "Knights of the Zodiac"
ALIAS_TITLE = "Saint Seiya"
INFOHASH = "a" * 40


class RemakeItem:
    top_title = CORRECT_TITLE
    log_string = CORRECT_TITLE
    country = None
    is_anime = False
    aired_at = None

    def __init__(self, aliases: dict | None = None):
        self._aliases = aliases

    def get_aliases(self):
        return self._aliases


@pytest.fixture(autouse=True)
def _reset_remake_flags():
    scraping = settings_manager.settings.scraping
    prev = (
        scraping.enable_aliases,
        scraping.enable_remake_aliases,
        list(scraping.remake_alias_groups),
    )
    scraping.enable_aliases = True
    scraping.enable_remake_aliases = False
    scraping.remake_alias_groups = []
    try:
        yield
    finally:
        scraping.enable_aliases = prev[0]
        scraping.enable_remake_aliases = prev[1]
        scraping.remake_alias_groups = prev[2]


def test_classic_rejects_remake_without_alias():
    """Default: remake release fails title match when no alias / remake opt-in."""

    settings = SettingsModel()
    settings.options.title_similarity = 0.85
    settings.languages.required = []
    settings.custom_ranks.extras.dubbed.fetch = True

    from RTN import RTN, DefaultRanking

    with pytest.raises(GarbageTorrent) as exc:
        _rank_with_language_compat(
            RTN(settings, DefaultRanking()),
            settings,
            raw_title=REMAKE_RAW,
            infohash=INFOHASH,
            correct_title=CORRECT_TITLE,
            remove_trash=True,
            aliases={},
            item=RemakeItem(),
        )

    assert bucket_rtn_reason(exc.value) == "title_mismatch"


def test_accepts_remake_when_indexer_alias_present():
    """Indexer aliases alone (enable_aliases) can accept remakes at default threshold."""

    settings = SettingsModel()
    settings.options.title_similarity = 0.85
    settings.languages.required = []
    settings.custom_ranks.extras.dubbed.fetch = True

    from RTN import RTN, DefaultRanking

    torrent = _rank_with_language_compat(
        RTN(settings, DefaultRanking()),
        settings,
        raw_title=REMAKE_RAW,
        infohash=INFOHASH,
        correct_title=CORRECT_TITLE,
        remove_trash=True,
        aliases={"us": [ALIAS_TITLE]},
        item=RemakeItem({"us": [ALIAS_TITLE]}),
    )
    assert torrent.infohash.lower() == INFOHASH


def test_merge_remake_aliases_default_off():
    merged = _merge_remake_aliases(CORRECT_TITLE, {"us": ["Knights"]})
    assert merged == {"us": ["Knights"]}


def test_merge_remake_aliases_injects_group_when_opted_in():
    scraping = settings_manager.settings.scraping
    scraping.enable_remake_aliases = True
    scraping.remake_alias_groups = [[CORRECT_TITLE, ALIAS_TITLE]]

    merged = _merge_remake_aliases(CORRECT_TITLE, {})
    assert ALIAS_TITLE in merged.get("xx", [])


def test_merge_remake_aliases_ignores_unrelated_groups():
    scraping = settings_manager.settings.scraping
    scraping.enable_remake_aliases = True
    scraping.remake_alias_groups = [["Some Other Movie", "Alternate Name"]]

    merged = _merge_remake_aliases(CORRECT_TITLE, {})
    assert merged == {}


def test_opt_in_accepts_remake_via_parse_results():
    scraping = settings_manager.settings.scraping
    scraping.enable_aliases = True
    scraping.enable_remake_aliases = True
    scraping.remake_alias_groups = [[CORRECT_TITLE, ALIAS_TITLE]]

    with settings_manager.override(
        languages={"required": []},
        options={
            **SettingsModel().options.model_dump(),
            "title_similarity": 0.85,
        },
    ):
        streams = parse_results(
            RemakeItem(),
            {INFOHASH: REMAKE_RAW},
        )

    assert list(streams) == [INFOHASH]


def test_opt_in_off_still_rejects_via_parse_results():
    scraping = settings_manager.settings.scraping
    scraping.enable_aliases = True
    scraping.enable_remake_aliases = False
    scraping.remake_alias_groups = [[CORRECT_TITLE, ALIAS_TITLE]]

    with settings_manager.override(
        languages={"required": []},
        options={
            **SettingsModel().options.model_dump(),
            "title_similarity": 0.85,
        },
    ):
        streams = parse_results(
            RemakeItem(),
            {INFOHASH: REMAKE_RAW},
        )

    assert streams == {}


def test_prepare_context_merges_remake_aliases():
    scraping = settings_manager.settings.scraping
    scraping.enable_aliases = True
    scraping.enable_remake_aliases = True
    scraping.remake_alias_groups = [[CORRECT_TITLE, ALIAS_TITLE]]

    _rtn, _settings, title, aliases = _prepare_rtn_ranking_context(RemakeItem())
    assert title == CORRECT_TITLE
    assert ALIAS_TITLE in aliases.get("xx", [])


def test_remake_diagnose_mode_is_tester_only():
    mode = matching_mode_by_id("remake_diagnose")
    assert mode is not None
    assert mode["diagnose_only"] is True
    assert mode.get("scrape_applied") is False


def test_funnel_still_buckets_title_mismatch():
    msg = (
        "title does not match the correct title 'Saint Seiya: Legend of Crimson Youth', "
        "parsed title: 'Knights of the Zodiac'"
    )
    assert bucket_rtn_reason(Exception(msg)) == "title_mismatch"


def test_accepts_anime_arc_subtitle_release():
    """Anime/series arc subtitles (Big Bang Mission, Ultra God Mission) are accepted automatically."""

    settings = SettingsModel()
    settings.options.title_similarity = 0.85
    settings.languages.required = []
    settings.custom_ranks.extras.dubbed.fetch = True

    from RTN import RTN, DefaultRanking

    raw_title = "[Grupa Mirai] Super Dragon Ball Heroes Big Bang Mission - 19 [WEB 1080p AAC] [PL]"
    correct_title = "Super Dragon Ball Heroes"

    torrent = _rank_with_language_compat(
        RTN(settings, DefaultRanking()),
        settings,
        raw_title=raw_title,
        infohash=INFOHASH,
        correct_title=correct_title,
        remove_trash=True,
        aliases={},
    )
    assert torrent.infohash.lower() == INFOHASH


def test_rejects_unrelated_franchise_title():
    """Unrelated series titles like Dragon Ball Super vs Super Dragon Ball Heroes are still rejected."""

    settings = SettingsModel()
    settings.options.title_similarity = 0.85
    settings.languages.required = []
    settings.custom_ranks.extras.dubbed.fetch = True

    from RTN import RTN, DefaultRanking

    raw_title = "Dragon.Ball.Super.2015-2018.MULTi.720p.BluRay.x264.AC3-EMiS"
    correct_title = "Super Dragon Ball Heroes"

    with pytest.raises(GarbageTorrent) as exc:
        _rank_with_language_compat(
            RTN(settings, DefaultRanking()),
            settings,
            raw_title=raw_title,
            infohash=INFOHASH,
            correct_title=correct_title,
            remove_trash=True,
            aliases={},
        )

    assert bucket_rtn_reason(exc.value) == "title_mismatch"


def test_hope_movie_rejects_unrelated_substring_release_alias_recovery():
    """Ordinary movie Hope (2026) must not accept an unrelated title like Star Wars via alias recovery."""
    settings = SettingsModel()
    settings.options.title_similarity = 0.85
    settings.languages.required = []

    from RTN import RTN, DefaultRanking

    raw_title = "Star.Wars.Episode.IV.A.New.Hope.1977.1080p"
    movie = Movie({"title": "Hope", "year": 2026})

    with pytest.raises(GarbageTorrent) as exc:
        _rank_with_language_compat(
            RTN(settings, DefaultRanking()),
            settings,
            raw_title=raw_title,
            infohash=INFOHASH,
            correct_title="Hope",
            remove_trash=True,
            aliases={},
            item=movie,
        )

    assert bucket_rtn_reason(exc.value) == "title_mismatch"

    # End-to-end parse_results must yield 0 streams
    streams = parse_results(movie, {INFOHASH: raw_title})
    assert streams == {}


def test_resident_evil_2026_rejects_dated_2002_release_via_year_validation():
    """Resident Evil (2026) must reject explicitly dated 2002 release via existing year validation."""
    raw_title = "Resident.Evil.2002.1080p.BluRay.x264"
    re_2026 = Movie({"title": "Resident Evil", "year": 2026})

    streams = parse_results(re_2026, {INFOHASH: raw_title})
    assert streams == {}


def test_legitimate_anime_series_arc_title_aliases_continue_working():
    """Legitimate anime/series arc-title aliases must continue to be accepted for Show items."""
    settings = SettingsModel()
    settings.options.title_similarity = 0.85
    settings.languages.required = []

    from RTN import RTN, DefaultRanking

    raw_title = "[Grupa Mirai] Super Dragon Ball Heroes Big Bang Mission - 19 [WEB 1080p AAC] [PL]"
    show = Show({"title": "Super Dragon Ball Heroes"})

    torrent = _rank_with_language_compat(
        RTN(settings, DefaultRanking()),
        settings,
        raw_title=raw_title,
        infohash=INFOHASH,
        correct_title="Super Dragon Ball Heroes",
        remove_trash=True,
        aliases={},
        item=show,
    )
    assert torrent.infohash.lower() == INFOHASH


def test_existing_valid_movie_match_unaffected():
    """Standard matching for a valid movie release remains completely unaffected."""
    raw_title = "Inception.2010.1080p.BluRay.x264"
    movie = Movie({"title": "Inception", "year": 2010})

    streams = parse_results(movie, {INFOHASH: raw_title})
    assert len(streams) == 1
    assert INFOHASH in streams
