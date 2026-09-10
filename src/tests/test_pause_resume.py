"""Regression coverage for pause/resume pipeline-state preservation and cascading."""

from unittest.mock import MagicMock, patch

from program.media.item import Episode, MediaItem, Season, Show
from program.media.state import States
from routers.secure.items import (
    _ADD_SKIP_REQUEUE_STATES,
    _RETRY_SKIP_RESET_STATES,
    _pause_item_and_descendants,
    _reset_scrape_state_for_retry,
    _unpause_item_and_descendants,
    restore_state_after_pause,
    save_state_before_pause,
)


def _item_with_state(item_type: type[MediaItem], state: States) -> MagicMock:
    item = MagicMock(spec=item_type)
    item.last_state = state
    item.state = state
    return item


@patch.object(MediaItem, "store_state")
def test_pause_preserves_effective_pipeline_state(mock_store_state: MagicMock) -> None:
    item = _item_with_state(MediaItem, States.Scraped)

    save_state_before_pause(item)

    assert item.state_before_pause == States.Scraped
    mock_store_state.assert_called_once_with(item, States.Paused)


@patch.object(MediaItem, "store_state")
def test_resume_restores_saved_pipeline_state(mock_store_state: MagicMock) -> None:
    item = _item_with_state(MediaItem, States.Paused)
    item.state_before_pause = States.Downloaded

    restore_state_after_pause(item)

    assert item.state_before_pause is None
    mock_store_state.assert_called_once_with(item, States.Downloaded)


@patch.object(MediaItem, "store_state")
def test_resume_legacy_paused_item_falls_back_to_requested(
    mock_store_state: MagicMock,
) -> None:
    item = _item_with_state(MediaItem, States.Paused)
    item.state_before_pause = None

    restore_state_after_pause(item)

    assert item.state_before_pause is None
    mock_store_state.assert_called_once_with(item, States.Requested)


@patch.object(MediaItem, "store_state")
def test_pause_resume_season_uses_base_state_storage(
    mock_store_state: MagicMock,
) -> None:
    season = _item_with_state(Season, States.Indexed)

    save_state_before_pause(season)
    restore_state_after_pause(season)

    assert mock_store_state.call_args_list == [
        ((season, States.Paused),),
        ((season, States.Indexed),),
    ]


def test_states_collections_do_not_block_paused_requeue() -> None:
    """Ensure paused items are not skipped during re-request or retry."""
    assert States.Paused not in _ADD_SKIP_REQUEUE_STATES
    assert States.Paused not in _RETRY_SKIP_RESET_STATES


@patch.object(MediaItem, "store_state")
def test_pause_cascades_to_seasons_and_episodes(mock_store_state: MagicMock) -> None:
    """Pausing a show must recursively pause active seasons and episodes."""
    show = _item_with_state(Show, States.Scraped)
    show.id = 1
    show.state_before_pause = None

    season1 = _item_with_state(Season, States.Indexed)
    season1.id = 2
    season1.state_before_pause = None

    season2 = _item_with_state(Season, States.Completed)
    season2.id = 3
    season2.state_before_pause = None

    ep1 = _item_with_state(Episode, States.Scraped)
    ep1.id = 4
    ep1.state_before_pause = None

    ep2 = _item_with_state(Episode, States.Completed)
    ep2.id = 5
    ep2.state_before_pause = None

    season1.seasons = []
    season1.episodes = [ep1]
    season2.seasons = []
    season2.episodes = [ep2]
    show.seasons = [season1, season2]

    _pause_item_and_descendants(show)

    assert show.state_before_pause == States.Scraped
    assert season1.state_before_pause == States.Indexed
    assert ep1.state_before_pause == States.Scraped
    # Completed items should not have been paused
    assert season2.state_before_pause is None
    assert ep2.state_before_pause is None


