"""Golden suite for Ranking Studio P0: pattern validation + RTN preset round-trips."""

from __future__ import annotations

import pytest
from RTN import RTN, DefaultRanking
from RTN.exceptions import GarbageTorrent
from RTN.models import SettingsModel

from program.settings.models import RTNSettingsModel
from program.settings.ranking_patterns import (
    MAX_PATTERN_LENGTH,
    MAX_PATTERNS_PER_LIST,
    validate_pattern_lists,
    validate_ranking_payload_patterns,
)
from program.services.scrapers.shared import normalize_rtn_language_settings

WEBDL_DDP = "The.Movie.2024.2160p.WEB-DL.DDP5.1.Atmos.H.265-GROUP"
ANIME_MULTI = "Dragon Ball Z Resurrection F 2015 MULTi TRUEFRENCH 1080p BluRay x264"
REMUX_TITLE = "The.Movie.2024.2160p.BluRay.REMUX.HEVC.TrueHD.Atmos-GROUP"
CAM_TITLE = "The.Movie.2024.CAM.XviD-GROUP"
MATTE_TITLE = "The.Movie.2024.1080p.BluRay.matte.x264-GROUP"
INFOHASH = "0" * 40


def _rank(raw_title: str, ranking: dict, *, correct_title: str = "", remove_trash: bool = True):
    validate_ranking_payload_patterns(ranking)
    settings_model = RTNSettingsModel(**ranking)
    normalize_rtn_language_settings(settings_model)
    rtn = RTN(settings_model, DefaultRanking())
    return rtn.rank(
        raw_title=raw_title,
        infohash=INFOHASH,
        correct_title=correct_title,
        remove_trash=remove_trash,
        aliases={},
    )


def _balanced_like() -> dict:
    settings = SettingsModel()
    dump = settings.model_dump()
    dump["resolutions"]["r2160p"] = True
    dump["resolutions"]["r1080p"] = True
    dump["custom_ranks"]["trash"]["cam"]["fetch"] = False
    dump["custom_ranks"]["quality"]["webdl"]["fetch"] = True
    dump["custom_ranks"]["audio"]["dolby_digital_plus"]["fetch"] = True
    dump["options"]["remove_all_trash"] = True
    dump["options"]["remove_adult_content"] = True
    return dump


def _remux_max_like() -> dict:
    settings = SettingsModel()
    dump = settings.model_dump()
    dump["resolutions"]["r2160p"] = True
    dump["resolutions"]["r1080p"] = True
    for cat, attrs in dump["custom_ranks"].items():
        for attr in attrs:
            dump["custom_ranks"][cat][attr]["fetch"] = False
    for attr in ("hevc", "avc", "bluray", "remux"):
        dump["custom_ranks"]["quality"][attr]["fetch"] = True
    for attr in ("atmos", "truehd", "dts_lossless", "flac"):
        dump["custom_ranks"]["audio"][attr]["fetch"] = True
    for attr in ("hdr", "hdr10plus", "dolby_vision", "bit10"):
        dump["custom_ranks"]["hdr"][attr]["fetch"] = True
    for attr in ("proper", "repack"):
        dump["custom_ranks"]["extras"][attr]["fetch"] = True
    dump["preferred"] = [r"\bREMUX\b", r"\bBluRay\b"]
    dump["exclude"] = [r"\bmatte\b"]
    return dump


def _kids_safe_like() -> dict:
    dump = _balanced_like()
    dump["options"]["remove_adult_content"] = True
    dump["options"]["remove_all_trash"] = True
    dump["options"]["title_similarity"] = 0.9
    dump["exclude"] = [r"\bxxx\b", r"\bporn\b"]
    for attr in dump["custom_ranks"]["trash"]:
        dump["custom_ranks"]["trash"][attr]["fetch"] = False
    return dump


def _anime_dub_like() -> dict:
    dump = _balanced_like()
    dump["custom_ranks"]["extras"]["dubbed"]["fetch"] = True
    dump["custom_ranks"]["extras"]["subbed"]["fetch"] = True
    dump["languages"]["preferred"] = ["anime"]
    dump["languages"]["required"] = []
    dump["exclude"] = [r"\bmatte\b"]
    return dump


