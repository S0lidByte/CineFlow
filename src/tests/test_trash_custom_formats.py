"""Unit tests for TRaSH Guides Custom Formats scoring and evaluation engine."""

import pytest

from program.services.scrapers.trash_scorer import (
    evaluate_trash_condition,
    evaluate_trash_custom_format,
    evaluate_trash_release,
)
from program.settings.trash_catalog import (
    get_default_trash_custom_formats,
    get_default_trash_profiles,
)
from program.settings.trash_models import (
    TrashCondition,
    TrashCustomFormat,
    TrashFormatMatchResult,
    TrashProfile,
)


class TestTrashConditionEvaluation:
    """Test evaluating individual regex conditions with negate/case flags."""

    def test_basic_regex_match(self):
        cond = TrashCondition(pattern=r"\b(Remux)\b", required=True)
        assert evaluate_trash_condition(cond, "Movie.2024.1080p.Remux.AVC.DTS-HD.MA.5.1-GROUP") is True
        assert evaluate_trash_condition(cond, "Movie.2024.1080p.WEB-DL.DDP5.1.H.264-GROUP") is False

    def test_case_insensitive_by_default(self):
        cond = TrashCondition(pattern=r"\btruehd\b", required=True)
        assert evaluate_trash_condition(cond, "Movie.2024.2160p.TrueHD.Atmos.7.1") is True
        assert evaluate_trash_condition(cond, "Movie.2024.2160p.TRUEHD.Atmos.7.1") is True
        assert evaluate_trash_condition(cond, "Movie.2024.2160p.truehd.Atmos.7.1") is True

    def test_negated_condition(self):
        cond = TrashCondition(pattern=r"\b(HDR|DV|Dolby\.?Vision)\b", required=True, negate=True)
        # Should be True when pattern does NOT match
        assert evaluate_trash_condition(cond, "Movie.2024.1080p.SDR.BluRay.x264-GROUP") is True
        # Should be False when pattern DOES match
        assert evaluate_trash_condition(cond, "Movie.2024.2160p.UHD.HDR.BluRay.x265-GROUP") is False

    def test_empty_pattern_returns_true(self):
        cond = TrashCondition(pattern="", required=True)
        assert evaluate_trash_condition(cond, "Any Release Title") is True


class TestTrashCustomFormatEvaluation:
    """Test custom format evaluation rules (required & optional logic)."""

    def test_required_and_optional_conditions(self):
        # Format requiring TrueHD and (Atmos OR 7.1)
        cf = TrashCustomFormat(
            trash_id="test_truehd_atmos",
            name="TrueHD Atmos",
            category="audio_advanced",
            score=1500,
            conditions=[
                TrashCondition(pattern=r"\bTrueHD\b", required=True),
                TrashCondition(pattern=r"\bAtmos\b", required=False),
                TrashCondition(pattern=r"\b7\.1\b", required=False),
            ],
        )

        # Passes required (TrueHD) and optional (Atmos)
        res1 = evaluate_trash_custom_format(cf, "Movie.2024.2160p.Remux.TrueHD.Atmos.x265")
        assert res1.matched is True
        assert res1.score == 1500

        # Passes required (TrueHD) and optional (7.1)
        res2 = evaluate_trash_custom_format(cf, "Movie.2024.2160p.Remux.TrueHD.7.1.x265")
        assert res2.matched is True
        assert res2.score == 1500

        # Fails optional conditions (neither Atmos nor 7.1)
        res3 = evaluate_trash_custom_format(cf, "Movie.2024.2160p.Remux.TrueHD.5.1.x265")
        assert res3.matched is False

        # Fails required condition (no TrueHD)
        res4 = evaluate_trash_custom_format(cf, "Movie.2024.2160p.Remux.DTS-HD.MA.7.1.Atmos.x265")
        assert res4.matched is False

    def test_disabled_format_returns_unmatched(self):
        cf = TrashCustomFormat(
            trash_id="disabled_cf",
            name="Disabled CF",
            category="source_remux_tier",
            score=500,
            enabled=False,
            conditions=[TrashCondition(pattern=r"\b1080p\b", required=True)],
        )
        res = evaluate_trash_custom_format(cf, "Movie.2024.1080p.BluRay")
        assert res.matched is False

    def test_score_override(self):
        cf = TrashCustomFormat(
            trash_id="remux_cf",
            name="Remux",
            category="source_remux_tier",
            score=2000,
            conditions=[TrashCondition(pattern=r"\bRemux\b", required=True)],
        )
        # Standard score
        res1 = evaluate_trash_custom_format(cf, "Movie.2024.1080p.Remux")
        assert res1.score == 2000

        # Profile score override
        res2 = evaluate_trash_custom_format(cf, "Movie.2024.1080p.Remux", score_override=3500)
        assert res2.score == 3500


