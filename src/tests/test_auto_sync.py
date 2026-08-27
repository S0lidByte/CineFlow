import asyncio
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from kink import di

from program.media.item import Season, Show
from program.program import Program
from program.services.indexers import IndexerService
from program.utils.locking import ItemLock
from routers.secure.scrape import (
    AutoScrapeRequest,
    _matching_targeted_episodes,
    _normalize_episode_numbers,
    _requested_season_numbers,
    auto_scrape,
)


def test_episode_targets_are_normalized_and_select_seasons():
    request = AutoScrapeRequest(
        media_type="tv",
        tvdb_id="359913",
        season_numbers=[2],
        episode_numbers={"1": [3, 1, 3], "-1": [1], "3": [0]},
    )

    assert _normalize_episode_numbers(request.episode_numbers) == {1: {1, 3}}
    assert _requested_season_numbers(request) == {1, 2}


def test_matching_targeted_episodes_returns_only_requested_numbers():
    season = MagicMock()
    season.number = 1
    episode_one = MagicMock()
    episode_one.number = 1
    episode_two = MagicMock()
    episode_two.number = 2
    episode_three = MagicMock()
    episode_three.number = 3
    season.episodes = [episode_one, episode_two, episode_three]

    assert _matching_targeted_episodes(season, {1: {1, 3}}) == [
        episode_one,
        episode_three,
    ]
    assert _matching_targeted_episodes(season, {2: {1}}) is None


@pytest.mark.asyncio
async def test_auto_scrape_triggers_sync_when_seasons_missing():
    # Setup mock item and session
    mock_show = MagicMock(spec=Show)
    mock_show.id = 123
    mock_show.log_string = "Test Show"
    mock_show.seasons = []  # No seasons in DB

    # Mock database session
    mock_session = MagicMock()
    # Mock the execute call that refreshes the item
    mock_session.execute.return_value.scalar_one.return_value = mock_show

    # Mock the TVDB metadata updater used by the on-demand sync path.
    mock_season = MagicMock(spec=Season)
    mock_season.number = 1
    mock_season.id = 321
    mock_season.episodes = []
    mock_tvdb_indexer = MagicMock()

    def add_requested_season(show):
        show.seasons = [mock_season]
        return True

    mock_tvdb_indexer._update_show_metadata.side_effect = add_requested_season
    mock_indexer = MagicMock(spec=IndexerService)
    mock_indexer.tvdb_indexer = mock_tvdb_indexer

    request = AutoScrapeRequest(media_type="tv", tvdb_id="359913", season_numbers=[1])

    # Patch session and db_session context manager
    with patch("routers.secure.scrape.db_session") as mock_db_sess_cm:
        mock_db_sess_cm.return_value.__enter__.return_value = mock_session

        # Patch the current external-ID lookup used by resolve_media_item.
        with patch("program.db.db_functions.get_item_by_external_id", return_value=mock_show):
            with patch("routers.secure.scrape.di") as mock_di:
                mock_di[Program].em = MagicMock()
                with patch("program.program.riven.services") as mock_services:
                    mock_services.indexer = mock_indexer
                    # Patch get_ranking_overrides to return a basic model
                    with patch(
                        "routers.secure.scrape.get_ranking_overrides", return_value=MagicMock()
                    ):
                        # Execute
                        response = await auto_scrape(request)

    # Verify
    assert "Started scrape" in response.message
    mock_tvdb_indexer._update_show_metadata.assert_called_once_with(mock_show)
    mock_session.add.assert_called_once_with(mock_season)


@pytest.mark.asyncio
async def test_auto_scrape_concurrency_returns_202():
    # Setup mock item
    mock_show = MagicMock(spec=Show)
    mock_show.id = 456
    mock_show.log_string = "Concurrent Show"
    mock_show.seasons = []  # Missing seasons to trigger sync

    mock_session = MagicMock()
    mock_session.execute.return_value.scalar_one.return_value = mock_show

    mock_indexer = MagicMock(spec=IndexerService)
    # We want to test that ItemLock handles the concurrency
    # indexer.run is called via asyncio.to_thread(run_sync)
    # where run_sync consumes the generator.

    with patch("program.program.riven.services") as mock_services:
        mock_services.indexer = mock_indexer

        request = AutoScrapeRequest(
            media_type="tv", tvdb_id="359913", season_numbers=[1]
        )

    # Manually acquire lock to simulate another sync in progress
    await ItemLock.acquire(mock_show.id)

    with patch("routers.secure.scrape.db_session") as mock_db_sess_cm:
        mock_db_sess_cm.return_value.__enter__.return_value = mock_session
        with patch("program.db.db_functions.get_item_by_external_id", return_value=mock_show):
            with patch(
                "routers.secure.scrape.get_ranking_overrides", return_value=MagicMock()
            ):
                # Execute
                response = await auto_scrape(request)

                # Verify
                assert "Sync already in progress" in response.message
                # Indexer should NOT be called because lock was already held
                assert mock_indexer.run.call_count == 0

    # Cleanup
    ItemLock.release(mock_show.id)


@pytest.mark.asyncio
async def test_auto_scrape_handles_sync_timeout():
    mock_show = MagicMock(spec=Show)
    mock_show.id = 789
    mock_show.log_string = "Timeout Show"
    mock_show.seasons = []

    mock_session = MagicMock()

    mock_tvdb_indexer = MagicMock()
    mock_tvdb_indexer._update_show_metadata.side_effect = asyncio.TimeoutError
    mock_indexer = MagicMock(spec=IndexerService)
    mock_indexer.tvdb_indexer = mock_tvdb_indexer

    request = AutoScrapeRequest(
        media_type="tv", tvdb_id="359913", season_numbers=[1]
    )

    mock_session.execute.return_value.scalar_one.return_value = mock_show
    ItemLock.release(mock_show.id)
    mock_tvdb_indexer._update_show_metadata.side_effect = asyncio.TimeoutError

    with patch("program.program.riven.services") as mock_services:
        mock_services.indexer = mock_indexer
        with patch("routers.secure.scrape.db_session") as mock_db_sess_cm:
            mock_db_sess_cm.return_value.__enter__.return_value = mock_session
            with patch("program.db.db_functions.get_item_by_external_id", return_value=mock_show):
                with patch("routers.secure.scrape.di") as mock_di:
                    mock_di[Program].em = MagicMock()
                    with patch(
                        "routers.secure.scrape.get_ranking_overrides", return_value=MagicMock()
                    ):
                        with pytest.raises(HTTPException) as excinfo:
                            await auto_scrape(request)
                        assert excinfo.value.status_code == 504
                        assert "Metadata sync timed out" in excinfo.value.detail

    # Verify lock is released even on timeout
    assert not (await ItemLock.get_lock(mock_show.id)).locked()
