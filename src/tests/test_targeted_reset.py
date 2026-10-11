"""Target-only reset regressions using real media objects and persisted relations."""

from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from RTN import ParsedData, Torrent
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from program.db.base_model import get_base_metadata
from program.media.item import Episode, Season, Show
from program.media.state import States
from program.media.stream import Stream
from program.program import Program
from program.services.downloaders import Downloader
from routers.models.shared import IdListPayload
from routers.secure.items import reset_item_streams, reset_items


def candidate(hash_char, title):
    return Stream(
        Torrent(
            raw_title=title,
            infohash=hash_char * 40,
            data=ParsedData(parsed_title="Example", raw_title=title),
            fetch=True,
            rank=100,
            lev_ratio=1.0,
        )
    )


def hierarchy():
    show = Show({"title": "Example", "imdb_id": "tt1234567"})
    season = Season({"number": 1})
    episode = Episode({"number": 1, "title": "Pilot"})
    sibling = Episode({"number": 2, "title": "Second"})
    show.seasons = [season]
    season.episodes = [episode, sibling]
    for item in (show, season, episode, sibling):
        item.aired_at = datetime(2020, 1, 1)
        item.last_state = States.Indexed
    return show, season, episode, sibling


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [False, True])
async def test_episode_restore_persists_local_candidates_and_can_reblacklist(active):
    engine = create_engine("sqlite:///:memory:")
    get_base_metadata().create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions() as db:
        show, season, target, sibling = hierarchy()
        own = candidate("a", "Example.S01E01.1080p.WEB-DL")
        shared = candidate("b", "Example.S01.Complete.1080p.WEB-DL")
        wrong = candidate("c", "Example.S01E02.1080p.WEB-DL")
        target.blacklisted_streams = [own]
        season.blacklisted_streams = [shared, wrong]
        sibling.streams = [shared]
        db.add(show)
        db.commit()
        target_id, season_id, sibling_id = target.id, season.id, sibling.id

    committed = False
    program = MagicMock()

    @contextmanager
    def session_scope():
        nonlocal committed
        with sessions() as db:
            original_commit = db.commit

            def commit():
                nonlocal committed
                original_commit()
                committed = True

            db.commit = commit
            yield db

    def dispatch(event):
        assert committed
        assert event.item_id == target_id
        return True

    program.em.add_event.side_effect = dispatch
    # ActiveStream is JSON-backed; use its real model when testing preservation.
    if active:
        from program.media.models import ActiveStream

        with sessions() as db:
            target = db.get(Episode, target_id)
            target.active_stream = ActiveStream(infohash="d" * 40, id="active")
            db.commit()

    with (
        patch("routers.secure.items.db_session", session_scope),
        patch("routers.secure.items.di", {Program: program}),
    ):
        await reset_item_streams(target_id)

    with sessions() as db:
        target = db.get(Episode, target_id)
        season = db.get(Season, season_id)
        sibling = db.get(Episode, sibling_id)
        assert {s.infohash for s in target.streams} == {"a" * 40, "b" * 40}
        assert target.blacklisted_streams == []
        assert {s.infohash for s in season.blacklisted_streams} == {"b" * 40, "c" * 40}
        assert season.streams == []
        assert len(sibling.streams) == 1
        assert sibling.last_state == States.Indexed
        assert target.last_state == (States.Indexed if active else States.Scraped)
        assert bool(target.active_stream) is active
        restored = next(s for s in target.streams if s.infohash == "b" * 40)
        assert restored.id != sibling.streams[0].id
        assert target.blacklist_stream(restored)
        db.commit()
        assert restored in target.blacklisted_streams
        assert sibling.streams[0].infohash == "b" * 40
    assert program.em.add_event.call_count == (0 if active else 1)
    program.em.cancel_job.assert_not_called()
    engine.dispose()


@pytest.mark.parametrize("linked", [False, True])
def test_episode_pack_rejects_sibling_before_noop(linked):
    _, _, target, sibling = hierarchy()
    target.id, sibling.id = 1, 2
    resolved = MagicMock(spec=Episode)
    resolved.id = sibling.id
    resolved.filesystem_entry = object() if linked else None
    show = MagicMock(spec=Show)
    show.get_absolute_episode.return_value = resolved
    downloader = object.__new__(Downloader)
    downloader.service = MagicMock()
    downloader._update_attributes = MagicMock()
    matched = downloader.match_file_to_item(
        target,
        ParsedData(raw_title="Example.S01E02", episodes=[2], seasons=[1]),
        SimpleNamespace(filename="Example.S01E02.mkv"),
        MagicMock(),
        show,
    )
    assert matched is False
    downloader._update_attributes.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_commit", [False, True])
@pytest.mark.parametrize("initial", [States.Paused, States.Failed, States.Completed])
async def test_main_reset_initial_state_and_postcommit_dispatch(initial, fail_commit):
    _, season, target, sibling = hierarchy()
    target.id, sibling.id = 1, 2
    target.last_state = initial
    shared = candidate("a", "Example.S01.Complete")
    season.streams = [shared]
    from program.media.models import ActiveStream

    target.active_stream = ActiveStream(infohash=shared.infohash, id="active")
    sibling.last_state = States.Completed
    db = MagicMock()
    db.execute.return_value.scalars.return_value.all.return_value = [target]
    committed = False

    def commit():
        nonlocal committed
        if fail_commit:
            raise RuntimeError("commit failed")
        committed = True

    db.commit.side_effect = commit
    program = MagicMock()
    program.services.updater = None

    def mutate(_program, session, item, mutation, bubble_parents):
        assert not bubble_parents
        mutation(item, session)

    def dispatch(event):
        assert committed
        assert event.item_id == target.id
        assert target.last_state == States.Indexed
        return True

    program.em.add_event.side_effect = dispatch
    with (
        patch("routers.secure.items.db_session") as scope,
        patch("routers.secure.items.di", {Program: program}),
        patch("routers.secure.items.apply_item_mutation", side_effect=mutate),
        patch("program.program.riven", None),
    ):
        scope.return_value.__enter__.return_value = db
        await reset_items(IdListPayload(ids=[str(target.id)]))
    assert program.em.add_event.call_count == (0 if fail_commit else 1)
    assert target.last_state == States.Indexed
    assert season.streams == [shared]
    assert season.blacklisted_streams == []
    assert sibling.last_state == States.Completed
