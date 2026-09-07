"""Regression coverage for manual reindex transaction ordering."""

from unittest.mock import MagicMock, patch

import pytest

from program.media.item import Movie


@pytest.mark.asyncio
@patch("routers.secure.items.db_session")
@patch("routers.secure.items.apply_item_mutation")
async def test_reindex_commits_before_publishing_retry_event(
    mock_apply_item_mutation, mock_db_session
) -> None:
    """RetryItem must only be visible to consumers after the mutation commits."""

    from program.program import Program
    from routers.secure.items import ReindexPayload, reindex_item

    item = MagicMock(spec=Movie)
    item.id = 42
    item.log_string = "Movie 42"
    session = MagicMock()
    session.get.return_value = item
    mock_db_session.return_value.__enter__.return_value = session

    call_order: list[str] = []
    reindexed_item = MagicMock()
    runner_result = MagicMock(media_items=[reindexed_item])
    indexer = MagicMock()
    indexer.run.return_value = iter([runner_result])
    program = MagicMock()
    program.services.indexer = indexer
    program.em.add_event.side_effect = lambda _event: call_order.append("event")
    session.commit.side_effect = lambda: call_order.append("commit")

    def apply_mutation(*, program, session, item, mutation_fn, bubble_parents):
        mutation_fn(item, session)
        call_order.append("mutation")

    mock_apply_item_mutation.side_effect = apply_mutation

    with patch("routers.secure.items.di", {Program: program}):
        response = await reindex_item(ReindexPayload(item_id=item.id))

    assert response.message == "Successfully re-indexed Movie 42"
    assert call_order == ["mutation", "commit", "event"]
    session.merge.assert_called_once_with(reindexed_item)
    event = program.em.add_event.call_args.args[0]
    assert event.emitted_by == "RetryItem"
    assert event.item_id == item.id
