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
        assert (
            evaluate_trash_condition(
                cond, "Movie.2024.1080p.Remux.AVC.DTS-HD.MA.5.1-GROUP"
            )
            is True
        )
        assert (
            evaluate_trash_condition(cond, "Movie.2024.1080p.WEB-DL.DDP5.1.H.264-GROUP")
            is False
        )

    def test_case_insensitive_by_default(self):
        cond = TrashCondition(pattern=r"\btruehd\b", required=True)
        assert (
            evaluate_trash_condition(cond, "Movie.2024.2160p.TrueHD.Atmos.7.1") is True
        )
        assert (
            evaluate_trash_condition(cond, "Movie.2024.2160p.TRUEHD.Atmos.7.1") is True
        )
        assert (
            evaluate_trash_condition(cond, "Movie.2024.2160p.truehd.Atmos.7.1") is True
        )

    def test_negated_condition(self):
        cond = TrashCondition(
            pattern=r"\b(HDR|DV|Dolby\.?Vision)\b", required=True, negate=True
        )
        # Should be True when pattern does NOT match
        assert (
            evaluate_trash_condition(cond, "Movie.2024.1080p.SDR.BluRay.x264-GROUP")
            is True
        )
        # Should be False when pattern DOES match
        assert (
            evaluate_trash_condition(cond, "Movie.2024.2160p.UHD.HDR.BluRay.x265-GROUP")
            is False
        )

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
        res1 = evaluate_trash_custom_format(
            cf, "Movie.2024.2160p.Remux.TrueHD.Atmos.x265"
        )
        assert res1.matched is True
        assert res1.score == 1500

        # Passes required (TrueHD) and optional (7.1)
        res2 = evaluate_trash_custom_format(
            cf, "Movie.2024.2160p.Remux.TrueHD.7.1.x265"
        )
        assert res2.matched is True
        assert res2.score == 1500

        # Fails optional conditions (neither Atmos nor 7.1)
        res3 = evaluate_trash_custom_format(
            cf, "Movie.2024.2160p.Remux.TrueHD.5.1.x265"
        )
        assert res3.matched is False

        # Fails required condition (no TrueHD)
        res4 = evaluate_trash_custom_format(
            cf, "Movie.2024.2160p.Remux.DTS-HD.MA.7.1.Atmos.x265"
        )
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
        res2 = evaluate_trash_custom_format(
            cf, "Movie.2024.1080p.Remux", score_override=3500
        )
        assert res2.score == 3500


class TestTrashCatalogAndEvaluation:
    """Test full release evaluation against the standard TRaSH Guides catalog."""

    @pytest.fixture
    def catalog(self):
        return get_default_trash_custom_formats()

    def test_dolby_vision_with_hdr10_fallback(self, catalog):
        # Hybrid release having both DV and HDR10
        dv_hdr10_title = (
            "Movie.2024.UHD.BluRay.2160p.TrueHD.Atmos.7.1.DV.HDR10.HEVC.REMUX-FraMeSToR"
        )
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
        summary = evaluate_trash_release(
            cam_title, formats=catalog, reject_unwanted_sources=True
        )

        assert summary.rejected_by_lq is True
        assert (
            "unwanted source" in summary.rejection_reason.lower()
            or "rejected" in summary.rejection_reason.lower()
        )
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
        summary = evaluate_trash_release(
            title, formats=formats, reject_negative_scores=True
        )

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

        remux_eval = evaluate_trash_release(
            remux_title, formats=catalog, profile=remux_profile
        )
        webdl_eval = evaluate_trash_release(
            webdl_title, formats=catalog, profile=remux_profile
        )

        assert remux_eval.total_score > webdl_eval.total_score
        assert remux_eval.total_score >= 3500

    def test_anime_fansub_tier_scoring(self):
        catalog = get_default_trash_custom_formats()
        profiles = {p.profile_id: p for p in get_default_trash_profiles()}
        anime_profile = profiles["trash_anime"]

        tier1_anime = (
            "[SubsPlease] Frieren - Beyond Journey's End - 01 (1080p) [12345678].mkv"
        )
        tier2_anime = (
            "[HorribleSubs] Frieren - Beyond Journey's End - 01 [1080p] [87654321].mkv"
        )

        t1_eval = evaluate_trash_release(
            tier1_anime, formats=catalog, profile=anime_profile
        )
        t2_eval = evaluate_trash_release(
            tier2_anime, formats=catalog, profile=anime_profile
        )

        assert t1_eval.total_score >= 1000
        assert t2_eval.total_score >= 800
        assert t1_eval.total_score > t2_eval.total_score


