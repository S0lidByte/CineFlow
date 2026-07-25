"""Characterization tests for Phase 2 anime ranking soft-opt-in.

Defaults must remain strict. Opt-in flags only affect is_anime items.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from RTN.exceptions import GarbageTorrent
from RTN.models import SettingsModel

from program.services.scrapers.shared import (
    _apply_anime_extras_dubbed_soft_opt_in,
    _rank_with_language_compat,
    parse_results,
)
from program.settings import settings_manager

DUAL_AUDIO_TITLE = (
    "Dragon Ball Z Resurrection F 2015 1080p BluRay Dual Audio DDP5.1 x264-EDGE2020"
)
MULTI_TITLE = "Dragon Ball Z Resurrection F 2015 MULTi TRUEFRENCH 1080p BluRay x264"
CORRECT_TITLE = "Dragon Ball Z: Resurrection F"


class AnimeItem:
    top_title = CORRECT_TITLE
    log_string = CORRECT_TITLE
    country = None
    is_anime = True
    aired_at = None

    @staticmethod
    def get_aliases():
        return {}


class NonAnimeItem:
    top_title = CORRECT_TITLE
    log_string = CORRECT_TITLE
    country = None
    is_anime = False
    aired_at = None

    @staticmethod
    def get_aliases():
        return {}


@pytest.fixture(autouse=True)
def _reset_soft_opt_in_flags():
    scraping = settings_manager.settings.scraping
    prev_dubbed = scraping.anime_allow_extras_dubbed
    prev_multi = scraping.anime_allow_multi_audio
    scraping.anime_allow_extras_dubbed = False
    scraping.anime_allow_multi_audio = False
    try:
        yield
    finally:
        scraping.anime_allow_extras_dubbed = prev_dubbed
        scraping.anime_allow_multi_audio = prev_multi


def test_defaults_keep_extras_dubbed_fetch_disabled_for_anime():
    settings = SettingsModel()
    settings.custom_ranks.extras.dubbed.fetch = False
    settings.languages.required = []

    item = SimpleNamespace(is_anime=True, log_string="Anime")
    result = _apply_anime_extras_dubbed_soft_opt_in(item, settings)

    assert result.custom_ranks.extras.dubbed.fetch is False
    assert result is settings


def test_opt_in_enables_extras_dubbed_fetch_for_anime_only():
    settings_manager.settings.scraping.anime_allow_extras_dubbed = True

    settings = SettingsModel()
    settings.custom_ranks.extras.dubbed.fetch = False
    settings.languages.required = []

    anime = SimpleNamespace(is_anime=True, log_string="Anime")
    non_anime = SimpleNamespace(is_anime=False, log_string="Movie")

    soft = _apply_anime_extras_dubbed_soft_opt_in(anime, settings)
    strict = _apply_anime_extras_dubbed_soft_opt_in(non_anime, settings)

    assert soft.custom_ranks.extras.dubbed.fetch is True
    assert settings.custom_ranks.extras.dubbed.fetch is False
    assert strict.custom_ranks.extras.dubbed.fetch is False


def test_default_rejects_dual_audio_when_extras_dubbed_fetch_off():
    settings = SettingsModel()
    settings.custom_ranks.extras.dubbed.fetch = False
    settings.languages.required = []

    from RTN import RTN, DefaultRanking

    with pytest.raises(GarbageTorrent, match="extras_dubbed"):
        _rank_with_language_compat(
            RTN(settings, DefaultRanking()),
            settings,
            raw_title=DUAL_AUDIO_TITLE,
            infohash="a" * 40,
            correct_title=CORRECT_TITLE,
            remove_trash=True,
            aliases={},
            item=AnimeItem(),
        )


def test_opt_in_allows_dual_audio_for_anime_via_parse_results():
    settings_manager.settings.scraping.anime_allow_extras_dubbed = True

    with settings_manager.override(
        languages={"required": []},
        custom_ranks={
            **SettingsModel().custom_ranks.model_dump(),
            "extras": {
                **SettingsModel().custom_ranks.extras.model_dump(),
                "dubbed": {"fetch": False, "use_custom_rank": False, "rank": 0},
            },
        },
    ):
        streams = parse_results(
            AnimeItem(),
            {"d" * 40: DUAL_AUDIO_TITLE},
        )

    assert list(streams) == ["d" * 40]


def test_opt_in_does_not_allow_dual_audio_for_non_anime():
    settings_manager.settings.scraping.anime_allow_extras_dubbed = True

    with settings_manager.override(
        languages={"required": []},
        custom_ranks={
            **SettingsModel().custom_ranks.model_dump(),
            "extras": {
                **SettingsModel().custom_ranks.extras.model_dump(),
                "dubbed": {"fetch": False, "use_custom_rank": False, "rank": 0},
            },
        },
    ):
        streams = parse_results(
            NonAnimeItem(),
            {"e" * 40: DUAL_AUDIO_TITLE},
        )

    assert list(streams) == []


def test_default_rejects_multi_when_english_required():
    settings = SettingsModel()
    settings.languages.required = ["en"]
    settings.custom_ranks.extras.dubbed.fetch = True

    from RTN import RTN, DefaultRanking

    with pytest.raises(GarbageTorrent, match="missing_required_language"):
        _rank_with_language_compat(
            RTN(settings, DefaultRanking()),
            settings,
            raw_title=MULTI_TITLE,
            infohash="b" * 40,
            correct_title=CORRECT_TITLE,
            remove_trash=True,
            aliases={},
            item=AnimeItem(),
        )


def test_opt_in_allows_multi_for_anime_via_parse_results():
    settings_manager.settings.scraping.anime_allow_multi_audio = True

    with settings_manager.override(languages={"required": ["en"]}):
        streams = parse_results(
            AnimeItem(),
            {"f" * 40: MULTI_TITLE},
        )

    assert list(streams) == ["f" * 40]


def test_opt_in_does_not_allow_multi_for_non_anime():
    settings_manager.settings.scraping.anime_allow_multi_audio = True

    with settings_manager.override(languages={"required": ["en"]}):
        streams = parse_results(
            NonAnimeItem(),
            {"g" * 40: MULTI_TITLE},
        )

    assert list(streams) == []
