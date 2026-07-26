"""Characterization harness for guessit (via subliminal) before a v4 bump.

CineFlow does not import guessit directly; subliminal does (Video.fromname).
guessit 4.x changes rebulk and anime absolute-episode / release_group rules.
These fixtures lock 3.8.x + subliminal 2.6 behavior so a future bump can be
diffed safely instead of blind-upgrading.
"""

from __future__ import annotations

from dataclasses import dataclass

import guessit as guessit_mod
import pytest
from guessit import guessit
from subliminal.video import Episode, Movie, Video


def test_guessit_major_is_still_v3() -> None:
    version = getattr(guessit_mod, "__version__", "0.0.0")
    major = int(str(version).split(".", maxsplit=1)[0])
    assert major == 3, f"expected guessit 3.x baseline, got {version}"


@dataclass(frozen=True)
class GuessCase:
    name: str
    """Filename / release name fed to guessit + Video.fromname."""
    expect_type: str
    """guessit 'type' field."""
    title: str
    """guessit title (series or movie)."""
    season: int | None = None
    episode: int | list[int] | None = None
    year: int | None = None
    release_group: str | None = None
    video_cls: type = Episode
    """Expected subliminal Video subclass."""
    video_season: int | None = None
    """subliminal may default season when guessit omits it."""
    video_episode: int | None = None
    """subliminal may coerce list episodes to the first value."""
    video_release_group: str | None = None
    """Override when subliminal differs from raw guessit release_group."""


# Golden matrix: dotted scene + bracket anime + remake + multi-ep.
# Values captured against guessit 3.8.0 / subliminal 2.6.0 (2026-07-26).
CASES: tuple[GuessCase, ...] = (
    GuessCase(
        name="Black.Torch.S01E04.1080p.WEB.h264-GROUP.mkv",
        expect_type="episode",
        title="Black Torch",
        season=1,
        episode=4,
        release_group="GROUP",
        video_season=1,
        video_episode=4,
    ),
    GuessCase(
        name="Show.Name.S02E05.720p.HDTV.x264-GROUP.mkv",
        expect_type="episode",
        title="Show Name",
        season=2,
        episode=5,
        release_group="GROUP",
        video_season=2,
        video_episode=5,
    ),
    GuessCase(
        name="Show.Name.1x05.720p.HDTV.x264-GROUP.mkv",
        expect_type="episode",
        title="Show Name",
        season=1,
        episode=5,
        release_group="GROUP",
        video_season=1,
        video_episode=5,
    ),
    GuessCase(
        name="Movie.Name.2020.1080p.BluRay.x264-GROUP.mkv",
        expect_type="movie",
        title="Movie Name",
        year=2020,
        release_group="GROUP",
        video_cls=Movie,
    ),
    GuessCase(
        name="Movie.Name.REPACK.2020.1080p.BluRay.x264-GROUP.mkv",
        expect_type="movie",
        title="Movie Name",
        year=2020,
        release_group="GROUP",
        video_cls=Movie,
    ),
    # Dotted absolute-looking token: 3.8 splits "1080" into season 10 / ep 80.
    GuessCase(
        name="One.Piece.1080.WEB-DL.x264-GROUP.mkv",
        expect_type="episode",
        title="One Piece",
        season=10,
        episode=80,
        release_group="GROUP",
        video_season=10,
        video_episode=80,
    ),
    # Dot absolute "150" becomes S01E50 under 3.8 (not absolute 150).
    GuessCase(
        name="Anime.Title.150.1080p.WEB.x264-SubsPlease.mkv",
        expect_type="episode",
        title="Anime Title",
        season=1,
        episode=50,
        release_group="SubsPlease",
        video_season=1,
        video_episode=50,
    ),
    # Bracket anime absolute: episode stays absolute; release_group is group tag.
    GuessCase(
        name="[SubsPlease] One Piece - 1080 (1080p) [DEADBEEF].mkv",
        expect_type="episode",
        title="One Piece",
        episode=1080,
        release_group="SubsPlease",
        video_season=1,
        video_episode=1080,
    ),
    GuessCase(
        name="[Erai-raws] Title - 12 [1080p].mkv",
        expect_type="episode",
        title="Title",
        episode=12,
        release_group="Erai-raws",
        video_season=1,
        video_episode=12,
    ),
    # Trailing [hash] wins release_group over leading [SubsPlease] in 3.8.
    GuessCase(
        name="[SubsPlease] Anime Title - 150 (1080p) [ABC123].mkv",
        expect_type="episode",
        title="Anime Title",
        episode=150,
        release_group="ABC123",
        video_season=1,
        video_episode=150,
        video_release_group="ABC123",
    ),
    # Multi-ep list in guessit; subliminal keeps the first episode only.
    GuessCase(
        name="Show.Name.S01E01E02.1080p.WEB.h264-GROUP.mkv",
        expect_type="episode",
        title="Show Name",
        season=1,
        episode=[1, 2],
        release_group="GROUP",
        video_season=1,
        video_episode=1,
    ),
)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_guessit_fields_match_3_8_baseline(case: GuessCase) -> None:
    parsed = dict(guessit(case.name))
    assert parsed.get("type") == case.expect_type
    assert parsed.get("title") == case.title
    assert parsed.get("season") == case.season
    assert parsed.get("episode") == case.episode
    assert parsed.get("year") == case.year
    assert parsed.get("release_group") == case.release_group


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_subliminal_video_fromname_matches_baseline(case: GuessCase) -> None:
    video = Video.fromname(case.name)
    assert isinstance(video, case.video_cls)

    if case.video_cls is Movie:
        assert video.title == case.title
        assert video.year == case.year
        assert getattr(video, "season", None) is None
        assert getattr(video, "episode", None) is None
    else:
        assert video.series == case.title
        assert video.season == case.video_season
        assert video.episode == case.video_episode

    expected_group = (
        case.video_release_group
        if case.video_release_group is not None
        else case.release_group
    )
    assert video.release_group == expected_group