class TestTrashHardeningAndSafety:
    """Test regex security bounds, timeout handling, and length limits."""

    def test_pattern_exceeding_max_length_handled_safely(self):
        long_pattern = r"\b" + "a" * 1050 + r"\b"
        cond = TrashCondition(pattern=long_pattern, required=True)
        # Should not crash, returns False safely
        assert evaluate_trash_condition(cond, "Sample Release Title") is False

    def test_raw_title_exceeding_max_length_truncated(self):
        cond = TrashCondition(pattern=r"\b(TARGET_MATCH)\b", required=True)
        # Target within 2048 chars matches
        long_title_near = "A" * 100 + " TARGET_MATCH " + "B" * 3000
        assert evaluate_trash_condition(cond, long_title_near) is True

        # Target beyond 2048 chars is truncated away
        long_title_far = "A" * 2100 + " TARGET_MATCH"
        assert evaluate_trash_condition(cond, long_title_far) is False

    def test_invalid_regex_syntax_handled_gracefully(self):
        cond = TrashCondition(pattern=r"[unclosed-bracket", required=True)
        assert evaluate_trash_condition(cond, "Sample Release Title") is False

    def test_slash_wrapped_pattern_evaluated_without_slashes(self):
        cond = TrashCondition(pattern=r"/Remux/", required=True)
        assert evaluate_trash_condition(cond, "Movie.1080p.Remux.x264") is True


class TestTrashEnrichedSummary:
    """Test enriched evaluation summary fields and unified format evaluation."""

    def test_unified_format_evaluate_method(self):
        cf = TrashCustomFormat(
            trash_id="unified_cf",
            name="Unified Test",
            category="audio_advanced",
            score=750,
            conditions=[TrashCondition(pattern=r"\bTrueHD\b", required=True)],
        )
        # TrashCustomFormat.evaluate() delegates to evaluate_trash_custom_format()
        res1 = cf.evaluate("Movie.2024.TrueHD.Atmos")
        res2 = evaluate_trash_custom_format(cf, "Movie.2024.TrueHD.Atmos")
        assert res1 is True
        assert res1 == res2.matched
        assert res2.score == 750
        assert res2.name == "Unified Test"

    def test_additive_and_negative_score_separation(self):
        formats = [
            TrashCustomFormat(
                trash_id="pos_audio",
                name="Pos Audio",
                category="audio_advanced",
                score=700,
                conditions=[TrashCondition(pattern=r"\bAtmos\b", required=True)],
            ),
            TrashCustomFormat(
                trash_id="pos_video",
                name="Pos Video",
                category="hdr_dv",
                score=800,
                conditions=[TrashCondition(pattern=r"\bDV\b", required=True)],
            ),
            TrashCustomFormat(
                trash_id="neg_lq",
                name="Neg LQ",
                category="unwanted_lq",
                score=-1500,
                conditions=[TrashCondition(pattern=r"\bYTS\b", required=True)],
            ),
        ]
        summary = evaluate_trash_release("Movie.2024.DV.Atmos.YTS", formats=formats)
        assert summary.total_score == 0
        assert summary.additive_score == 1500
        assert summary.negative_score == -1500
        assert "audio_advanced" in summary.categorized_matches
        assert "hdr_dv" in summary.categorized_matches
        assert "unwanted_lq" in summary.categorized_matches

    def test_cutoff_reached_flag(self):
        prof = TrashProfile(
            profile_id="cutoff_test",
            name="Cutoff Test",
            cutoff_score=1000,
            format_scores={},
        )
        cf = TrashCustomFormat(
            trash_id="score_cf",
            name="High Score",
            category="hdr_dv",
            score=1200,
            conditions=[TrashCondition(pattern=r"\bUHD\b", required=True)],
        )
        summary = evaluate_trash_release("Movie.2024.UHD", formats=[cf], profile=prof)
        assert summary.cutoff_reached is True
        assert summary.active_profile_id == "cutoff_test"

        # Below cutoff
        cf_low = TrashCustomFormat(
            trash_id="low_cf",
            name="Low Score",
            category="hdr_dv",
            score=500,
            conditions=[TrashCondition(pattern=r"\bHD\b", required=True)],
        )
        summary_low = evaluate_trash_release(
            "Movie.2024.HD", formats=[cf_low], profile=prof
        )
        assert summary_low.cutoff_reached is False


