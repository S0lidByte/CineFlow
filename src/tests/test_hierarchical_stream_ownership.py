"""Parent-owned streams must retain parent ownership during episode playback cleanup."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from program.media.item import MediaItem
from program.media.state import States


class FakeStream:
    def __init__(self, stream_id: int, infohash: str):
        self.id = stream_id
        self.infohash = infohash
        self.raw_title = "shared-stream.mkv"

    def __eq__(self, other: object) -> bool:
        return getattr(other, "id", None) == self.id

    __hash__ = object.__hash__


@pytest.mark.asyncio
@patch("sqlalchemy.orm.object_session")
@patch("program.media.item.object_session")
@patch("routers.secure.items.db_session")
@patch("routers.secure.items.apply_item_mutation")
async def test_blacklisting_parent_owned_active_stream_resets_only_episode(
    mock_apply_mutation,
    mock_db_session,
    mock_media_object_session,
    mock_orm_object_session,
):
    """Episode teardown must not clear the Season's active playback state."""
    from program.program import Program
    from routers.secure.items import blacklist_stream

    stream = FakeStream(7, "a" * 40)
    season = SimpleNamespace(
        id=10,
        log_string="Season 1",
        streams=[stream],
        blacklisted_streams=[],
        active_stream=stream,
        parent=None,
    )
    episode = SimpleNamespace(
        id=42,
        log_string="Episode 1",
        streams=[],
        blacklisted_streams=[],
        active_stream=stream,
        parent=season,
        media_entry=None,
        filesystem_entries=[MagicMock()],
        subtitles=[MagicMock()],
        scraped_at=object(),
        scraped_times=2,
        failed_attempts=1,
        store_state=MagicMock(),
    )

    session = MagicMock()
    session.execute.return_value.unique.return_value.scalar_one_or_none.return_value = (
        episode
    )
    session.query.return_value.filter_by.return_value.first.return_value = None
    mock_db_session.return_value.__enter__.return_value = session
    mock_media_object_session.return_value = session
    mock_orm_object_session.return_value = session

    def apply_mutation(_program, db, consumer, mutation, bubble_parents=True):
        assert db is session
        assert consumer is episode
        assert bubble_parents is False
        mutation(consumer, db)

    mock_apply_mutation.side_effect = apply_mutation

    program = MagicMock()
    vfs = MagicMock()
    program.riven_vfs = vfs

    with patch("routers.secure.items.di", {Program: program}):
        response = await blacklist_stream(item_id=episode.id, stream_id=stream.id)

    assert "Blacklisted active stream" in response.message
    assert season.blacklisted_streams == [stream]
    assert season.streams == []
    assert season.active_stream is stream
    assert episode.active_stream is None
    assert episode.filesystem_entries == []
    assert episode.subtitles == []
    episode.store_state.assert_called_once_with(States.Indexed)
    vfs.remove.assert_called_once_with(episode)
    program.em.add_event.assert_called_once()
    event = program.em.add_event.call_args.args[0]
    assert event.emitted_by == "RetryItem"
    assert event.item_id == episode.id


def test_unblacklist_resolves_parent_owned_blacklisted_stream():
    """An episode request can restore a stream moved to its season blacklist."""
    stream = FakeStream(7, "a" * 40)
    season = SimpleNamespace(
        id=10,
        log_string="Season 1",
        streams=[],
        blacklisted_streams=[stream],
        parent=None,
    )
    episode = SimpleNamespace(
        id=42,
        log_string="Episode 1",
        streams=[],
        blacklisted_streams=[],
        parent=season,
    )

    assert MediaItem.get_stream_owner(episode, stream_id=stream.id) is season

    MediaItem.unblacklist_stream(episode, stream)

    assert season.blacklisted_streams == []
    assert season.streams == [stream]


@pytest.mark.asyncio
async def test_reset_item_streams_preserves_active_stream_and_unblacklists(monkeypatch):
    """reset_item_streams unblacklists streams without wiping active_stream or candidate streams."""
    from contextlib import contextmanager

    from routers.secure.items import reset_item_streams

    active_stream = FakeStream(1, "1" * 40)
    existing_candidate = FakeStream(2, "2" * 40)
    blacklisted_stream = FakeStream(3, "3" * 40)

    season = SimpleNamespace(
        id=10,
        log_string="Season 1",
        streams=[],
        blacklisted_streams=[],
        parent=None,
    )
    episode = SimpleNamespace(
        id=42,
        log_string="Episode 1",
        streams=[existing_candidate],
        blacklisted_streams=[blacklisted_stream],
        active_stream=active_stream,
        parent=season,
    )

    class DummyExecute:
        def unique(self):
            return self

        def scalar_one_or_none(self):
            return episode

    class DummySession:
        def execute(self, _query):
            return DummyExecute()

        def commit(self):
            pass

    @contextmanager
    def dummy_db_session():
        yield DummySession()

    monkeypatch.setattr("routers.secure.items.db_session", dummy_db_session)

    response = await reset_item_streams(item_id=42)

    assert "Successfully reset streams for item 42" in response.message
    # Active stream must be preserved
    assert episode.active_stream is active_stream
    # Blacklisted stream moved back to streams
    assert blacklisted_stream not in episode.blacklisted_streams
    assert blacklisted_stream in episode.streams
    # Existing candidate preserved
    assert existing_candidate in episode.streams


