"""Idempotent blacklist_stream against existing StreamBlacklistRelation rows."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from program.media.item import MediaItem


class _FakeStream:
    def __init__(self, infohash: str, stream_id: int):
        self.infohash = infohash
        self.id = stream_id

    def __hash__(self) -> int:
        return hash(self.infohash)

    def __eq__(self, other: object) -> bool:
        return getattr(other, "infohash", None) == self.infohash


def test_blacklist_stream_skips_append_when_relation_already_in_db():
    """Stale collection + existing DB row must not attempt a duplicate INSERT."""

    stream = _FakeStream("a" * 40, 2283376)
    item = SimpleNamespace(
        id=9415,
        log_string="Item 9415",
        streams=[stream],
        blacklisted_streams=[],
    )

    session = MagicMock()
    session.query.return_value.filter_by.return_value.first.return_value = (1,)

    with patch("program.media.item.object_session", return_value=session):
        assert MediaItem.blacklist_stream(item, stream) is True  # type: ignore[arg-type]

    assert stream not in item.streams
    session.expire.assert_called_once_with(item, ["blacklisted_streams"])
    assert item.blacklisted_streams == []


def test_blacklist_stream_appends_when_not_already_related():
    stream = _FakeStream("b" * 40, 99)
    item = SimpleNamespace(
        id=1,
        log_string="Item 1",
        streams=[stream],
        blacklisted_streams=[],
    )

    session = MagicMock()
    session.query.return_value.filter_by.return_value.first.return_value = None

    with patch("program.media.item.object_session", return_value=session):
        assert MediaItem.blacklist_stream(item, stream) is True  # type: ignore[arg-type]

    assert stream not in item.streams
    assert stream in item.blacklisted_streams
