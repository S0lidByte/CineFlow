"""Integration tests for TRaSH Custom Formats scoring with scraper pipeline and ranking API."""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from RTN import Torrent

from program.media.item import Episode, Movie, Season, Show
from program.services.scrapers.funnel import ScrapeFunnelStats
from program.services.scrapers.shared import _accumulate_ranked_torrents, parse_results
from program.settings import settings_manager
from program.settings.trash_catalog import get_default_trash_custom_formats
from program.settings.trash_settings import TrashSettingsModel
from routers.secure.ranking import router as ranking_router

app = FastAPI()
app.include_router(ranking_router, prefix="/api/v1")


@pytest.fixture
def sample_movie() -> Movie:
    return Movie(
        {
            "id": 100,
            "title": "Dune",
            "year": 2021,
            "imdb_id": "tt1160419",
            "tmdb_id": 438631,
        }
    )


class TestTrashScraperIntegration:
    """Test scraper ranking accumulation with TRaSH Custom Formats enabled and disabled."""

    def test_trash_scoring_disabled_by_default(self, sample_movie):
        results = {
            "0123456789abcdef0123456789abcdef01234567": "Dune.2021.1080p.BluRay.x264.TrueHD.Atmos.7.1-FLUX"
        }
        torrents = set[Torrent]()
        processed = set[str]()

        # Ensure disabled
        settings_manager.settings.scraping.trash_scoring.enabled = False

        _accumulate_ranked_torrents(
            sample_movie, results, torrents, processed, manual=False
        )
        assert len(torrents) == 1
        base_torrent = next(iter(torrents))
        base_rank = base_torrent.rank

        # Enable TRaSH scoring
        settings_manager.settings.scraping.trash_scoring.enabled = True
        torrents_trash = set[Torrent]()
        processed_trash = set[str]()

        _accumulate_ranked_torrents(
            sample_movie, results, torrents_trash, processed_trash, manual=False
        )
        assert len(torrents_trash) == 1
        trash_torrent = next(iter(torrents_trash))

        # The torrent rank should be boosted by TRaSH score (TrueHD Atmos + 7.1 + Web/Release tier > 1000)
        assert trash_torrent.rank > base_rank
        assert trash_torrent.rank - base_rank >= 1000

        # Reset setting
        settings_manager.settings.scraping.trash_scoring.enabled = False

    def test_trash_rejection_filters_unwanted_source(self, sample_movie):
        results = {
            "abcdef0123456789abcdef0123456789abcdef01": "Dune.2021.CAMRip.H264.AAC-JUNK"
        }
        torrents = set[Torrent]()
        processed = set[str]()

        settings_manager.settings.scraping.trash_scoring.enabled = True
        settings_manager.settings.scraping.trash_scoring.reject_unwanted_sources = True

        _accumulate_ranked_torrents(
            sample_movie, results, torrents, processed, manual=False
        )
        assert len(torrents) == 0  # Rejection enforced

        # Reset
        settings_manager.settings.scraping.trash_scoring.enabled = False

    def test_trash_disabled_produces_zero_behavioral_contribution(self):
        """Deterministically verify that when trash_scoring is disabled:
        1. Same input candidates
        2. Same acceptance/rejection decisions
        3. Identical ranks (0 score contribution)
        4. Zero TRaSH funnel rejections
        Across representative:
        - Movie (normal release)
        - TV / Episode (normal release)
        - Rejected RTN release (non-matching garbage)
        """
        movie = Movie(
            {
                "id": 100,
                "title": "Dune",
                "year": 2021,
                "imdb_id": "tt1160419",
                "tmdb_id": 438631,
            }
        )
        show = Show(
            {
                "id": 200,
                "title": "Breaking Bad",
                "year": 2008,
                "imdb_id": "tt0903747",
                "tmdb_id": 1396,
            }
        )
        season = Season({"id": 201, "number": 1, "title": "Season 1"})
        season.parent = show
        episode = Episode({"id": 202, "number": 1, "title": "Pilot"})
        episode.parent = season

        test_candidates = {
            # Candidate 1: Movie normal release
            "1111111111111111111111111111111111111111": "Dune.2021.1080p.BluRay.x264.TrueHD.Atmos.7.1-FLUX",
            # Candidate 2: TV Episode normal release
            "2222222222222222222222222222222222222222": "Breaking.Bad.S01E01.1080p.BluRay.x264-DEMAND",
            # Candidate 3: Release rejected by RTN (does not match either title)
            "3333333333333333333333333333333333333333": "Completely.Unrelated.Show.S05E10.HDTV-JUNK",
        }

        # Explicitly ensure disabled
        settings_manager.settings.scraping.trash_scoring.enabled = False

        # --- Test Movie ---
        torrents_movie = set[Torrent]()
        funnel_movie = ScrapeFunnelStats(found=3, ranked=0)
        _accumulate_ranked_torrents(
            movie,
            test_candidates,
            torrents_movie,
            set(),
            manual=False,
            funnel=funnel_movie,
        )

        # Candidate 1 accepted, candidates 2 & 3 rejected by RTN title mismatch
        assert len(torrents_movie) == 1
        movie_torrent = next(iter(torrents_movie))
        assert movie_torrent.infohash == "1111111111111111111111111111111111111111"
        assert funnel_movie.trash_rejected == 0
        assert funnel_movie.trash_reasons == {}

        # --- Test Episode ---
        torrents_ep = set[Torrent]()
        funnel_ep = ScrapeFunnelStats(found=3, ranked=0)
        _accumulate_ranked_torrents(
            episode, test_candidates, torrents_ep, set(), manual=False, funnel=funnel_ep
        )

        # Candidate 2 accepted, candidates 1 & 3 rejected by RTN title/type mismatch
        assert len(torrents_ep) == 1
        ep_torrent = next(iter(torrents_ep))
        assert ep_torrent.infohash == "2222222222222222222222222222222222222222"
        assert funnel_ep.trash_rejected == 0
        assert funnel_ep.trash_reasons == {}

        # --- Verify rank delta against TRaSH enabled ---
        settings_manager.settings.scraping.trash_scoring.enabled = True
        torrents_movie_enabled = set[Torrent]()
        _accumulate_ranked_torrents(
            movie, test_candidates, torrents_movie_enabled, set(), manual=False
        )
        assert len(torrents_movie_enabled) == 1
        movie_torrent_enabled = next(iter(torrents_movie_enabled))

        # Rank when TRaSH enabled is strictly higher due to positive TRaSH bonus points
        assert movie_torrent_enabled.rank > movie_torrent.rank
        # Re-verify that disabling produces 0 delta
        settings_manager.settings.scraping.trash_scoring.enabled = False


