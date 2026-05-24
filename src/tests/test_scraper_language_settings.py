import pytest
from RTN import RTN, DefaultRanking
from RTN.exceptions import GarbageTorrent
from RTN.models import SettingsModel

from program.services.scrapers.shared import (
    _normalize_rtn_language_settings,
    _rank_with_language_compat,
    parse_results,
)
from program.settings import settings_manager


class DummyItem:
    top_title = "Stargate: Continuum"
    log_string = "Stargate: Continuum"
    country = None
    is_anime = False
    aired_at = None

    @staticmethod
    def get_aliases():
        return {}


def test_normalize_rtn_language_settings_accepts_common_alpha3_codes():
    settings = SettingsModel()
    settings.languages.required = ["eng"]
    settings.languages.exclude = ["jpn"]
    settings.languages.allowed = ["por"]
    settings.languages.preferred = ["fra"]

    _normalize_rtn_language_settings(settings)

    assert settings.languages.required == ["en"]
    assert settings.languages.exclude == ["ja"]
    assert settings.languages.allowed == ["pt"]
    assert settings.languages.preferred == ["fr"]


def test_rank_with_language_compat_treats_untagged_release_as_english_when_allowed():
    settings = SettingsModel()
    settings.languages.required = ["eng"]
    _normalize_rtn_language_settings(settings)

    torrent = _rank_with_language_compat(
        RTN(settings, DefaultRanking()),
        settings,
        raw_title="Stargate Continuum 2008 1080p BluRay x264-OFT",
        infohash="a" * 40,
        correct_title="Stargate: Continuum",
        remove_trash=True,
        aliases={},
    )

    assert torrent.fetch is True
    assert torrent.data.languages == []


def test_rank_with_language_compat_still_rejects_explicit_non_english_release():
    settings = SettingsModel()
    settings.languages.required = ["eng"]
    _normalize_rtn_language_settings(settings)

    with pytest.raises(GarbageTorrent, match="missing_required_language"):
        _rank_with_language_compat(
            RTN(settings, DefaultRanking()),
            settings,
            raw_title="Stargate Continuum 2008 MULTi TRUEFRENCH 1080p WEB H264-DELiCiOUS mkv",
            infohash="b" * 40,
            correct_title="Stargate: Continuum",
            remove_trash=True,
            aliases={},
        )


def test_parse_results_applies_language_compatibility_to_active_settings():
    with settings_manager.override(languages={"required": ["eng"]}):
        streams = parse_results(
            DummyItem(),
            {"c" * 40: "Stargate Continuum 2008 1080p BluRay x264-OFT"},
        )

    assert list(streams) == ["c" * 40]
