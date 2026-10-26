"""Automatic dead-link removal must preserve blacklist and queue re-scrape."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from program.media.item import Episode
from program.media.state import States
from program.types import Event


class _FakeStream:
    def __init__(self, infohash: str, stream_id: int = 1):
        self.infohash = infohash
        self.id = stream_id
        self.raw_title = f"{infohash[:8]}.mkv"

    def __hash__(self) -> int:
        return hash(self.infohash)

    def __eq__(self, other: object) -> bool:
        return getattr(other, "infohash", None) == self.infohash


def _episode_with_streams() -> SimpleNamespace:
    dead = _FakeStream("dead" * 10, 1)
    other = _FakeStream("live" * 10, 2)
    episode = SimpleNamespace(
        id=42,
        title="Test Show S01E01",
        number=1,
        log_string="Item 42 Test Show S01E01",
        streams=[dead, other],
        blacklisted_streams=[],
        active_stream=dead,
        filesystem_entries=[],
        subtitles=[],
        scraped_at=None,
        scraped_times=3,
        failed_attempts=2,
        updated=False,
        last_state=States.Completed,
        media_entry=None,
        parent=None,
        store_state=MagicMock(),
    )
    episode.blacklist_active_stream = Episode.blacklist_active_stream.__get__(
        episode, SimpleNamespace
    )
    episode.blacklist_stream = Episode.blacklist_stream.__get__(
        episode, SimpleNamespace
    )
    episode.prepare_for_automatic_rescrape = (
        Episode.prepare_for_automatic_rescrape.__get__(episode, SimpleNamespace)
    )
    episode._reset = Episode._reset.__get__(episode, SimpleNamespace)
    return episode


def _mock_riven_vfs():
    mock_riven = MagicMock()
    mock_riven.services.filesystem.riven_vfs = MagicMock()
    return mock_riven


def _mock_session_no_existing_blacklist():
    session = MagicMock()
    session.query.return_value.filter_by.return_value.first.return_value = None
    return session


@patch("program.program.riven")
@patch("program.media.item.object_session")
def test_prepare_for_automatic_rescrape_preserves_blacklist(
    mock_object_session,
    mock_riven_module,
):
    episode = _episode_with_streams()
    mock_riven_module.services = _mock_riven_vfs().services
    mock_object_session.return_value = _mock_session_no_existing_blacklist()

    episode.prepare_for_automatic_rescrape()

    assert len(episode.blacklisted_streams) == 1
    assert episode.blacklisted_streams[0].infohash == "dead" * 10
    assert episode.streams == []
    assert episode.active_stream is None
    assert episode.scraped_at is None
    assert episode.scraped_times == 0
    assert episode.failed_attempts == 0
    mock_riven_module.services.filesystem.riven_vfs.remove.assert_called_once_with(
        episode
    )


@patch("program.program.riven")
@patch("program.media.item.object_session")
def test_reset_clears_blacklist_but_prepare_does_not(
    mock_object_session,
    mock_riven_module,
):
    """Regression: full reset() must not be used for automatic dead-link recovery."""
    episode = _episode_with_streams()
    mock_riven_module.services = _mock_riven_vfs().services
    mock_object_session.return_value = _mock_session_no_existing_blacklist()

    episode.blacklist_active_stream()
    episode._reset()

    assert episode.blacklisted_streams == []


@patch("program.services.filesystem.vfs.db.di")
@patch("program.services.filesystem.vfs.db.apply_item_mutation")
def test_schedule_dead_link_rescrape_queues_scrape_with_overrides(
    mock_apply,
    mock_di,
):
    from program.services.filesystem.vfs.db import VFSDatabase

    episode = _episode_with_streams()
    episode.prepare_for_automatic_rescrape = MagicMock()
    entry = MagicMock()
    entry.media_item = episode
    session = MagicMock()
    program = MagicMock()
    mock_di.__getitem__.return_value = program

    def _apply_side_effect(*, program, item, mutation_fn, session):
        mutation_fn(item, session)
        # Mirror cascade clear of filesystem_entries → media_item becomes None
        entry.media_item = None

    mock_apply.side_effect = _apply_side_effect

    vfs_db = VFSDatabase(downloader=MagicMock())

    assert vfs_db.schedule_dead_link_rescrape(entry, session) is True

    mock_apply.assert_called_once()
    session.commit.assert_called_once()
    program.em.add_event.assert_called_once()
    event = program.em.add_event.call_args.args[0]
    assert isinstance(event, Event)
    assert event.emitted_by == "VFS"
    assert event.item_id == 42
    assert event.overrides == {"automatic_rescrape": True}
    assert entry.media_item is None


def test_state_transition_indexed_with_overrides_queues_scrape():
    from program.state_transition import process_event

    episode = _episode_with_streams()
    episode.last_state = States.Indexed
    episode.scraped_at = None

    scraping = MagicMock()
    scraping.should_submit.return_value = False

    program = MagicMock()
    program.services.scraping = scraping
    with patch("program.state_transition.di") as mock_di:
        mock_di.__getitem__.return_value = program
        processed = process_event(
            "VFS",
            existing_item=episode,
            overrides={"automatic_rescrape": True},
        )

    assert processed.service is scraping
    assert list(processed.related_media_items) == [episode]


@pytest.mark.asyncio
@patch("sqlalchemy.orm.object_session")
@patch("program.media.item.object_session")
@patch("routers.secure.items.db_session")
@patch("routers.secure.items.apply_item_mutation")
async def test_active_stream_blacklisting_lifecycle_ordering(
    mock_apply_mutation,
    mock_db_session,
    mock_object_session,
    mock_orm_object_session,
):
    from routers.secure.items import blacklist_stream

    episode = _episode_with_streams()
    active_stream = episode.active_stream
    entry = MagicMock()
    entry.path = "/media/tv/Test Show/S01E01.mkv"
    episode.media_entry = entry
    episode.filesystem_entries = [entry]
    episode.subtitles = [MagicMock()]

    session = MagicMock()
    session.execute.return_value.unique.return_value.scalar_one_or_none.return_value = (
        episode
    )
    session.query.return_value.filter_by.return_value.first.return_value = None
    mock_db_session.return_value.__enter__.return_value = session
    mock_object_session.return_value = session
    mock_orm_object_session.return_value = session

    call_order = []

    def _apply_mutation_side_effect(
        program, sess, item, mutation_fn, bubble_parents=True
    ):
        mutation_fn(item, sess)
        call_order.append("mutation_applied")

    mock_apply_mutation.side_effect = _apply_mutation_side_effect

    def _commit_side_effect():
        call_order.append("db_commit")

    session.commit.side_effect = _commit_side_effect

    updater = MagicMock()
    updater.refresh_path = MagicMock(
        side_effect=lambda p: call_order.append("updater_refresh")
    )
    updater.empty_trash = MagicMock(
        side_effect=lambda p: call_order.append("updater_empty_trash")
    )

    program = MagicMock()
    program.services.updater = updater
    vfs_mock = MagicMock()
    vfs_mock.remove.side_effect = lambda item: call_order.append("vfs_remove")
    program.services.filesystem.riven_vfs = vfs_mock
    program.riven_vfs = vfs_mock

    from program.program import Program

    di = {Program: program}

    with patch("routers.secure.items.di", di):
        response = await blacklist_stream(item_id=42, stream_id=active_stream.id)

    assert "Blacklisted active stream" in response.message
    assert episode.active_stream is None
    assert episode.filesystem_entries == []
    assert episode.subtitles == []

    # Verify strict lifecycle order:
    # 1. mutation_applied -> 2. initial db_commit -> 3. vfs_remove -> 4. final db_commit -> 5. updater calls -> 6. emit event
    assert call_order.index("mutation_applied") < call_order.index("db_commit")
    assert call_order.index("db_commit") < call_order.index("vfs_remove")
    assert call_order.index("vfs_remove") < call_order.index("updater_refresh")
    assert call_order.index("updater_refresh") < call_order.index("updater_empty_trash")

    program.em.add_event.assert_called_once()


@patch("program.services.filesystem.vfs.db.di")
@patch("program.services.filesystem.vfs.db.apply_item_mutation")
def test_schedule_dead_link_rescrape_soft_does_not_blacklist(mock_apply, mock_di):
    """Soft/transient CDN validate failure must re-scrape WITHOUT blacklisting active stream."""
    from program.services.filesystem.vfs.db import VFSDatabase

    episode = _episode_with_streams()
    episode.prepare_for_automatic_rescrape = MagicMock()
    entry = MagicMock()
    entry.media_item = episode
    session = MagicMock()
    program = MagicMock()
    mock_di.__getitem__.return_value = program

    def _apply_side_effect(*, program, item, mutation_fn, session):
        mutation_fn(item, session)
        entry.media_item = None

    mock_apply.side_effect = _apply_side_effect

    vfs_db = VFSDatabase(downloader=MagicMock())

    assert vfs_db.schedule_dead_link_rescrape(entry, session, permanent=False) is True
    episode.prepare_for_automatic_rescrape.assert_called_once_with(blacklist=False)


@patch("program.services.filesystem.vfs.db.di")
@patch("program.services.filesystem.vfs.db.apply_item_mutation")
def test_schedule_dead_link_rescrape_permanent_does_blacklist(mock_apply, mock_di):
    """Permanent CDN dead-link must re-scrape WITH blacklisting."""
    from program.services.filesystem.vfs.db import VFSDatabase

    episode = _episode_with_streams()
    episode.prepare_for_automatic_rescrape = MagicMock()
    entry = MagicMock()
    entry.media_item = episode
    session = MagicMock()
    program = MagicMock()
    mock_di.__getitem__.return_value = program

    def _apply_side_effect(*, program, item, mutation_fn, session):
        mutation_fn(item, session)
        entry.media_item = None

    mock_apply.side_effect = _apply_side_effect

    vfs_db = VFSDatabase(downloader=MagicMock())

    assert vfs_db.schedule_dead_link_rescrape(entry, session, permanent=True) is True
    episode.prepare_for_automatic_rescrape.assert_called_once_with(blacklist=True)

    event = program.em.add_event.call_args.args[0]
    assert event.emitted_by == "VFS"
    assert event.item_id == 42


@pytest.mark.asyncio
@patch("sqlalchemy.orm.object_session")
@patch("program.media.item.object_session")
@patch("routers.secure.items.db_session")
@patch("routers.secure.items.apply_item_mutation")
async def test_inactive_stream_blacklisting_leaves_vfs_untouched(
    mock_apply_mutation,
    mock_db_session,
    mock_object_session,
    mock_orm_object_session,
):
    from routers.secure.items import blacklist_stream

    episode = _episode_with_streams()
    inactive_stream = episode.streams[1]  # episode.active_stream is streams[0]
    entry = MagicMock()
    episode.filesystem_entries = [entry]

    session = MagicMock()
    session.execute.return_value.unique.return_value.scalar_one_or_none.return_value = (
        episode
    )
    session.query.return_value.filter_by.return_value.first.return_value = None
    mock_db_session.return_value.__enter__.return_value = session
    mock_object_session.return_value = session
    mock_orm_object_session.return_value = session

    program = MagicMock()
    vfs_mock = MagicMock()
    program.services.filesystem.riven_vfs = vfs_mock

    from program.program import Program

    di = {Program: program}

    with patch("routers.secure.items.di", di):
        response = await blacklist_stream(item_id=42, stream_id=inactive_stream.id)

    assert "Blacklisted stream" in response.message
    assert episode.active_stream is episode.streams[0]
    vfs_mock.remove.assert_not_called()
    program.em.add_event.assert_not_called()


@pytest.mark.asyncio
@patch("sqlalchemy.orm.object_session")
@patch("program.media.item.object_session")
@patch("routers.secure.items.db_session")
@patch("routers.secure.items.apply_item_mutation")
async def test_active_stream_blacklisting_external_failures_isolated(
    mock_apply_mutation,
    mock_db_session,
    mock_object_session,
    mock_orm_object_session,
):
    from routers.secure.items import blacklist_stream

    episode = _episode_with_streams()
    active_stream = episode.active_stream
    entry = MagicMock()
    entry.path = "/media/tv/Test Show/S01E01.mkv"
    episode.media_entry = entry

    session = MagicMock()
    session.execute.return_value.unique.return_value.scalar_one_or_none.return_value = (
        episode
    )
    session.query.return_value.filter_by.return_value.first.return_value = None
    mock_db_session.return_value.__enter__.return_value = session
    mock_object_session.return_value = session
    mock_orm_object_session.return_value = session

    def _apply_mutation_side_effect(
        program, sess, item, mutation_fn, bubble_parents=True
    ):
        mutation_fn(item, sess)

    mock_apply_mutation.side_effect = _apply_mutation_side_effect

    updater = MagicMock()
    updater.refresh_path.side_effect = RuntimeError("Plex connection failed")
    updater.empty_trash.side_effect = RuntimeError("Plex trash failed")

    program = MagicMock()
    program.services.updater = updater
    vfs_mock = MagicMock()
    vfs_mock.remove.side_effect = RuntimeError("VFS unmount error")
    program.services.filesystem.riven_vfs = vfs_mock

    from program.program import Program

    di = {Program: program}

    with patch("routers.secure.items.di", di):
        response = await blacklist_stream(item_id=42, stream_id=active_stream.id)

    # Blacklist mutation, active_stream clearing, and RetryItem event must still complete successfully
    assert "Blacklisted active stream" in response.message
    assert episode.active_stream is None
    program.em.add_event.assert_called_once()
    event = program.em.add_event.call_args.args[0]
    assert event.emitted_by == "RetryItem"
    assert event.item_id == 42
