from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from RTN import ParsedData, Torrent
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from program.db.base_model import get_base_metadata
from program.media.item import Episode, Movie, Season, Show
from program.media.stream import Stream, StreamBlacklistRelation, StreamRelation
from program.settings import settings_manager
from routers import app_router


def create_candidate_stream(hash_char: str, title: str) -> Stream:
    return Stream(
        Torrent(
            raw_title=title,
            infohash=hash_char * 40,
            data=ParsedData(parsed_title=title, raw_title=title),
            fetch=True,
            rank=100,
            lev_ratio=1.0,
        )
    )


@pytest.fixture
def blacklist_test_context(monkeypatch):
    """Set up an isolated in-memory SQLite database and FastAPI TestClient."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    get_base_metadata().create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    @contextmanager
    def _test_session():
        session = TestingSession()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr("program.db.db.db_session", _test_session)

    app = FastAPI()
    app.include_router(app_router)
    client = TestClient(app)

    return client, TestingSession


def test_get_blacklisted_streams_count_empty(blacklist_test_context):
    client, _ = blacklist_test_context
    api_key = settings_manager.settings.api_key

    response = client.get(
        "/api/v1/settings/maintenance/blacklisted-streams/count",
        headers={"x-api-key": api_key},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 0
    assert "0 blacklisted" in data["message"]


def test_clear_all_blacklisted_streams_empty(blacklist_test_context):
    client, _ = blacklist_test_context
    api_key = settings_manager.settings.api_key

    response = client.post(
        "/api/v1/settings/maintenance/blacklisted-streams/clear",
        headers={"x-api-key": api_key},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["cleared_count"] == 0
    assert "0 blacklisted" in data["message"]


def test_clear_all_blacklisted_streams_multi_media_and_idempotence(
    blacklist_test_context,
):
    client, session_factory = blacklist_test_context
    api_key = settings_manager.settings.api_key

    with session_factory() as session:
        # Create a Movie
        movie = Movie({"title": "Inception"})
        session.add(movie)

        # Create a Show with Season and Episode
        show = Show({"title": "Breaking Bad"})
        season = Season({"number": 1})
        episode = Episode({"number": 1, "title": "Pilot"})

        season.parent = show
        episode.parent = season
        show.seasons.append(season)
        season.episodes.append(episode)

        session.add_all([show, season, episode])

        # Create Streams
        stream1 = create_candidate_stream("1", "Inception 1080p")
        stream2 = create_candidate_stream("2", "Inception 4K")
        stream3 = create_candidate_stream("3", "Breaking Bad S01E01 720p")
        stream_active = create_candidate_stream("4", "Inception Active 1080p")
        session.add_all([stream1, stream2, stream3, stream_active])
        session.flush()

        # Movie has stream_active as candidate
        session.add(StreamRelation(parent_id=movie.id, child_id=stream_active.id))

        # Movie has stream1, stream2 blacklisted
        session.add(
            StreamBlacklistRelation(media_item_id=movie.id, stream_id=stream1.id)
        )
        session.add(
            StreamBlacklistRelation(media_item_id=movie.id, stream_id=stream2.id)
        )

        # Episode has stream3 blacklisted
        session.add(
            StreamBlacklistRelation(media_item_id=episode.id, stream_id=stream3.id)
        )

        session.commit()

        movie_id = movie.id
        episode_id = episode.id
        s1_id = stream1.id
        s2_id = stream2.id
        s3_id = stream3.id
        s_act_id = stream_active.id

    # 1. Count should be 3
    count_res = client.get(
        "/api/v1/settings/maintenance/blacklisted-streams/count",
        headers={"x-api-key": api_key},
    )
    assert count_res.status_code == 200
    assert count_res.json()["count"] == 3

    # 2. Clear all
    clear_res = client.post(
        "/api/v1/settings/maintenance/blacklisted-streams/clear",
        headers={"x-api-key": api_key},
    )
    assert clear_res.status_code == 200
    assert clear_res.json()["cleared_count"] == 3

    # 3. Verify in database:
    # - StreamBlacklistRelation is empty
    # - StreamRelation contains stream_active, stream1, stream2 for movie, and stream3 for episode
    with session_factory() as session:
        blacklisted = session.execute(select(StreamBlacklistRelation)).scalars().all()
        assert len(blacklisted) == 0

        relations = session.execute(select(StreamRelation)).scalars().all()
        rel_tuples = {(r.parent_id, r.child_id) for r in relations}

        assert (movie_id, s_act_id) in rel_tuples
        assert (movie_id, s1_id) in rel_tuples
        assert (movie_id, s2_id) in rel_tuples
        assert (episode_id, s3_id) in rel_tuples
        assert len(relations) == 4

    # 4. Idempotency check: repeated calls succeed safely with count 0
    count_res2 = client.get(
        "/api/v1/settings/maintenance/blacklisted-streams/count",
        headers={"x-api-key": api_key},
    )
    assert count_res2.status_code == 200
    assert count_res2.json()["count"] == 0

    clear_res2 = client.post(
        "/api/v1/settings/maintenance/blacklisted-streams/clear",
        headers={"x-api-key": api_key},
    )
    assert clear_res2.status_code == 200
    assert clear_res2.json()["cleared_count"] == 0


@pytest.mark.parametrize("method,path", [("get", "count"), ("post", "clear")])
def test_global_blacklist_requires_auth(blacklist_test_context, method, path):
    client, _ = blacklist_test_context
    response = getattr(client, method)(
        f"/api/v1/settings/maintenance/blacklisted-streams/{path}"
    )
    assert response.status_code in (401, 403)


def test_clear_only_restores_and_deletes_its_snapshot(
    blacklist_test_context, monkeypatch
):
    client, factory = blacklist_test_context
    with factory() as session:
        movie = Movie({"title": "Snapshot"})
        first = create_candidate_stream("a", "First")
        later = create_candidate_stream("b", "Later")
        session.add_all([movie, first, later])
        session.flush()
        ids = movie.id, first.id, later.id
        session.add(StreamBlacklistRelation(media_item_id=ids[0], stream_id=ids[1]))
        session.commit()

    @contextmanager
    def interleaved_session():
        with factory() as session:
            original_execute = session.execute
            inserted = False

            def execute(statement, *args, **kwargs):
                nonlocal inserted
                if not inserted and "StreamRelation" in str(statement):
                    inserted = True
                    session.add(
                        StreamBlacklistRelation(media_item_id=ids[0], stream_id=ids[2])
                    )
                    session.flush()
                return original_execute(statement, *args, **kwargs)

            monkeypatch.setattr(session, "execute", execute)
            yield session

    monkeypatch.setattr("program.db.db.db_session", interleaved_session)
    response = client.post(
        "/api/v1/settings/maintenance/blacklisted-streams/clear",
        headers={"x-api-key": settings_manager.settings.api_key},
    )
    assert response.status_code == 200
    assert response.json()["cleared_count"] == 1
    with factory() as session:
        assert [
            (r.media_item_id, r.stream_id)
            for r in session.scalars(select(StreamBlacklistRelation))
        ] == [(ids[0], ids[2])]
        assert [
            (r.parent_id, r.child_id) for r in session.scalars(select(StreamRelation))
        ] == [(ids[0], ids[1])]


def test_clear_rolls_back_on_commit_failure(blacklist_test_context, monkeypatch):
    client, factory = blacklist_test_context
    with factory() as session:
        movie = Movie({"title": "Rollback"})
        stream = create_candidate_stream("c", "Rollback")
        session.add_all([movie, stream])
        session.flush()
        session.add(
            StreamBlacklistRelation(media_item_id=movie.id, stream_id=stream.id)
        )
        session.commit()

    @contextmanager
    def failing_session():
        with factory() as session:

            def fail_commit():
                session.flush()
                raise RuntimeError("simulated commit failure")

            monkeypatch.setattr(session, "commit", fail_commit)
            yield session

    monkeypatch.setattr("program.db.db.db_session", failing_session)
    with pytest.raises(RuntimeError, match="simulated commit failure"):
        client.post(
            "/api/v1/settings/maintenance/blacklisted-streams/clear",
            headers={"x-api-key": settings_manager.settings.api_key},
        )
    with factory() as session:
        assert len(session.scalars(select(StreamBlacklistRelation)).all()) == 1
        assert session.scalars(select(StreamRelation)).all() == []
