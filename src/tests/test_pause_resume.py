"""Regression coverage for pause/resume pipeline-state preservation."""

from unittest.mock import MagicMock, patch

from program.media.item import MediaItem, Season
from program.media.state import States
from routers.secure.items import restore_state_after_pause, save_state_before_pause


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
