"""Regression coverage for Unreleased state routing."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from program.media.state import States
from program.state_transition import process_event


def _unreleased_item() -> SimpleNamespace:
    return SimpleNamespace(
        last_state=States.Unreleased,
        log_string="Item 42 Test Movie",
    )


def _program_with_indexer() -> tuple[MagicMock, MagicMock]:
    indexer = MagicMock(name="indexer")
    program = MagicMock()
    program.services.indexer = indexer
    return program, indexer


def test_unreleased_item_from_another_source_routes_to_indexer() -> None:
    program, indexer = _program_with_indexer()
    item = _unreleased_item()

    with patch("program.state_transition.di") as mock_di:
        mock_di.__getitem__.return_value = program
        processed = process_event("Scheduler", existing_item=item)

    assert processed.service is indexer
    assert list(processed.related_media_items or []) == [item]


def test_unreleased_item_from_indexer_stops_processing() -> None:
    program, indexer = _program_with_indexer()
    item = _unreleased_item()

    with patch("program.state_transition.di") as mock_di:
        mock_di.__getitem__.return_value = program
        processed = process_event(indexer, existing_item=item)

    assert processed.service is None
    assert list(processed.related_media_items or []) == []


def test_missing_item_does_not_route_to_indexer() -> None:
    program, _ = _program_with_indexer()

    with patch("program.state_transition.di") as mock_di:
        mock_di.__getitem__.return_value = program
        processed = process_event("Scheduler")

    assert processed.service is None
    assert list(processed.related_media_items or []) == []