class TestTrashProfileEngine:
    """Test media-type aware profile engine and fallback hierarchy."""

    def test_profile_resolution_hierarchy(self):
        from program.settings.trash_settings import TrashSettingsModel

        settings = TrashSettingsModel(
            active_profile="trash_balanced",
            movie_profile="trash_remux",
            show_profile="trash_webdl",
            anime_profile="trash_anime",
        )

        # Movies resolve to movie_profile
        movie_prof = settings.resolve_profile(media_type="movie")
        assert movie_prof is not None
        assert movie_prof.profile_id == "trash_remux"

        # Shows resolve to show_profile
        show_prof = settings.resolve_profile(media_type="show")
        assert show_prof is not None
        assert show_prof.profile_id == "trash_webdl"

        # Series also resolve to show_profile
        series_prof = settings.resolve_profile(media_type="series")
        assert series_prof is not None
        assert series_prof.profile_id == "trash_webdl"

        # Anime resolves to anime_profile
        anime_prof = settings.resolve_profile(is_anime=True)
        assert anime_prof is not None
        assert anime_prof.profile_id == "trash_anime"

        # Fallback to active_profile when specific profile not set
        settings_fallback = TrashSettingsModel(active_profile="trash_balanced")
        assert (
            settings_fallback.resolve_profile(media_type="movie").profile_id
            == "trash_balanced"
        )
        assert (
            settings_fallback.resolve_profile(media_type="show").profile_id
            == "trash_balanced"
        )
        # Anime defaults to "trash_anime" preset in default profiles if available
        assert (
            settings_fallback.resolve_profile(is_anime=True).profile_id == "trash_anime"
        )

    def test_get_profile_for_item_mock(self):
        from program.settings.trash_settings import TrashSettingsModel

        settings = TrashSettingsModel(
            active_profile="trash_balanced",
            movie_profile="trash_remux",
            show_profile="trash_webdl",
        )

        class MockMovie:
            type = "movie"
            is_anime = False

        class MockShow:
            type = "show"
            is_anime = False

        class MockAnime:
            type = "show"
            is_anime = True

        assert settings.get_profile_for_item(MockMovie()).profile_id == "trash_remux"
        assert settings.get_profile_for_item(MockShow()).profile_id == "trash_webdl"
        assert settings.get_profile_for_item(MockAnime()).profile_id == "trash_anime"


class TestRegexTimeoutAndSafety:
    """Test regex timeout handling, catastrophic backtracking safety, and lack of unbounded 're' fallback."""

    def test_regex_timeout_caught_gracefully(self, monkeypatch):
        import program.services.scrapers.trash_scorer as ts

        # Set a tiny timeout to deterministically trigger TimeoutError
        monkeypatch.setattr(ts, "REGEX_TIMEOUT_SECONDS", 0.000001)

        cond = TrashCondition(pattern=r"(.*a){25}", required=True)
        # 200 characters will exceed the 1 microsecond timeout during matching
        result = evaluate_trash_condition(cond, "a" * 200)

        # Must return False gracefully without raising or hanging
        assert result is False

    def test_no_re_fallback_in_trash_scorer(self):
        """Verify that trash_scorer imports and uses 'regex' (regex_lib) and has NO fallback to 're'."""
        import program.services.scrapers.trash_scorer as ts

        # Confirm regex_lib is the C-extension regex module supporting timeouts
        assert hasattr(ts, "regex_lib")
        assert ts.regex_lib.__name__ == "regex"

        # Verify 're' is not present in trash_scorer namespace
        assert "re" not in ts.__dict__


