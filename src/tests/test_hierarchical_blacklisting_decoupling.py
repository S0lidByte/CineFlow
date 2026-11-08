"""Tests ensuring episode-level stream rejection does not taint season or show streams."""

from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

from program.media.item import MediaItem


class FakeStream:
    def __init__(self, stream_id: int, infohash: str):
        self.id = stream_id
        self.infohash = infohash
        self.raw_title = f"Show.S01.Complete.1080p-{infohash[:6]}.mkv"

    def __eq__(self, other: object) -> bool:
        return getattr(other, "infohash", None) == self.infohash

    def __hash__(self) -> int:
        return hash(self.infohash)


class MockItem(SimpleNamespace):
    def blacklist_stream(self, stream):
        return MediaItem.blacklist_stream(self, stream)

    def blacklist_active_stream(self):
        return MediaItem.blacklist_active_stream(self)

    def unblacklist_stream(self, stream):
        return MediaItem.unblacklist_stream(self, stream)

    def prepare_for_automatic_rescrape(self, blacklist=True):
        return MediaItem.prepare_for_automatic_rescrape(self, blacklist=blacklist)


def _build_show_hierarchy() -> (
    tuple[MockItem, MockItem, MockItem, MockItem, FakeStream]
):
    stream = FakeStream(101, "seasonpack1234567890abcdef1234567890abcdef")

    show = MockItem(
        id=1,
        type="show",
        log_string="Show 1",
        streams=[],
        blacklisted_streams=[],
        parent=None,
    )

    season = MockItem(
        id=10,
        type="season",
        log_string="Season 1",
        streams=[stream],
        blacklisted_streams=[],
        parent=show,
    )

    def _get_stream_owner(s):
        if s in season.streams:
            return season
        if s in show.streams:
            return show
        return None

    show.get_stream_owner = _get_stream_owner
    season.get_stream_owner = _get_stream_owner

    ep1 = MockItem(
        id=101,
        type="episode",
        log_string="Episode 1",
        streams=[],
        blacklisted_streams=[],
        active_stream=None,
        parent=season,
        media_entry=None,
        filesystem_entries=[MagicMock()],
        subtitles=[MagicMock()],
        scraped_at=object(),
        scraped_times=2,
        failed_attempts=1,
        store_state=MagicMock(),
        get_stream_owner=_get_stream_owner,
    )

    ep2 = MockItem(
        id=102,
        type="episode",
        log_string="Episode 2",
        streams=[],
        blacklisted_streams=[],
        active_stream=None,
        parent=season,
        media_entry=None,
        filesystem_entries=[],
        subtitles=[],
        scraped_at=None,
        scraped_times=0,
        failed_attempts=0,
        store_state=MagicMock(),
        get_stream_owner=_get_stream_owner,
    )

    return show, season, ep1, ep2, stream


def test_episode_blacklist_stream_preserves_season_pack_for_siblings():
    """When an episode blacklists a stream, the season pack must remain on Season for sibling episodes."""
    show, season, ep1, ep2, stream = _build_show_hierarchy()

    with patch("program.media.item.object_session", return_value=None):
        result = ep1.blacklist_stream(stream)

    assert result is True
    # Episode 1 has the stream in blacklisted_streams
    assert stream in ep1.blacklisted_streams

    # CRITICAL: Season still has the stream in its active streams!
    assert stream in season.streams
    # Season's blacklisted_streams must NOT contain the stream
    assert stream not in season.blacklisted_streams

    # CRITICAL: Show must remain completely untouched
    assert stream not in show.blacklisted_streams

    # CRITICAL: Episode 2 (sibling) is completely untainted
    assert stream not in ep2.blacklisted_streams


def test_episode_blacklist_active_stream_preserves_season():
    """Tearing down an active stream on an episode must not clear the season pack from Season."""
    show, season, ep1, ep2, stream = _build_show_hierarchy()

    ep1.active_stream = stream

    with patch("program.media.item.object_session", return_value=None):
        result = ep1.blacklist_active_stream()

    assert result is True
    assert stream in ep1.blacklisted_streams

    # Season must still keep the stream!
    assert stream in season.streams
    assert stream not in season.blacklisted_streams
    assert stream not in ep2.blacklisted_streams


def test_prepare_for_automatic_rescrape_soft_does_not_blacklist():
    """prepare_for_automatic_rescrape(blacklist=False) must not blacklist active stream."""
    show, season, ep1, _ep2, stream = _build_show_hierarchy()

    ep1.active_stream = stream
    ep1.blacklist_active_stream = Mock()

    ep1.prepare_for_automatic_rescrape(blacklist=False)

    ep1.blacklist_active_stream.assert_not_called()
    assert ep1.active_stream is None
    assert stream not in ep1.blacklisted_streams
    assert stream in season.streams


def test_prepare_for_automatic_rescrape_permanent_blacklists_episode_only():
    """prepare_for_automatic_rescrape(blacklist=True) blacklists on episode, preserving season."""
    show, season, ep1, ep2, stream = _build_show_hierarchy()

    ep1.active_stream = stream

    with patch("program.media.item.object_session", return_value=None):
        ep1.prepare_for_automatic_rescrape(blacklist=True)

    assert stream in ep1.blacklisted_streams
    assert ep1.active_stream is None
    assert stream in season.streams
    assert stream not in season.blacklisted_streams
    assert stream not in ep2.blacklisted_streams


def test_unblacklist_stream_on_episode_only_restores_episode():
    """Unblacklisting a stream on an episode restores it for that episode without affecting others."""
    show, season, ep1, ep2, stream = _build_show_hierarchy()

    with patch("program.media.item.object_session", return_value=None):
        ep1.blacklist_stream(stream)

    assert stream in ep1.blacklisted_streams

    with patch("program.media.item.object_session", return_value=None):
        ep1.unblacklist_stream(stream)

    assert stream not in ep1.blacklisted_streams
    assert stream in ep1.streams
    assert stream in season.streams
    assert stream not in ep2.blacklisted_streams