@patch.object(MediaItem, "store_state")
def test_unpause_cascades_from_show_to_descendants(mock_store_state: MagicMock) -> None:
    """Unpausing a show must restore all paused seasons and episodes."""
    show = _item_with_state(Show, States.Paused)
    show.id = 10
    show.state_before_pause = States.Requested

    season1 = _item_with_state(Season, States.Paused)
    season1.id = 20
    season1.state_before_pause = States.Indexed
    season1.seasons = []

    season2 = _item_with_state(Season, States.Completed)
    season2.id = 30
    season2.seasons = []

    ep1 = _item_with_state(Episode, States.Paused)
    ep1.id = 40
    ep1.state_before_pause = States.Scraped

    ep2 = _item_with_state(Episode, States.Completed)
    ep2.id = 50

    season1.episodes = [ep1]
    season2.episodes = [ep2]
    show.seasons = [season1, season2]

    unpaused_ids: set[int] = set()
    _unpause_item_and_descendants(show, unpaused_ids)

    assert unpaused_ids == {10, 20, 40}
    assert show.state_before_pause is None
    assert season1.state_before_pause is None
    assert ep1.state_before_pause is None


@patch.object(MediaItem, "store_state")
def test_unpause_season_unpauses_children_and_unblocks_parent_show(
    mock_store_state: MagicMock,
) -> None:
    """Unpausing a season must unpause its episodes and unblock the parent show if paused."""
    show = _item_with_state(Show, States.Paused)
    show.id = 100
    show.state_before_pause = States.Requested

    season = _item_with_state(Season, States.Paused)
    season.id = 200
    season.state_before_pause = States.Indexed
    season.parent = show
    season.seasons = []

    ep1 = _item_with_state(Episode, States.Paused)
    ep1.id = 300
    ep1.state_before_pause = States.Scraped
    ep1.parent = season

    season.episodes = [ep1]
    show.seasons = [season]

    unpaused_ids: set[int] = set()
    _unpause_item_and_descendants(season, unpaused_ids)

    assert unpaused_ids == {100, 200, 300}
    assert show.state_before_pause is None
    assert season.state_before_pause is None
    assert ep1.state_before_pause is None


@patch.object(MediaItem, "store_state")
def test_unpause_episode_unblocks_parent_season_and_grandparent_show(
    mock_store_state: MagicMock,
) -> None:
    """Unpausing an episode must unblock paused parent season and grandparent show."""
    show = _item_with_state(Show, States.Paused)
    show.id = 1000
    show.state_before_pause = States.Requested

    season = _item_with_state(Season, States.Paused)
    season.id = 2000
    season.state_before_pause = States.Indexed
    season.parent = show

    ep = _item_with_state(Episode, States.Paused)
    ep.id = 3000
    ep.state_before_pause = States.Scraped
    ep.parent = season

    season.episodes = [ep]
    show.seasons = [season]

    unpaused_ids: set[int] = set()
    _unpause_item_and_descendants(ep, unpaused_ids)

    assert unpaused_ids == {1000, 2000, 3000}
    assert ep.state_before_pause is None
    assert season.state_before_pause is None
    assert show.state_before_pause is None


@patch.object(MediaItem, "store_state")
def test_reset_scrape_state_for_retry_handles_paused_items(
    mock_store_state: MagicMock,
) -> None:
    """Retrying or re-requesting a paused item resets its scrape blockers and sets Indexed."""
    show = _item_with_state(Show, States.Paused)
    show.id = 10
    show.state_before_pause = States.Scraped
    show.streams = [MagicMock()]
    show.active_stream = MagicMock()

    season = _item_with_state(Season, States.Paused)
    season.id = 20
    season.state_before_pause = States.Scraped
    season.streams = [MagicMock()]
    season.active_stream = MagicMock()
    season.seasons = []

    ep = _item_with_state(Episode, States.Paused)
    ep.id = 30
    ep.state_before_pause = States.Scraped
    ep.streams = [MagicMock()]
    ep.active_stream = MagicMock()

    season.episodes = [ep]
    show.seasons = [season]

    _reset_scrape_state_for_retry(show)

    assert show.state_before_pause is None
    assert show.streams == []
    assert show.active_stream is None

    assert season.state_before_pause is None
    assert season.streams == []
    assert season.active_stream is None

    assert ep.state_before_pause is None
    assert ep.streams == []
    assert ep.active_stream is None