def test_validate_patterns_accepts_safe_lists():
    result = validate_pattern_lists(
        require=[r"\b1080p\b"],
        exclude=[r"\bmatte\b"],
        preferred=[r"/HDR|HDR10/"],
        preview_title=WEBDL_DDP,
    )
    assert result.valid is True
    assert result.errors == []
    assert result.preview is not None
    assert result.preview.require_matches == []


def test_validate_patterns_rejects_redos_and_overlong():
    nested = validate_pattern_lists(exclude=["(a+)+"])
    assert nested.valid is False
    assert any("ReDoS" in e.message for e in nested.errors)

    too_long = "a" * (MAX_PATTERN_LENGTH + 1)
    long_result = validate_pattern_lists(require=[too_long])
    assert long_result.valid is False

    too_many = [f"pat{i}" for i in range(MAX_PATTERNS_PER_LIST + 1)]
    count_result = validate_pattern_lists(require=too_many)
    assert count_result.valid is False


def test_validate_ranking_payload_patterns_raises():
    with pytest.raises(ValueError, match="Invalid ranking pattern"):
        validate_ranking_payload_patterns({"exclude": ["(a+)+"]})


def test_ranking_accepts_webdl_ddp_balanced():
    torrent = _rank(WEBDL_DDP, _balanced_like(), correct_title="The Movie")
    assert torrent.fetch is True
    assert torrent.rank > 0


def test_ranking_rejects_cam_kids_safe():
    with pytest.raises(GarbageTorrent):
        _rank(CAM_TITLE, _kids_safe_like(), correct_title="The Movie")


def test_ranking_remux_max_accepts_remux_rejects_webdl():
    remux = _rank(REMUX_TITLE, _remux_max_like(), correct_title="The Movie")
    assert remux.fetch is True
    with pytest.raises(GarbageTorrent):
        _rank(WEBDL_DDP, _remux_max_like(), correct_title="The Movie")


def test_ranking_matte_exclude_round_trip():
    ranking = _anime_dub_like()
    preview = validate_pattern_lists(
        exclude=ranking["exclude"],
        preview_title=MATTE_TITLE,
    )
    assert preview.valid is True
    assert preview.preview is not None
    assert preview.preview.exclude_matches == [r"\bmatte\b"]

    with pytest.raises(GarbageTorrent):
        _rank(MATTE_TITLE, ranking, correct_title="The Movie")


def test_ranking_languages_required_can_reject_multi():
    ranking = _balanced_like()
    ranking["languages"]["required"] = ["en"]
    ranking["options"]["allow_english_in_languages"] = False
    # MULTI / TRUEFRENCH lacks English tagging → language reject when remove_trash=True.
    with pytest.raises(GarbageTorrent, match="missing_required_language"):
        _rank(
            ANIME_MULTI,
            ranking,
            correct_title="Dragon Ball Z: Resurrection F",
        )


def test_invalid_patterns_blocked_before_rtn():
    ranking = _balanced_like()
    ranking["exclude"] = ["(a+)+"]
    with pytest.raises(ValueError, match="Invalid ranking pattern"):
        validate_ranking_payload_patterns(ranking)


def test_meta_soft_opt_in_keys_present():
    from program.settings.ranking_descriptions import DENY_KEY_HELP

    assert "extras_dubbed" in DENY_KEY_HELP
    assert "anime_allow_extras_dubbed" in DENY_KEY_HELP["extras_dubbed"]
    assert "missing_required_language" in DENY_KEY_HELP
    assert "anime_allow_multi_audio" in DENY_KEY_HELP["missing_required_language"]
    assert "title_mismatch" in DENY_KEY_HELP


def test_golden_remake_title_present_in_shared_contract():
    from program.settings.ranking_presets import GOLDEN_TITLES

    assert "Knights.of.the.Zodiac" in GOLDEN_TITLES["title_mismatch_remake"]