class TestMediaTypeRoutingPrecedence:
    """Exhaustively verify media type routing precedence across all 10 required matrix cases."""

    def test_case_1_movie_with_movie_profile_set(self):
        from program.settings.trash_settings import TrashSettingsModel

        settings = TrashSettingsModel(
            active_profile="trash_balanced",
            movie_profile="trash_remux",
        )
        assert settings.resolve_profile(media_type="movie").profile_id == "trash_remux"

    def test_case_2_movie_with_movie_profile_unset(self):
        from program.settings.trash_settings import TrashSettingsModel

        settings = TrashSettingsModel(
            active_profile="trash_balanced",
            movie_profile=None,
        )
        assert (
            settings.resolve_profile(media_type="movie").profile_id == "trash_balanced"
        )

    def test_case_3_show_with_show_profile_set(self):
        from program.settings.trash_settings import TrashSettingsModel

        settings = TrashSettingsModel(
            active_profile="trash_balanced",
            show_profile="trash_webdl",
        )
        assert settings.resolve_profile(media_type="show").profile_id == "trash_webdl"

    def test_case_4_show_with_show_profile_unset(self):
        from program.settings.trash_settings import TrashSettingsModel

        settings = TrashSettingsModel(
            active_profile="trash_balanced",
            show_profile=None,
        )
        assert (
            settings.resolve_profile(media_type="show").profile_id == "trash_balanced"
        )

    def test_case_5_season_with_show_profile_set(self):
        from program.settings.trash_settings import TrashSettingsModel

        settings = TrashSettingsModel(
            active_profile="trash_balanced",
            show_profile="trash_webdl",
        )
        assert settings.resolve_profile(media_type="season").profile_id == "trash_webdl"

    def test_case_6_season_with_show_profile_unset(self):
        from program.settings.trash_settings import TrashSettingsModel

        settings = TrashSettingsModel(
            active_profile="trash_balanced",
            show_profile=None,
        )
        assert (
            settings.resolve_profile(media_type="season").profile_id == "trash_balanced"
        )

    def test_case_7_episode_with_show_profile_set(self):
        from program.settings.trash_settings import TrashSettingsModel

        settings = TrashSettingsModel(
            active_profile="trash_balanced",
            show_profile="trash_webdl",
        )
        assert (
            settings.resolve_profile(media_type="episode").profile_id == "trash_webdl"
        )

    def test_case_8_episode_with_show_profile_unset(self):
        from program.settings.trash_settings import TrashSettingsModel

        settings = TrashSettingsModel(
            active_profile="trash_balanced",
            show_profile=None,
        )
        assert (
            settings.resolve_profile(media_type="episode").profile_id
            == "trash_balanced"
        )

    def test_case_9_anime_with_anime_profile_set(self):
        from program.settings.trash_settings import TrashSettingsModel

        # Set dedicated custom anime profile
        prof = TrashProfile(profile_id="custom_anime", name="Custom Anime")
        settings = TrashSettingsModel(
            active_profile="trash_balanced",
            anime_profile="custom_anime",
            profiles=[prof] + get_default_trash_profiles(),
        )
        # Any media type flagged as is_anime resolves to anime_profile
        for m_type in ["movie", "show", "season", "episode"]:
            assert (
                settings.resolve_profile(media_type=m_type, is_anime=True).profile_id
                == "custom_anime"
            )

    def test_case_10_anime_with_anime_profile_unset(self):
        from program.settings.trash_settings import TrashSettingsModel

        settings = TrashSettingsModel(
            active_profile="trash_balanced",
            anime_profile=None,
        )
        # When anime_profile is unset, falls back to default 'trash_anime' profile
        assert (
            settings.resolve_profile(media_type="show", is_anime=True).profile_id
            == "trash_anime"
        )

        # When 'trash_anime' is not present in profiles, falls back to active_profile
        settings_no_anime_prof = TrashSettingsModel(
            active_profile="trash_balanced",
            anime_profile=None,
            profiles=[TrashProfile(profile_id="trash_balanced", name="Balanced")],
        )
        assert (
            settings_no_anime_prof.resolve_profile(is_anime=True).profile_id
            == "trash_balanced"
        )

    def test_explicit_profile_id_takes_highest_precedence(self):
        from program.settings.trash_settings import TrashSettingsModel

        settings = TrashSettingsModel(
            active_profile="trash_balanced",
            movie_profile="trash_remux",
            anime_profile="trash_anime",
        )
        # Even for anime movie, explicit profile_id wins
        resolved = settings.resolve_profile(
            media_type="movie", is_anime=True, profile_id="trash_webdl"
        )
        assert resolved.profile_id == "trash_webdl"


