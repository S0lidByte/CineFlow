"""Episode scrape must match season+episode, not episode number alone."""

from RTN import parse

from program.media.item import Episode, Season
from program.services.scrapers.shared import (
    _normalize_episode_title_notation,
    episode_release_matches,
)


def test_malformed_season_dash_episode_is_normalized_for_rtn_and_preserved():
    """Doomdos-style ``Season 3 - 9`` must rank as S03E09 without UI title drift."""
    raw_title = (
        "[Doomdos] - Mushoku Tensei Jobless Reincarnation Season 3 - 9 "
        "[2160p IQ WEB-DL]"
    )

    season = Season({"number": 3})
    episode = Episode({"number": 9})
    episode.parent = season

    normalized_title = _normalize_episode_title_notation(raw_title, episode)
    parsed = parse(normalized_title)

    assert parsed.seasons == [3]
    assert parsed.episodes == [9]
    assert raw_title == (
        "[Doomdos] - Mushoku Tensei Jobless Reincarnation Season 3 - 9 "
        "[2160p IQ WEB-DL]"
    )


def test_episode_release_rejects_wrong_season_same_episode_number():
    """S08E14 must not match S06E14 (Blacklist log regression)."""
    assert not episode_release_matches(
        episode_number=14,
        absolute_number=None,
        season_number=6,
        parsed_episodes=[14],
        parsed_seasons=[8],
    )
    assert not episode_release_matches(
        episode_number=14,
        absolute_number=None,
        season_number=6,
        parsed_episodes=[14],
        parsed_seasons=[5],
    )
    assert not episode_release_matches(
        episode_number=14,
        absolute_number=None,
        season_number=6,
        parsed_episodes=[14],
        parsed_seasons=[9],
    )


def test_episode_release_accepts_correct_season_and_episode():
    assert episode_release_matches(
        episode_number=14,
        absolute_number=None,
        season_number=6,
        parsed_episodes=[14],
        parsed_seasons=[6],
    )


def test_episode_release_accepts_explicit_season_pack_containing_parent_season():
    assert episode_release_matches(
        episode_number=14,
        absolute_number=None,
        season_number=6,
        parsed_episodes=None,
        parsed_seasons=[1, 2, 3, 4, 5, 6, 7, 8, 9],
        raw_title="Show Name Seasons 1-9 Complete 1080p",
        is_complete=True,
    )


def test_episode_release_rejects_unmarked_season_only_release():
    """A bare season tag is not evidence that it includes every episode."""
    assert not episode_release_matches(
        episode_number=9,
        absolute_number=None,
        season_number=3,
        parsed_episodes=None,
        parsed_seasons=[3],
        raw_title="Show Name Season 3 2160p WEB-DL",
    )


def test_episode_release_rejects_season_pack_missing_parent_season():
    assert not episode_release_matches(
        episode_number=14,
        absolute_number=None,
        season_number=6,
        parsed_episodes=None,
        parsed_seasons=[1, 2, 3],
        raw_title="Show Name Seasons 1-3 Complete 1080p",
        is_complete=True,
    )


def test_episode_release_rejects_wrong_episode_number():
    assert not episode_release_matches(
        episode_number=14,
        absolute_number=None,
        season_number=6,
        parsed_episodes=[9],
        parsed_seasons=[6],
    )


def test_episode_release_accepts_absolute_number_match():
    assert episode_release_matches(
        episode_number=14,
        absolute_number=120,
        season_number=6,
        parsed_episodes=[120],
        parsed_seasons=[6],
    )


def test_episode_release_rejects_relative_episode_without_season():
    """E14 with no season tag must not match any season (CodeRabbit gap)."""
    assert not episode_release_matches(
        episode_number=14,
        absolute_number=None,
        season_number=6,
        parsed_episodes=[14],
        parsed_seasons=None,
    )
    assert not episode_release_matches(
        episode_number=14,
        absolute_number=None,
        season_number=6,
        parsed_episodes=[14],
        parsed_seasons=[],
    )


def test_episode_release_accepts_absolute_without_season():
    """Anime absolute episode numbers may omit season tags."""
    assert episode_release_matches(
        episode_number=14,
        absolute_number=120,
        season_number=6,
        parsed_episodes=[120],
        parsed_seasons=None,
    )


def test_episode_release_absolute_when_equal_to_relative_without_season():
    """When abs == episode number and seasons empty, absolute path still matches."""
    assert episode_release_matches(
        episode_number=1,
        absolute_number=1,
        season_number=1,
        parsed_episodes=[1],
        parsed_seasons=None,
    )
