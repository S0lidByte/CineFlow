"""Automatic dead-link removal must preserve blacklist and queue re-scrape."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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
    )
    episode.blacklist_active_stream = Episode.blacklist_active_stream.__get__(
        episode, SimpleNamespace
    )
    episode.blacklist_stream = Episode.blacklist_stream.__get__(episode, SimpleNamespace)
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
