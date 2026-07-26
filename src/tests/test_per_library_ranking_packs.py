"""Per-library ranking_pack binding on filesystem.library_profiles."""

from __future__ import annotations

from program.services.scrapers.shared import (
    _prepare_rtn_ranking_context,
    item_uses_anime_ranking,
    resolve_ranking_pack,
)
from program.settings import settings_manager
from program.settings.models import LibraryProfile, LibraryProfileFilterRules


class _StubItem:
    """Minimal MediaItem stand-in for ranking pack resolution."""

    def __init__(
        self,
        *,
        title: str,
        is_anime: bool,
        item_type: str = "movie",
        genres: list[str] | None = None,
    ):
        self.top_title = title
        self.log_string = title
        self.is_anime = is_anime
        self.type = item_type
        self.genres = genres or []
        self.country = None
        self.aired_at = None
        self.year = 2020
        self.rating = None
        self.content_rating = None
        self.network = None
        self.language = None

    def get_aliases(self):
        return {}


def _swap_profiles(profiles: dict[str, LibraryProfile]):
    fs = settings_manager.settings.filesystem
    previous = fs.library_profiles
    fs.library_profiles = profiles
    return previous


def test_resolve_falls_back_to_is_anime_when_no_pack_bound():
    previous = _swap_profiles(
        {
            "anime": LibraryProfile(
                name="Anime",
                library_path="/anime",
                enabled=True,
                ranking_pack=None,
                filter_rules=LibraryProfileFilterRules(is_anime=True),
            )
        }
    )
    try:
        anime = _StubItem(title="One Piece", is_anime=True)
        movie = _StubItem(title="Inception", is_anime=False)
        assert resolve_ranking_pack(anime) == "ranking_anime"
        assert resolve_ranking_pack(movie) == "ranking"
        assert item_uses_anime_ranking(anime) is True
        assert item_uses_anime_ranking(movie) is False
    finally:
        settings_manager.settings.filesystem.library_profiles = previous


def test_matching_profile_ranking_pack_overrides_is_anime():
    """Kids-style profile can force Movies pack even for anime-flagged items."""
    previous = _swap_profiles(
        {
            "kids": LibraryProfile(
                name="Kids",
                library_path="/kids",
                enabled=True,
                ranking_pack="ranking",
                filter_rules=LibraryProfileFilterRules(
                    genres=["animation", "family"],
                ),
            ),
            "anime": LibraryProfile(
                name="Anime",
                library_path="/anime",
                enabled=True,
                ranking_pack="ranking_anime",
                filter_rules=LibraryProfileFilterRules(is_anime=True),
            ),
        }
    )
    try:
        # Matches kids first (settings order) via genre — pack forces movies.
        kid_anime = _StubItem(
            title="Kids Anime Movie",
            is_anime=True,
            genres=["animation", "family"],
        )
        assert resolve_ranking_pack(kid_anime) == "ranking"
        assert item_uses_anime_ranking(kid_anime) is False

        # Pure anime with no kids genres → anime pack from anime profile.
        pure_anime = _StubItem(
            title="Attack on Titan",
            is_anime=True,
            genres=["action"],
        )
        assert resolve_ranking_pack(pure_anime) == "ranking_anime"
    finally:
        settings_manager.settings.filesystem.library_profiles = previous


def test_first_matching_profile_with_pack_wins():
    previous = _swap_profiles(
        {
            "a": LibraryProfile(
                name="A",
                library_path="/a",
                enabled=True,
                ranking_pack=None,
                filter_rules=LibraryProfileFilterRules(content_types=["movie"]),
            ),
            "b": LibraryProfile(
                name="B",
                library_path="/b",
                enabled=True,
                ranking_pack="ranking_anime",
                filter_rules=LibraryProfileFilterRules(content_types=["movie"]),
            ),
            "c": LibraryProfile(
                name="C",
                library_path="/c",
                enabled=True,
                ranking_pack="ranking",
                filter_rules=LibraryProfileFilterRules(content_types=["movie"]),
            ),
        }
    )
    try:
        item = _StubItem(title="Movie", is_anime=False, item_type="movie")
        # a matches but has no pack; b is first with pack set.
        assert resolve_ranking_pack(item) == "ranking_anime"
    finally:
        settings_manager.settings.filesystem.library_profiles = previous


def test_prepare_context_uses_library_profile_pack():
    ranking = settings_manager.settings.ranking
    ranking_anime = settings_manager.settings.ranking_anime
    prev_movie_sim = ranking.options.title_similarity
    prev_anime_sim = ranking_anime.options.title_similarity
    previous = _swap_profiles(
        {
            "force_anime": LibraryProfile(
                name="Force Anime Pack",
                library_path="/force-anime",
                enabled=True,
                ranking_pack="ranking_anime",
                filter_rules=LibraryProfileFilterRules(content_types=["movie"]),
            )
        }
    )
    try:
        ranking.options.title_similarity = 0.91
        ranking_anime.options.title_similarity = 0.42
        item = _StubItem(title="Non-anime forced", is_anime=False, item_type="movie")
        _rtn, active, _title, _aliases = _prepare_rtn_ranking_context(item)  # type: ignore[arg-type]
        assert active.options.title_similarity == 0.42
    finally:
        ranking.options.title_similarity = prev_movie_sim
        ranking_anime.options.title_similarity = prev_anime_sim
        settings_manager.settings.filesystem.library_profiles = previous


def test_filesystem_default_anime_profile_binds_ranking_anime_pack():
    from program.settings.models import FilesystemModel

    fs = FilesystemModel()
    anime_profile = fs.library_profiles["anime"]
    assert anime_profile.ranking_pack == "ranking_anime"