class TestTrashCatalogAndEvaluation:
    """Test full release evaluation against the standard TRaSH Guides catalog."""

    @pytest.fixture
    def catalog(self):
        return get_default_trash_custom_formats()

    def test_dolby_vision_with_hdr10_fallback(self, catalog):
        # Hybrid release having both DV and HDR10
        dv_hdr10_title = "Movie.2024.UHD.BluRay.2160p.TrueHD.Atmos.7.1.DV.HDR10.HEVC.REMUX-FraMeSToR"
        summary = evaluate_trash_release(dv_hdr10_title, formats=catalog)

        matched_ids = {m.trash_id for m in summary.matched_formats}
        assert "dv-hdr10-fallback" in matched_ids
        assert "truehd-atmos" in matched_ids
        assert "remux-tier-01" in matched_ids
        assert summary.total_score > 3000
        assert summary.rejected_by_lq is False

    def test_dolby_vision_fallback_sdr_penalty(self, catalog):
        # Profile 5 Dolby Vision without HDR10 fallback (purple/green tint risk on SDR/HDR10 TVs)
        dv_profile5_title = "Movie.2024.2160p.WEB-DL.DDP5.1.Atmos.DV.H.265-FLUX"
        summary = evaluate_trash_release(dv_profile5_title, formats=catalog)

        matched_ids = {m.trash_id for m in summary.matched_formats}
        assert "dv-no-fallback" in matched_ids
        assert "web-tier-01" in matched_ids

    def test_cam_telesync_critical_rejection(self, catalog):
        # CAM / Telesync / LQ source should be rejected instantly
        cam_title = "Blockbuster.2025.CAMRip.H264.AAC-JUNK"
        summary = evaluate_trash_release(cam_title, formats=catalog, reject_unwanted_sources=True)

        assert summary.rejected_by_lq is True
        assert "unwanted source" in summary.rejection_reason.lower() or "rejected" in summary.rejection_reason.lower()
        matched_ids = {m.trash_id for m in summary.matched_formats}
        assert "unwanted-video-sources" in matched_ids

    def test_negative_score_rejection(self):
        formats = [
            TrashCustomFormat(
                trash_id="bad_audio",
                name="Bad Audio",
                category="audio_advanced",
                score=-500,
                conditions=[TrashCondition(pattern=r"\bAAC1\.0\b", required=True)],
            ),
            TrashCustomFormat(
                trash_id="bad_codec",
                name="Bad Codec",
                category="unwanted_lq",
                score=-600,
                conditions=[TrashCondition(pattern=r"\bXviD\b", required=True)],
            ),
        ]
        title = "Movie.2005.DVDRip.XviD.AAC1.0-BAD"
        summary = evaluate_trash_release(title, formats=formats, reject_negative_scores=True)

        assert summary.total_score == -1100
        assert summary.rejected_by_lq is True
        assert "negative" in summary.rejection_reason.lower()

    def test_minimum_score_threshold_enforcement(self, catalog):
        title = "Movie.2024.720p.HDTV.x264-GROUP"
        # Minimum score required is 1000
        summary = evaluate_trash_release(title, formats=catalog, min_score=1000)

        assert summary.rejected_by_lq is True
        assert "minimum threshold" in summary.rejection_reason.lower()

    def test_clear_trash_cache_lifecycle(self):
        from program.services.scrapers.trash_scorer import clear_trash_cache
        cond = TrashCondition(pattern=r"\b(TEST_CACHE_PATTERN)\b", required=True)
        assert evaluate_trash_condition(cond, "Sample.TEST_CACHE_PATTERN.Movie") is True
        clear_trash_cache()
        assert evaluate_trash_condition(cond, "Sample.TEST_CACHE_PATTERN.Movie") is True



class TestTrashProfiles:
    """Test TRaSH Profiles (Balanced, Remux, WEB-DL, Anime)."""

    def test_default_profiles_exist(self):
        profiles = get_default_trash_profiles()
        profile_map = {p.profile_id: p for p in profiles}

        assert "trash_balanced" in profile_map
        assert "trash_remux" in profile_map
        assert "trash_webdl" in profile_map
        assert "trash_anime" in profile_map

    def test_remux_profile_favors_remux_over_webdl(self):
        catalog = get_default_trash_custom_formats()
        profiles = {p.profile_id: p for p in get_default_trash_profiles()}
        remux_profile = profiles["trash_remux"]

        remux_title = "Movie.2024.1080p.BluRay.Remux.AVC.DTS-HD.MA.5.1-FraMeSToR"
        webdl_title = "Movie.2024.1080p.WEB-DL.DDP5.1.Atmos.H.264-FLUX"

        remux_eval = evaluate_trash_release(remux_title, formats=catalog, profile=remux_profile)
        webdl_eval = evaluate_trash_release(webdl_title, formats=catalog, profile=remux_profile)

        assert remux_eval.total_score > webdl_eval.total_score
        assert remux_eval.total_score >= 3500

    def test_anime_fansub_tier_scoring(self):
        catalog = get_default_trash_custom_formats()
        profiles = {p.profile_id: p for p in get_default_trash_profiles()}
        anime_profile = profiles["trash_anime"]

        tier1_anime = "[SubsPlease] Frieren - Beyond Journey's End - 01 (1080p) [12345678].mkv"
        tier2_anime = "[HorribleSubs] Frieren - Beyond Journey's End - 01 [1080p] [87654321].mkv"

        t1_eval = evaluate_trash_release(tier1_anime, formats=catalog, profile=anime_profile)
        t2_eval = evaluate_trash_release(tier2_anime, formats=catalog, profile=anime_profile)

        assert t1_eval.total_score >= 1000
        assert t2_eval.total_score >= 800
        assert t1_eval.total_score > t2_eval.total_score
