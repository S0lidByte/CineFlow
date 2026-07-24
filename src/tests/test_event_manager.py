"""EventManager concurrency helpers (Phase 4 scaffolding)."""

from __future__ import annotations

from unittest.mock import MagicMock

from program.managers.event_manager import (
    EventManager,
    compute_transient_retry_delay,
)
from program.types import Event


def test_compute_transient_retry_delay_exponential():
    assert compute_transient_retry_delay(1) == 60
    assert compute_transient_retry_delay(2) == 120
    assert compute_transient_retry_delay(3) == 240
    assert compute_transient_retry_delay(4) == 480


def test_compute_transient_retry_delay_honours_rate_limit():
    assert compute_transient_retry_delay(2, rate_limit_retry_after=45) == 45.0


def test_item_exists_in_queue_matches_tmdb():
    em = EventManager.__new__(EventManager)
    queued_item = MagicMock(id=None, tmdb_id=42, tvdb_id=None, imdb_id=None)
    queue = [Event(emitted_by="Overseerr", content_item=queued_item)]

    candidate = MagicMock(id=None, tmdb_id=42, tvdb_id=None, imdb_id=None)
    assert em.item_exists_in_queue(candidate, queue) is True

    other = MagicMock(id=None, tmdb_id=99, tvdb_id=None, imdb_id=None)
    assert em.item_exists_in_queue(other, queue) is False


def test_item_exists_in_queue_matches_item_id():
    em = EventManager.__new__(EventManager)
    queue = [Event(emitted_by="Manual", item_id=7)]
    item = MagicMock(id=7, tmdb_id=None, tvdb_id=None, imdb_id=None)
    assert em.item_exists_in_queue(item, queue) is True