@pytest.mark.asyncio
async def test_reset_item_streams_unblacklists_parent_owned_streams(monkeypatch):
    """reset_item_streams resolves parent-owned blacklisted streams and unblacklists them on owner."""
    from contextlib import contextmanager

    from routers.secure.items import reset_item_streams

    parent_blacklisted = FakeStream(9, "9" * 40)
    season = SimpleNamespace(
        id=10,
        log_string="Season 1",
        streams=[],
        blacklisted_streams=[parent_blacklisted],
        parent=None,
    )
    episode = SimpleNamespace(
        id=42,
        log_string="Episode 1",
        streams=[],
        blacklisted_streams=[],
        active_stream=None,
        parent=season,
    )

    class DummyExecute:
        def unique(self):
            return self

        def scalar_one_or_none(self):
            return episode

    class DummySession:
        def execute(self, _query):
            return DummyExecute()

        def commit(self):
            pass

    @contextmanager
    def dummy_db_session():
        yield DummySession()

    monkeypatch.setattr("routers.secure.items.db_session", dummy_db_session)

    response = await reset_item_streams(item_id=42)

    assert "Successfully reset streams for item 42" in response.message
    # Stream should be moved back to season.streams
    assert season.blacklisted_streams == []
    assert parent_blacklisted in season.streams


@pytest.mark.asyncio
async def test_reset_item_streams_auto_reevaluates_restored_candidate(monkeypatch):
    """
    Test 1: Downloader.run ranks and selects restored candidate without triggering scrapers.
    When an item with no active playback resets streams, it transitions to States.Scraped
    and dispatches RetryItem post-commit, enabling Downloader.run to pick the best restored candidate.
    """
    from contextlib import contextmanager

    from loguru import logger

    from program.program import Program
    from program.services.downloaders import Downloader
    from routers.secure.items import reset_item_streams

    try:
        logger.level("DEBRID")
    except ValueError:
        logger.level("DEBRID", no=20)

    restored_low = FakeStream(101, "1" * 40)
    restored_low.rank = 50
    restored_low.resolution = "720p"
    restored_low.raw_title = "Candidate.720p.mkv"

    restored_high = FakeStream(102, "2" * 40)
    restored_high.rank = 200
    restored_high.resolution = "1080p"
    restored_high.raw_title = "Candidate.1080p.mkv"

    item = SimpleNamespace(
        id=55,
        log_string="Movie 55",
        streams=[],
        blacklisted_streams=[restored_low, restored_high],
        active_stream=None,
        last_state=States.Indexed,
        parent=None,
        store_state=MagicMock(),
    )

    class DummyExecute:
        def unique(self):
            return self

        def scalar_one_or_none(self):
            return item

    committed = False

    class DummySession:
        def execute(self, _query):
            return DummyExecute()

        def commit(self):
            nonlocal committed
            committed = True

    @contextmanager
    def dummy_db_session():
        yield DummySession()

    monkeypatch.setattr("routers.secure.items.db_session", dummy_db_session)

    mock_em = MagicMock()
    mock_em.add_event.return_value = True
    program = MagicMock()
    program.em = mock_em

    with patch("routers.secure.items.di", {Program: program}):
        response = await reset_item_streams(item_id=55)

    assert "Successfully reset streams for item 55" in response.message
    assert committed is True
    # Streams moved from blacklisted_streams to streams
    assert item.blacklisted_streams == []
    assert set(item.streams) == {restored_low, restored_high}
    # State transitioned to Scraped for direct Downloader execution
    item.store_state.assert_called_once_with(States.Scraped)
    # Event RetryItem emitted post-commit
    mock_em.add_event.assert_called_once()
    retry_event = mock_em.add_event.call_args.args[0]
    assert retry_event.emitted_by == "RetryItem"
    assert retry_event.item_id == 55

    # Verify Downloader.run candidate ranking selection using mock debrid service
    with patch.object(Downloader, "__init__", lambda *_: None):
        dl = Downloader()
        dl.initialized = True
        mock_svc = MagicMock()
        mock_svc.key = "realdebrid"
        mock_svc.initialized = True
        dl.services = {type(mock_svc): mock_svc}
        dl.initialized_services = [mock_svc]
        dl.service = mock_svc
        dl._service_cooldowns = {}
        dl.subtitles_enabled = False

        dl.validate_stream_on_service = MagicMock(return_value=SimpleNamespace(id="c1"))
        dl.download_cached_stream_on_service = MagicMock(
            return_value=SimpleNamespace(id="d1")
        )
        dl.update_item_attributes = MagicMock(return_value=True)

        runner_gen = dl.run(item)
        res = next(runner_gen, None)
        assert res is not None

        # Confirms Downloader evaluated the highest-ranked restored candidate first
        first_selected_stream = dl.validate_stream_on_service.call_args_list[0][0][0]
        assert first_selected_stream.id == restored_high.id
        assert first_selected_stream.rank == 200