class TestSettingsAliasIdentityAndMutation:
    """Verify pointer identity, bidirectional mutation, and serialization of settings alias."""

    def test_alias_in_memory_identity_and_bidirectional_mutation(self):
        from program.settings.models import AppModel

        app = AppModel()

        # 1. In-memory object identity
        assert app.ranking.trash_scoring is app.scraping.trash_scoring
        assert app.ranking_anime.trash_scoring is app.scraping.trash_scoring
        assert app.trash_scoring is app.scraping.trash_scoring

        # 2. Mutating through scraping reflects in ranking
        app.scraping.trash_scoring.enabled = True
        app.scraping.trash_scoring.min_score = 150
        assert app.ranking.trash_scoring.enabled is True
        assert app.ranking.trash_scoring.min_score == 150
        assert app.ranking_anime.trash_scoring.enabled is True

        # 3. Mutating through ranking reflects in scraping
        app.ranking.trash_scoring.enabled = False
        app.ranking.trash_scoring.reject_negative_scores = True
        assert app.scraping.trash_scoring.enabled is False
        assert app.scraping.trash_scoring.reject_negative_scores is True

    def test_alias_serialization_and_conflict_resolution(self):
        from program.settings.models import AppModel

        app = AppModel()
        app.scraping.trash_scoring.enabled = True
        app.scraping.trash_scoring.min_score = 500

        # Dump to dictionary
        dumped = app.model_dump()
        assert "trash_scoring" in dumped["scraping"]

        # Validate back from dictionary
        reloaded = AppModel.model_validate(dumped)
        assert reloaded.ranking.trash_scoring is reloaded.scraping.trash_scoring
        assert reloaded.scraping.trash_scoring.enabled is True
        assert reloaded.scraping.trash_scoring.min_score == 500

        # Conflict resolution: if both paths are supplied in a raw dictionary,
        # scraping.trash_scoring is canonical and ranking binds to it.
        conflicting_raw = {
            "scraping": {"trash_scoring": {"enabled": True, "min_score": 999}},
            "ranking": {"trash_scoring": {"enabled": False, "min_score": -100}},
        }
        app_conflict = AppModel(**conflicting_raw)
        assert app_conflict.scraping.trash_scoring.enabled is True
        assert app_conflict.scraping.trash_scoring.min_score == 999
        assert app_conflict.ranking.trash_scoring is app_conflict.scraping.trash_scoring
        assert app_conflict.ranking.trash_scoring.enabled is True
        assert app_conflict.ranking.trash_scoring.min_score == 999
        assert (
            app_conflict.scraping.trash_scoring.get_profile_for_item(None).profile_id
            == "trash_balanced"
        )


