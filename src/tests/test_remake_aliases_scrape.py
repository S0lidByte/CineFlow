"""Scrape-time remake alias soft-opt-in.

Defaults must reject remake titles without an alias. Opt-in merges
remake_alias_groups into RTN aliases; funnel still buckets title_mismatch.
"""

from __future__ import annotations

import pytest
from RTN.exceptions import GarbageTorrent
from RTN.models import SettingsModel

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
