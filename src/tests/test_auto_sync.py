import asyncio
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from program.media.item import Show
from program.services.indexers import IndexerService
from program.utils.locking import ItemLock
from routers.secure.scrape import AutoScrapeRequest, auto_scrape


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

    # Mock IndexerService
    mock_indexer = MagicMock(spec=IndexerService)
    mock_indexer.run.return_value = iter([mock_show])  # Generator returns the item

    # Setup DI mock
    with patch("program.program.riven.services") as mock_services:
        mock_services.indexer = mock_indexer

    request = AutoScrapeRequest(media_type="tv", tvdb_id="359913", season_numbers=[1])

    # Patch session and db_session context manager
    with patch("routers.secure.scrape.db_session") as mock_db_sess_cm:
        mock_db_sess_cm.return_value.__enter__.return_value = mock_session

        # Patch db_functions.get_item which is called before the season check
        with patch("program.db.db_functions.get_item", return_value=mock_show):
            # Patch get_ranking_overrides to return a basic model
            with patch(
                "routers.secure.scrape.get_ranking_overrides", return_value=MagicMock()
            ):
                # Execute
                response = await auto_scrape(request)

                # Verify
                assert "Started scrape" in response.message
                mock_indexer.run.assert_called_once_with(mock_show)
                mock_session.expire.assert_called_with(mock_show)


@pytest.mark.asyncio
async def test_auto_scrape_concurrency_returns_202():
    # Setup mock item
    mock_show = MagicMock(spec=Show)
    mock_show.id = 456
    mock_show.log_string = "Concurrent Show"
    mock_show.seasons = []  # Missing seasons to trigger sync

    mock_session = MagicMock()
    mock_session.execute.return_value.scalar_one.return_value = mock_show

    # Mock IndexerService with a delay to simulate work
    async def slow_sync():
        await asyncio.sleep(0.5)
        return iter([mock_show])

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
        with patch("program.db.db_functions.get_item", return_value=mock_show):
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

    with patch("program.program.riven.services") as mock_services:
        mock_services.indexer = MagicMock(spec=IndexerService)

        request = AutoScrapeRequest(
            media_type="tv", tvdb_id="359913", season_numbers=[1]
        )

    with patch("routers.secure.scrape.db_session") as mock_db_sess_cm:
        mock_db_sess_cm.return_value.__enter__.return_value = mock_session
        with patch("program.db.db_functions.get_item", return_value=mock_show):
            with patch(
                "routers.secure.scrape.get_ranking_overrides", return_value=MagicMock()
            ):
                # Simulate timeout in asyncio.wait_for
                with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
                    with pytest.raises(HTTPException) as excinfo:
                        await auto_scrape(request)
                    assert excinfo.value.status_code == 504
                    assert "Metadata sync timed out" in excinfo.value.detail

    # Verify lock is released even on timeout
    assert not (await ItemLock.get_lock(mock_show.id)).locked()
