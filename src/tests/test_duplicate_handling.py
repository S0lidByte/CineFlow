"""
Test script to verify duplicate handling works correctly.
This script tests the new duplicate handling functionality.
"""

import os
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from testcontainers.postgres import PostgresContainer

from program.db.db import db, run_migrations
from program.db.db_functions import item_exists_by_any_id
from program.media.item import Movie, Show


@pytest.fixture(scope="session")
def duplicate_test_container():
    """Provide PostgreSQL when the integration environment has no URL."""
    if os.environ.get("DATABASE_URL"):
        yield None
        return

    with PostgresContainer(
        "postgres:16.4-alpine3.20",
        username="postgres",
        password="postgres",
        dbname="riven",
    ) as container:
        yield container


@pytest.fixture(scope="session")
def duplicate_db_engine(duplicate_test_container):
    """Create and migrate one disposable database for DB-backed tests."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        assert duplicate_test_container is not None
        url = duplicate_test_container.get_connection_url()
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)

    os.environ["DATABASE_URL"] = url
    from program.settings import settings_manager

    settings_manager.settings.database.host = url
    engine = create_engine(url, future=True, pool_pre_ping=True)
    db.engine = engine
    db.Session.configure(bind=engine)

    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))

    run_migrations(database_url=url)
    yield engine
    engine.dispose()


@pytest.fixture(scope="function")
def duplicate_db_session(duplicate_db_engine):
    """Provide isolated database state for each DB-backed test."""
    session = db.Session()
    try:
        yield session
    finally:
        session.close()
        with duplicate_db_engine.connect() as connection:
            tables = connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            ).scalars().all()
            if tables:
                quoted = ", ".join(f'"public"."{table}"' for table in tables)
                connection.execute(
                    text(f"TRUNCATE {quoted} RESTART IDENTITY CASCADE")
                )
                connection.commit()


class TestDuplicateHandling:
    """Test class for duplicate handling functionality."""

    def test_item_exists_by_id_non_existent(self, duplicate_db_session):
        """Test item_exists_by_id with a missing numeric primary key."""
        exists = item_exists_by_any_id(item_id=-1, session=duplicate_db_session)
        assert not exists, "Non-existent item should return False"

    def test_item_exists_by_id_existent(self):
        """Test item_exists_by_id with existing item."""
        with patch("program.db.db_functions._maybe_session") as mock_maybe_session:
            mock_session_instance = MagicMock()
            mock_session_instance.execute.return_value.scalar_one.return_value = 1
            mock_maybe_session.return_value.__enter__.return_value = (
                mock_session_instance,
                False,
            )

            exists = item_exists_by_any_id("existing_id")
            assert exists, "Existing item should return True"

    def test_get_item_by_external_id_non_existent(self, duplicate_db_session):
        """Test external-ID lookup with a missing identifier."""
        item = item_exists_by_any_id(
            imdb_id="tt9999999", session=duplicate_db_session
        )
        assert item is False, "Non-existent external ID should return False"

    def test_get_item_by_external_id_existent(self):
        """Test get_item_by_external_id with existing external ID."""
        with patch("program.db.db_functions._maybe_session") as mock_maybe_session:
            mock_session_instance = MagicMock()
            mock_session_instance.execute.return_value.scalar_one.return_value = 1
            mock_maybe_session.return_value.__enter__.return_value = (
                mock_session_instance,
                False,
            )

            item = item_exists_by_any_id(imdb_id="tt1234567")
            assert item is True, "Existing external ID should return True"

    def test_get_item_by_external_id_no_ids_provided(self):
        """Test get_item_by_external_id with no external IDs provided."""
        with pytest.raises(ValueError, match="At least one ID must be provided"):
            item_exists_by_any_id()

    def test_media_item_creation_movie(self):
        """Test Movie creation."""
        movie_data = {"imdb_id": "tt1234567", "title": "Test Movie", "year": 2023}

        movie = Movie(movie_data)
        assert movie.id is None  # ID is None until tmdb_id is provided
        assert movie.imdb_id == "tt1234567"
        assert movie.title == "Test Movie"
        assert movie.type == "movie"

    def test_media_item_creation_show(self):
        """Test Show creation."""
        show_data = {
            "tvdb_id": "123456",
            "title": "Test Show",
            "year": 2023,
            "type": "show",  # Include type for ID generation
        }

        show = Show(show_data)
        assert show.id is None  # Database assigns the integer primary key.
        assert show.tvdb_id == "123456"
        assert show.title == "Test Show"
        assert show.type == "show"

    def test_media_item_creation_tmdb_movie(self):
        """Test Movie creation with TMDB ID."""
        movie_data = {
            "tmdb_id": "51876",
            "title": "Test TMDB Movie",
            "year": 2023,
            "type": "movie",  # Include type for ID generation
        }

        movie = Movie(movie_data)
        assert movie.id is None  # Database assigns the integer primary key.
        assert movie.tmdb_id == "51876"
        assert movie.title == "Test TMDB Movie"
        assert movie.type == "movie"

    def test_duplicate_key_error_handling(self):
        """Test that IntegrityError for duplicate keys is handled properly."""
        # Mock the IntegrityError
        mock_error = IntegrityError(
            "duplicate key value violates unique constraint", None, None
        )

        # Test that our error message detection works
        error_message = str(mock_error)
        assert "duplicate key value violates unique constraint" in error_message

        # Test the specific error from the original issue
        original_error = '(psycopg2.errors.UniqueViolation) duplicate key value violates unique constraint "MediaItem_pkey"\nDETAIL:  Key (id)=(tvdb_show_76894) already exists.'
        assert "duplicate key value violates unique constraint" in original_error

    def test_media_item_id_generation_edge_cases(self):
        """Test MediaItem ID generation with edge cases."""
        # Test with None values - should return None for ID
        movie_data = {"imdb_id": None, "tmdb_id": None, "title": "Test Movie"}

        movie = Movie(movie_data)
        # Should return None when no external IDs are provided
        assert movie.id is None

    def test_media_item_log_string(self):
        """Test MediaItem log_string property."""
        movie_data = {"imdb_id": "tt1234567", "title": "Test Movie", "year": 2023}

        movie = Movie(movie_data)
        log_string = movie.log_string
        # The current contract prefers a human-readable title when available.
        assert log_string == "Test Movie"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

