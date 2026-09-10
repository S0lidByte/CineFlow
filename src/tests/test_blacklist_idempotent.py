"""Idempotent blacklist_stream against existing StreamBlacklistRelation rows."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from program.media.item import MediaItem


class _FakeStream:
    def __init__(self, infohash: str, stream_id: int | None):
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


def test_reset_scrape_state_for_retry_preserves_blacklisted_streams():
    """Manual/automated retries must preserve blacklisted_streams relation."""
    from program.media.state import States
    from routers.secure.items import _reset_scrape_state_for_retry

    blacklisted_stream = _FakeStream("c" * 40, 500)
    item = SimpleNamespace(
        id=10,
        streams=[_FakeStream("d" * 40, 501)],
        blacklisted_streams=[blacklisted_stream],
        active_stream=_FakeStream("d" * 40, 501),
        scraped_at=12345,
        scraped_times=2,
        failed_attempts=1,
        updated=True,
        last_state=States.Indexed,
        store_state=MagicMock(),
    )

    _reset_scrape_state_for_retry(item)

    assert len(item.blacklisted_streams) == 1
    assert item.blacklisted_streams[0] is blacklisted_stream
    assert item.streams == []
    assert item.active_stream is None
    assert item.scraped_at is None
    assert item.scraped_times == 1
    assert item.failed_attempts == 0
    item.store_state.assert_called_once_with(States.Indexed)


def test_is_active_stream_primary_and_fallback_matching():
    """_is_active_stream matches by stream ID primarily and infohash secondarily."""
    from routers.secure.items import _is_active_stream

    active = _FakeStream("hash1" + "0" * 35, 100)
    other = _FakeStream("hash2" + "0" * 35, 101)
    same_hash_diff_id = _FakeStream("hash1" + "0" * 35, 102)
    same_hash_no_id = _FakeStream("hash1" + "0" * 35, None)
    active_no_id = _FakeStream("hash1" + "0" * 35, None)

    item = SimpleNamespace(active_stream=active)

    assert _is_active_stream(item, active) is True
    assert _is_active_stream(item, other) is False
    # Primary stream ID authority: different non-None IDs -> not active stream
    assert _is_active_stream(item, same_hash_diff_id) is False

    # Infohash fallback when stream ID is None
    item_no_active_id = SimpleNamespace(active_stream=active_no_id)
    assert _is_active_stream(item_no_active_id, same_hash_no_id) is True

    item_no_active = SimpleNamespace(active_stream=None)
    assert _is_active_stream(item_no_active, active) is False


def test_scraper_rejects_blacklisted_stream_and_picks_clean_stream():
    """Scraper candidate filtering must reject blacklisted candidate and allow clean candidate."""
    from program.services.scrapers import Scraping

    blacklisted_stream = _FakeStream("hash_blacklisted" + "0" * 24, 1)
    clean_stream = _FakeStream("hash_clean" + "0" * 30, 2)

    item = SimpleNamespace(
        id=1,
        log_string="Test Item",
        streams=[],
        blacklisted_streams=[blacklisted_stream],
        updated=True,
        failed_attempts=0,
        scraped_times=0,
        store_state=MagicMock(),
        set=MagicMock(),
    )

    scrapers = object.__new__(Scraping)
    scrapers.scrape = MagicMock(
        return_value={
            blacklisted_stream.infohash: blacklisted_stream,
            clean_stream.infohash: clean_stream,
        }
    )

    with patch("program.services.scrapers.logger"):
        list(Scraping.run(scrapers, item))

    assert len(item.streams) == 1
    assert item.streams[0] is clean_stream
    assert blacklisted_stream not in item.streams


def test_scraper_marks_item_failed_at_default_retry_limit():
    """The default policy must terminate repeated zero-new-stream scrapes."""
    from program.media.state import States
    from program.services.scrapers import DEFAULT_MAX_FAILED_ATTEMPTS, Scraping

    item = SimpleNamespace(
        id=1,
        log_string="Exhausted Item",
        streams=[],
        blacklisted_streams=[],
        updated=True,
        failed_attempts=DEFAULT_MAX_FAILED_ATTEMPTS - 1,
        scraped_times=0,
        store_state=MagicMock(),
        set=MagicMock(),
    )

    scrapers = object.__new__(Scraping)
    scrapers.max_failed_attempts = DEFAULT_MAX_FAILED_ATTEMPTS
    scrapers.scrape = MagicMock(return_value={})

    with patch("program.services.scrapers.logger"):
        list(Scraping.run(scrapers, item))

    assert item.failed_attempts == DEFAULT_MAX_FAILED_ATTEMPTS
    item.store_state.assert_called_once_with(States.Failed)


def test_should_submit_uses_bounded_default_for_legacy_zero_limit():
    """Legacy persisted zero values must not permit retries forever."""
    from datetime import datetime, timedelta

    from program.services.scrapers import DEFAULT_MAX_FAILED_ATTEMPTS, Scraping
    from program.settings import settings_manager

    scrapers = object.__new__(Scraping)
    item = SimpleNamespace(
        scraped_at=datetime.now() - timedelta(days=2),
        scraped_times=0,
        failed_attempts=DEFAULT_MAX_FAILED_ATTEMPTS,
    )

    with settings_manager.override(scraping={"max_failed_attempts": 0}):
        assert not Scraping.should_submit(scrapers, item)