@pytest.mark.asyncio
async def test_reset_item_streams_active_playback_noop_reevaluation(monkeypatch):
    """
    Test 2: Active playback presence prevents re-evaluation, state modification, and job cancellation.
    When an item has active_stream, blacklists are restored non-destructively, but state
    is NOT modified to States.Scraped and NO RetryItem event is dispatched.
    """
    from contextlib import contextmanager

    from program.program import Program
    from routers.secure.items import reset_item_streams

    active = FakeStream(1, "a" * 40)
    blacklisted = FakeStream(2, "b" * 40)

    item = SimpleNamespace(
        id=77,
        log_string="Movie 77",
        streams=[active],
        blacklisted_streams=[blacklisted],
        active_stream=active,
        last_state=States.Downloaded,
        parent=None,
        store_state=MagicMock(),
    )

    class DummyExecute:
        def unique(self):
            return self

        def scalar_one_or_none(self):
            return item

    class DummySession:
        def execute(self, _query):
            return DummyExecute()

        def commit(self):
            pass

    @contextmanager
    def dummy_db_session():
        yield DummySession()

    monkeypatch.setattr("routers.secure.items.db_session", dummy_db_session)

    mock_em = MagicMock()
    program = MagicMock()
    program.em = mock_em

    with patch("routers.secure.items.di", {Program: program}):
        response = await reset_item_streams(item_id=77)

    assert "Successfully reset streams for item 77" in response.message
    # Active playback remains active
    assert item.active_stream is active
    # Blacklisted stream unblacklisted
    assert blacklisted not in item.blacklisted_streams
    assert blacklisted in item.streams
    # NO state change or event emitted because active playback exists
    item.store_state.assert_not_called()
    mock_em.add_event.assert_not_called()
    mock_em.cancel_job.assert_not_called()


@pytest.mark.asyncio
async def test_reset_item_streams_season_pack_isolation(monkeypatch):
    """
    Test 3: Season pack isolation.
    An episode without streams unblacklisting a parent season pack stream must re-evaluate
    the target episode ONLY without modifying sibling episodes' active playback or streams.
    """
    from contextlib import contextmanager

    from program.media.item import Episode
    from program.program import Program
    from routers.secure.items import reset_item_streams

    parent_stream = FakeStream(99, "9" * 40)
    sibling_active = FakeStream(88, "8" * 40)

    season = SimpleNamespace(
        id=10,
        log_string="Season 1",
        streams=[],
        blacklisted_streams=[parent_stream],
        parent=None,
    )

    # Targeted episode 1: no active playback, no local streams
    target_episode = MagicMock(spec=Episode)
    target_episode.id = 101
    target_episode.log_string = "Episode 1"
    target_episode.streams = []
    target_episode.blacklisted_streams = []
    target_episode.active_stream = None
    target_episode.last_state = States.Indexed
    target_episode.parent = season

    # Sibling episode 2: has active playback and own stream
    sibling_episode = MagicMock(spec=Episode)
    sibling_episode.id = 102
    sibling_episode.log_string = "Episode 2"
    sibling_episode.streams = [sibling_active]
    sibling_episode.blacklisted_streams = []
    sibling_episode.active_stream = sibling_active
    sibling_episode.last_state = States.Downloaded
    sibling_episode.parent = season

    season.episodes = [target_episode, sibling_episode]

    class DummyExecute:
        def unique(self):
            return self

        def scalar_one_or_none(self):
            return target_episode

    class DummySession:
        def execute(self, _query):
            return DummyExecute()

        def commit(self):
            pass

    @contextmanager
    def dummy_db_session():
        yield DummySession()

    monkeypatch.setattr("routers.secure.items.db_session", dummy_db_session)

    mock_em = MagicMock()
    mock_em.add_event.return_value = True
    program = MagicMock()
    program.em = mock_em

    with patch("routers.secure.items.di", {Program: program}):
        response = await reset_item_streams(item_id=101)

    assert "Successfully reset streams for item 101" in response.message
    # Parent stream was restored to season.streams
    assert parent_stream in season.streams
    assert season.blacklisted_streams == []
    # Target episode was transitioned to Scraped and enqueued
    target_episode.store_state.assert_called_once_with(States.Scraped)
    mock_em.add_event.assert_called_once()
    event = mock_em.add_event.call_args.args[0]
    assert event.emitted_by == "RetryItem"
    assert event.item_id == 101

    # Sibling episode was completely isolated: no changes to its streams, active stream, or state
    assert sibling_episode.active_stream is sibling_active
    assert sibling_episode.streams == [sibling_active]
    assert sibling_episode.last_state == States.Downloaded
    sibling_episode.store_state.assert_not_called()