class TestTrashExpandedCatalog:
    """Test newly added curated custom formats."""

    @pytest.fixture
    def catalog(self):
        return get_default_trash_custom_formats()

    def test_bad_dual_groups_format(self, catalog):
        title = "Anime.Series.S01E01.Dual-Audio-Poor.1080p"
        summary = evaluate_trash_release(title, formats=catalog)
        matched_ids = {m.trash_id for m in summary.matched_formats}
        assert "bad-dual-groups" in matched_ids

    def test_retags_format(self, catalog):
        title = "Movie.2024.1080p.WEBRip.[rarbg].x264"
        summary = evaluate_trash_release(title, formats=catalog)
        matched_ids = {m.trash_id for m in summary.matched_formats}
        assert "retags" in matched_ids

    def test_x265_hd_penalty_applies_to_sdr(self, catalog):
        # 1080p x265 without HDR/DV should receive penalty
        sdr_title = "Movie.2023.1080p.BluRay.x265.10bit.DTS-GROUP"
        summary = evaluate_trash_release(sdr_title, formats=catalog)
        matched_ids = {m.trash_id for m in summary.matched_formats}
        assert "x265-hd-penalty" in matched_ids

        # 1080p x265 WITH HDR should NOT receive penalty
        hdr_title = "Movie.2023.1080p.BluRay.x265.HDR.DTS-GROUP"
        summary_hdr = evaluate_trash_release(hdr_title, formats=catalog)
        matched_ids_hdr = {m.trash_id for m in summary_hdr.matched_formats}
        assert "x265-hd-penalty" not in matched_ids_hdr

    def test_streaming_services_formats(self, catalog):
        atvp_title = "Ted.Lasso.S03E01.2160p.ATVP.WEB-DL.DDP5.1.Atmos.DV.H.265-FLUX"
        summary = evaluate_trash_release(atvp_title, formats=catalog)
        matched_ids = {m.trash_id for m in summary.matched_formats}
        assert "streaming-apple-atvp" in matched_ids

        dsnp_title = "Loki.S02E01.2160p.DSNP.WEB-DL.DDP5.1.Atmos.DV.H.265-FLUX"
        summary_dsnp = evaluate_trash_release(dsnp_title, formats=catalog)
        matched_ids_dsnp = {m.trash_id for m in summary_dsnp.matched_formats}
        assert "streaming-disney-dsnp" in matched_ids_dsnp

    def test_audio_pcm_and_imax_enhanced(self, catalog):
        pcm_title = "Concert.Live.1080p.BluRay.PCM.2.0.AVC-GROUP"
        summary_pcm = evaluate_trash_release(pcm_title, formats=catalog)
        matched_ids_pcm = {m.trash_id for m in summary_pcm.matched_formats}
        assert "audio-pcm" in matched_ids_pcm

        imax_title = "Oppenheimer.2023.IMAX.Enhanced.2160p.UHD.BluRay.x265-GROUP"
        summary_imax = evaluate_trash_release(imax_title, formats=catalog)
        matched_ids_imax = {m.trash_id for m in summary_imax.matched_formats}
        assert "imax-enhanced" in matched_ids_imax


class TestTrashSettingsDualCompatibility:
    """Test dual access path compatibility between ranking.trash_scoring and scraping.trash_scoring."""

    def test_dual_settings_paths_resolve_to_same_instance(self):
        from program.settings import settings_manager

        ranking_trash = settings_manager.settings.ranking.trash_scoring
        scraping_trash = settings_manager.settings.scraping.trash_scoring
        app_trash = settings_manager.settings.trash_scoring

        assert ranking_trash is not None
        assert scraping_trash is not None
        assert ranking_trash is scraping_trash
        assert app_trash is scraping_trash

    def test_mutations_reflected_across_both_paths(self):
        from program.settings import settings_manager

        initial_state = settings_manager.settings.scraping.trash_scoring.enabled
        try:
            settings_manager.settings.ranking.trash_scoring.enabled = True
            assert settings_manager.settings.scraping.trash_scoring.enabled is True

            settings_manager.settings.scraping.trash_scoring.enabled = False
            assert settings_manager.settings.ranking.trash_scoring.enabled is False
        finally:
            settings_manager.settings.scraping.trash_scoring.enabled = initial_state


class TestTrashScraperFunnel:
    """Test scrape funnel stats for TRaSH rejection tracking."""

    def test_record_trash_reject_and_summary(self):
        from program.services.scrapers.funnel import ScrapeFunnelStats

        funnel = ScrapeFunnelStats(found=10, ranked=2)
        funnel.record_trash_reject(
            "Rejected by critical unwanted source format: Cam / Telesync"
        )
        funnel.record_trash_reject("Rejected due to negative TRaSH net score (-500)")
        funnel.record_trash_reject(
            "Rejected: TRaSH score (100) is below minimum threshold (500)"
        )

        assert funnel.trash_rejected == 3
        assert funnel.trash_reasons["unwanted_source"] == 1
        assert funnel.trash_reasons["negative_score"] == 1
        assert funnel.trash_reasons["below_min_threshold"] == 1

        summary = funnel.to_summary(item_id=42, item_log="Movie Test")
        assert summary["trash_rejected"] == 3
        assert len(summary["trash_top"]) == 3

        line = funnel.summary_line("Movie Test")
        assert "trash_rejected=3" in line
        assert "trash_top=[" in line
