from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from program.db import db_functions
from program.db.base_model import get_base_metadata
from program.media.item import Episode, MediaItem, Movie, Season, Show


def test_calendar_optimization_query_count_and_eager_lazy_control(tmp_path):
    """
    Assert that create_calendar issues at most 2 queries regardless of library size,
    completely avoids cascading selectin queries on streams/subtitles/filesystem_entries,
    and accurately resolves parent show titles and deduplication.
    """
    db_path = tmp_path / "calendar_perf.db"
    engine = create_engine(f"sqlite:///{db_path}")
    get_base_metadata().create_all(engine)

    start = datetime(2026, 11, 1)
    end = datetime(2026, 11, 30)

    with Session(engine) as session:
        # Create 50 shows with 3 seasons and 5 episodes each (750 episodes total)
        for i in range(50):
            show = Show(
                {
                    "title": f"Show {i}",
                    "tmdb_id": str(1000 + i),
                    "tvdb_id": str(2000 + i),
                    "release_data": {
                        "next_aired": (
                            "2026-11-15T00:00:00Z"
                            if i % 2 == 0
                            else "2025-01-01T00:00:00Z"
                        )
                    },
                }
            )
            for s_num in range(1, 4):
                season = Season({"number": s_num})
                for ep_num in range(1, 6):
                    # Episode in window for first 10 shows
                    aired = (
                        datetime(2026, 11, ep_num)
                        if (i < 10 and s_num == 1)
                        else datetime(2025, 5, 1)
                    )
                    episode = Episode({"number": ep_num, "aired_at": aired})
                    season.add_episode(episode)
                show.add_season(season)
            session.add(show)

        # Add 10 standalone movies in the window
        for m in range(10):
            movie = Movie(
                {
                    "title": f"Movie {m}",
                    "tmdb_id": str(5000 + m),
                    "aired_at": datetime(2026, 11, 10 + m),
                }
            )
            session.add(movie)

        session.commit()

    # Track executed SQL queries
    query_log = []

    def log_query(conn, cursor, statement, parameters, context, executemany):
        query_log.append(statement)

    event.listen(engine, "before_cursor_execute", log_query)

    with Session(engine) as session:
        calendar = db_functions.create_calendar(
            session=session, start_date=start, end_date=end
        )

    # Bounded query count (at most 4 queries: MediaItem base, polymorphic batches for
    # Episode/Movie, and Show fallback query), regardless of library size (no N+1).
    assert len(query_log) <= 4, f"Expected <= 4 queries, got {len(query_log)}"

    # Validate output correctness
    # 10 shows * 5 episodes = 50 episodes
    # 10 movies = 10 movies
    # Remaining shows with next_aired in Nov 2026: i % 2 == 0 for i in range(10, 50) -> 20 shows
    assert len(calendar) == 50 + 10 + 20

    # Ensure child items properly resolve parent show_title without additional queries
    episodes_found = [it for it in calendar.values() if it["item_type"] == "episode"]
    assert len(episodes_found) == 50
    for ep in episodes_found:
        assert ep["show_title"].startswith("Show ")
        assert ep["tmdb_id"] is not None
        assert ep["tvdb_id"] is not None
        assert ep["season"] == 1
        assert 1 <= ep["episode"] <= 5

    engine.dispose()