@pytest.mark.asyncio
async def test_reset_item_streams_real_sqlite_persistence():
    """
    Test 4: Fresh API read verifies persisted blacklist state using real SQLite database.
    """
    from contextlib import contextmanager

    from RTN import ParsedData, Torrent
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import routers.secure.items as items_mod
    from program.db.base_model import get_base_metadata
    from program.media.item import Movie
    from program.media.stream import Stream
    from program.program import Program

    engine = create_engine("sqlite:///:memory:")
    get_base_metadata().create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    with Session() as session:
        movie = Movie({"title": "SQLite Movie", "year": 2026, "imdb_id": "tt9999999"})
        movie.last_state = States.Indexed
        pd = ParsedData(parsed_title="SQLite Movie", raw_title="SQLite.Movie.1080p")
        torrent = Torrent(
            raw_title="SQLite.Movie.1080p",
            infohash="f" * 40,
            data=pd,
            fetch=True,
            rank=100,
            lev_ratio=1.0,
        )
        stream = Stream(torrent)
        session.add(movie)
        session.add(stream)
        session.flush()
        movie.blacklisted_streams.append(stream)
        session.commit()
        movie_id = movie.id

    @contextmanager
    def sqlite_db_session():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    orig_session = items_mod.db_session
    items_mod.db_session = sqlite_db_session

    mock_em = MagicMock()
    mock_em.add_event.return_value = True
    program = MagicMock()
    program.em = mock_em

    try:
        with patch("routers.secure.items.di", {Program: program}):
            response = await items_mod.reset_item_streams(item_id=movie_id)
    finally:
        items_mod.db_session = orig_session

    assert f"Successfully reset streams for item {movie_id}" in response.message

    # Fresh read from SQLite database verifying persisted state
    with Session() as verify_session:
        refreshed = verify_session.get(Movie, movie_id)
        assert refreshed is not None
        assert len(refreshed.blacklisted_streams) == 0
        assert len(refreshed.streams) == 1
        assert refreshed.streams[0].infohash == "f" * 40
        assert refreshed.last_state == States.Scraped


@pytest.mark.asyncio
async def test_reset_item_streams_commit_failure_no_dispatch(monkeypatch):
    """
    Test 5: Commit failure does NOT dispatch RetryItem event (idempotency & crash-consistency).
    """
    from contextlib import contextmanager

    from program.program import Program
    from routers.secure.items import reset_item_streams

    stream = FakeStream(1, "c" * 40)
    item = SimpleNamespace(
        id=66,
        log_string="Movie 66",
        streams=[],
        blacklisted_streams=[stream],
        active_stream=None,
        last_state=States.Indexed,
        parent=None,
        store_state=MagicMock(),
    )

    class DummyExecute:
        def unique(self):
            return self

        def scalar_one_or_none(self):
            return item

    class FailingSession:
        def execute(self, _query):
            return DummyExecute()

        def commit(self):
            raise RuntimeError("Database disk full or connection dropped")

    @contextmanager
    def failing_db_session():
        yield FailingSession()

    monkeypatch.setattr("routers.secure.items.db_session", failing_db_session)

    mock_em = MagicMock()
    program = MagicMock()
    program.em = mock_em

    with patch("routers.secure.items.di", {Program: program}):
        with pytest.raises(RuntimeError, match="Database disk full"):
            await reset_item_streams(item_id=66)

    # Event MUST NOT be dispatched if commit failed
    mock_em.add_event.assert_not_called()
