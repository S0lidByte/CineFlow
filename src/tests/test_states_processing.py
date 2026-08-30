from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from program.media.item import Episode, MediaItem, Movie, Season, Show
from program.media.state import States
from program.state_transition import process_event


@pytest.fixture
def movie():
    return Movie({"imdb_id": "tt1375666", "requested_by": "Iceberg"})


@pytest.fixture
def show():
    show = Show({"imdb_id": "tt0903747", "requested_by": "Iceberg"})
    season = Season({"number": 1})
    episode = Episode({"number": 1})
    season.add_episode(episode)
    show.add_season(season)
    return show


@pytest.fixture
def media_item_movie():
    return MediaItem({"imdb_id": "tt1375666", "requested_by": "Iceberg"})


@pytest.fixture
def media_item_show():
    show = MediaItem({"imdb_id": "tt0903747", "requested_by": "Iceberg"})
    season = MediaItem({"number": 1})
    episode = MediaItem({"number": 1})
    season.add_episode(episode)
    show.add_season(season)
    return show


@pytest.fixture
def season(show):
    return show.seasons[0]


@pytest.fixture
def episode(season):
    return season.episodes[0]


@pytest.fixture
def services():
    return SimpleNamespace(
        indexer=MagicMock(name="indexer"),
        scraping=MagicMock(name="scraping"),
        downloader=MagicMock(name="downloader"),
        filesystem=MagicMock(name="filesystem"),
        updater=MagicMock(name="updater"),
        post_processing=MagicMock(name="post_processing"),
    )


@pytest.fixture
def process_event_with_services(services):
    program = SimpleNamespace(services=services)
    with patch("program.state_transition.di") as mock_di:
        mock_di.__getitem__.return_value = program
        yield services


def test_initial_state(movie, show, season, episode):
    """Test that items start in the Unknown state."""
    # Given: A new media item (movie, episode, season, show)
    # When: The item is first created
    # Then: The item's state should be Unknown

    # As long as we initialize Movies with an imdb_id and requested_by,
    # it should end up as Requested.
    assert movie.state == States.Requested, "Movie should start in Requested state"

    # A show with only unaired seasons is unreleased; its child hierarchy is unknown.
    assert show.state == States.Unreleased, "Show should start unreleased when its seasons have not aired"
    assert season.state == States.Unknown, "Season should start in Unknown state"
    assert episode.state == States.Unknown, "Episode should start in Unknown state"


def test_requested_state(movie):
    """Test transition to the Requested state."""
    # Given: A media item (movie)
    movie.set("requested_by", "user")
    # When: The item is requested by a user
    # Then: The item's state should be Requested
    assert movie.state == States.Requested, "Movie should transition to Requested state"


def test_indexed_state(movie):
    """Test transition to the Indexed state."""
    # Given: A released media item (movie) with indexed metadata
    movie.set("title", "Inception")
    movie.aired_at = datetime.now() - timedelta(days=1)
    # Then: The item's state should be Indexed
    assert movie.state == States.Indexed, "Released movie metadata should transition to Indexed"


def test_scraped_state(episode):
    """Test transition to the Scraped state."""
    # Given: A media item (episode) with a usable stream relationship
    episode.is_scraped = lambda: True
    # Then: The item's state should be Scraped
    assert episode.state == States.Scraped, "Episode should transition to Scraped state"


def test_downloaded_state(episode):
    """Test transition to the Downloaded state."""
    # Given: A media item (episode) with a downloader-created filesystem entry
    with patch.object(Episode, "filesystem_entry", new_callable=PropertyMock, return_value=object()):
        # Then: The item's state should be Downloaded
        assert episode.state == States.Downloaded, (
            "Episode should transition to Downloaded when it has a filesystem entry"
        )


def test_completed_state(movie):
    """Test transition to the Completed state."""
    # Given: A media item (movie) marked updated after library processing
    movie.updated = True
    # Then: The item's state should be Completed
    assert movie.state == States.Completed, "Updated movie should transition to Completed state"


def test_show_state_transitions(show):
    """Test full state transitions of a show."""
    # Given: A show whose only episode completed library processing
    show.seasons[0].episodes[0].updated = True

    # Then: The show's state should transition based on its episodes and seasons
    assert show.state == States.Completed, "Show should transition to Completed state"


@pytest.mark.parametrize(
    "state, emitted_by, expected_service",
    [
        (States.Unknown, "StateTransition", "scraping"),
        (States.Indexed, "StateTransition", "scraping"),
        (States.Scraped, "StateTransition", "downloader"),
        (States.Downloaded, "StateTransition", "filesystem"),
        (States.Symlinked, "StateTransition", "updater"),
        (States.Completed, "StateTransition", "post_processing"),
        (States.Completed, "post_processing", None),
    ],
)
@pytest.mark.parametrize("item_fixture", ["movie", "show", "media_item_movie"])
def test_process_event_transitions(
    request, state, emitted_by, expected_service, item_fixture, process_event_with_services
):
    """Test each state routes to the active service instance or stops processing."""
    item = request.getfixturevalue(item_fixture)
    item.last_state = state
    services = process_event_with_services
    emitter = getattr(services, emitted_by) if emitted_by != "StateTransition" else emitted_by

    processed_event = process_event(emitter, existing_item=item)

    expected = getattr(services, expected_service) if expected_service else None
    assert processed_event.service is expected


# test media item show
# @pytest.mark.parametrize("state, service, next_service", [
#     (States.Unknown, Program, TraktIndexer),
#     # (States.Requested, TraktIndexer, TraktIndexer),
#     (States.Indexed, TraktIndexer, Scraping),
#     (States.Scraped, Scraping, Debrid),
#     (States.Downloaded, Debrid, FilesystemService),
#     (States.Symlinked, FilesystemService, PlexUpdater),
#     (States.Completed, PlexUpdater, None)
# ])
# def test_process_event_transitions_media_item_show(state, service, next_service, media_item_show):
#     """Test processing events for state transitions."""
#     # Given: A media item (movie) and a service
#     media_item_show._determine_state = lambda: state

#     # When: The event is processed
#     updated_item, next_service_result, items_to_submit = process_event(None, service, media_item_show)

#     if next_service is Scraping:
#         assert isinstance(updated_item, Show), "Updated item should be of type Show"

#     # Then: The next service should be as expected based on the current service
#     if next_service is None:
#         assert next_service_result is None, f"Next service should be None for {service}"
#     else:
#         assert next_service_result == next_service, f"Next service should be {next_service} for {service}"
