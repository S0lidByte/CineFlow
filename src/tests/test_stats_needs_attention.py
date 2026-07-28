"""Stats / needs_attention characterization tests."""

from __future__ import annotations

from program.media.state import States
from routers.secure.default import NeedsAttentionItem, StatsResponse


def test_needs_attention_item_roundtrip():
    item = NeedsAttentionItem(
        id=42,
        title="One Piece",
        state="Failed",
        scraped_times=7,
    )
    assert item.model_dump() == {
        "id": 42,
        "title": "One Piece",
        "state": "Failed",
        "scraped_times": 7,
    }


def test_stats_response_includes_needs_attention_default():
    payload = StatsResponse(
        total_items=1,
        total_movies=1,
        total_shows=0,
        total_seasons=0,
        total_episodes=0,
        total_symlinks=0,
        incomplete_items=0,
        states={States.Completed: 1},
        activity={},
        media_year_releases=[],
    )
    assert payload.needs_attention == []