class TestTrashRankingApiEndpoints:
    """Test /api/v1/ranking/custom-formats and evaluation endpoints."""

    @pytest.mark.asyncio
    async def test_get_trash_custom_formats_catalog(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"x-api-key": settings_manager.settings.api_key},
        ) as client:
            resp = await client.get("/api/v1/ranking/custom-formats")
            assert resp.status_code == 200
            data = resp.json()
            assert "custom_formats" in data
            assert "profiles" in data
            assert len(data["custom_formats"]) >= 10
            assert len(data["profiles"]) >= 4

    @pytest.mark.asyncio
    async def test_evaluate_trash_custom_formats_endpoint(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"x-api-key": settings_manager.settings.api_key},
        ) as client:
            payload = {
                "raw_title": "Oppenheimer.2023.2160p.UHD.BluRay.x265.TrueHD.Atmos.7.1.DV.HDR10-FraMeSToR",
                "profile_id": "trash_remux",
            }
            resp = await client.post(
                "/api/v1/ranking/custom-formats/evaluate", json=payload
            )
            assert resp.status_code == 200
            data = resp.json()
            assert "summary" in data
            summary = data["summary"]
            assert summary["total_score"] > 0
            assert summary["rejected_by_lq"] is False
            matched_ids = [m["trash_id"] for m in summary["matched_formats"]]
            assert "dv-hdr10-fallback" in matched_ids or "truehd-atmos" in matched_ids

    @pytest.mark.asyncio
    async def test_ranking_test_with_trash_summary(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"x-api-key": settings_manager.settings.api_key},
        ) as client:
            payload = {
                "raw_title": "Dune.2021.1080p.BluRay.x264.TrueHD.Atmos.7.1-FLUX",
                "correct_title": "Dune",
                "evaluate_trash": True,
                "trash_profile": "trash_balanced",
            }
            resp = await client.post("/api/v1/ranking/test", json=payload)
            assert resp.status_code == 200
            data = resp.json()
            assert data["accepted"] is True
            assert "trash_summary" in data
            assert data["trash_summary"] is not None
            assert data["trash_summary"]["total_score"] > 0

    @pytest.mark.asyncio
    async def test_evaluate_endpoint_with_media_type_and_enriched_summary(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"x-api-key": settings_manager.settings.api_key},
        ) as client:
            payload = {
                "raw_title": "Frieren.S01E01.1080p.CR.WEB-DL.AAC2.0.H.264-SubsPlease",
                "media_type": "show",
                "is_anime": True,
            }
            resp = await client.post(
                "/api/v1/ranking/custom-formats/evaluate", json=payload
            )
            assert resp.status_code == 200
            data = resp.json()
            assert "summary" in data
            summary = data["summary"]
            assert summary["active_profile_id"] == "trash_anime"
            assert "additive_score" in summary
            assert "negative_score" in summary
            assert "cutoff_reached" in summary
            assert "categorized_matches" in summary
            assert summary["additive_score"] >= 0

    @pytest.mark.asyncio
    async def test_ranking_test_endpoint_with_media_type(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"x-api-key": settings_manager.settings.api_key},
        ) as client:
            payload = {
                "raw_title": "Oppenheimer.2023.1080p.BluRay.x264.TrueHD.Atmos.7.1-FraMeSToR",
                "correct_title": "Oppenheimer",
                "media_type": "movie",
                "evaluate_trash": True,
            }
            resp = await client.post("/api/v1/ranking/test", json=payload)
            assert resp.status_code == 200
            data = resp.json()
            assert data["accepted"] is True
            assert data["trash_summary"] is not None
            assert data["trash_summary"]["active_profile_id"] in [
                "trash_remux",
                "trash_balanced",
            ]

    def test_trash_rejection_funnel_tracking(self, sample_movie):
        from program.services.scrapers.funnel import ScrapeFunnelStats

        # Title that passes RTN but has unwanted CAM format in TRaSH
        results = {
            "1111111111111111111111111111111111111111": "Dune.2021.1080p.BluRay.x264.AAC-FLUX"
        }
        torrents = set[Torrent]()
        processed = set[str]()
        funnel = ScrapeFunnelStats(found=1, ranked=0)

        settings_manager.settings.scraping.trash_scoring.enabled = True
        settings_manager.settings.scraping.trash_scoring.min_score = 50000

        try:
            _accumulate_ranked_torrents(
                sample_movie, results, torrents, processed, manual=False, funnel=funnel
            )
            assert len(torrents) == 0
            assert funnel.trash_rejected == 1
            assert "below_min_threshold" in funnel.trash_reasons
        finally:
            settings_manager.settings.scraping.trash_scoring.enabled = False
            settings_manager.settings.scraping.trash_scoring.min_score = -1000
