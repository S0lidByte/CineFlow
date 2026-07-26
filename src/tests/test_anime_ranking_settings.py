"""Independent anime ranking pack (ranking_anime) characterization tests."""

from __future__ import annotations

from program.services.scrapers.shared import _prepare_rtn_ranking_context
from program.settings import settings_manager
from program.settings.ranking_presets import (
    apply_ranking_preset,
    default_anime_rtn_settings,
)
from RTN.models import SettingsModel


class AnimeItem:
    top_title = "Dragon Ball Z: Resurrection F"
    log_string = "Dragon Ball Z: Resurrection F"
    country = None
    is_anime = True
    aired_at = None

    @staticmethod
    def get_aliases():
        return {}


class NonAnimeItem:
    top_title = "The Movie"
    log_string = "The Movie"
    country = None
    is_anime = False
    aired_at = None

    @staticmethod
    def get_aliases():
        return {}


def test_anime_dub_preset_enables_dubbed_and_lowers_title_similarity():
    applied = apply_ranking_preset(SettingsModel(), "anime_dub")

    assert applied.custom_ranks.extras.dubbed.fetch is True
    assert applied.custom_ranks.extras.subbed.fetch is True
    assert applied.options.title_similarity == 0.75
    assert applied.languages.preferred == ["anime"]


def test_default_anime_rtn_settings_matches_anime_dub_preset():
    defaults = default_anime_rtn_settings()
    applied = apply_ranking_preset(SettingsModel(), "anime_dub")

    assert defaults.model_dump() == applied.model_dump()


def test_app_model_ranking_anime_defaults_to_anime_dub():
    anime = settings_manager.settings.ranking_anime
    expected = default_anime_rtn_settings()

    assert anime.custom_ranks.extras.dubbed.fetch is True
    assert anime.options.title_similarity == expected.options.title_similarity


def test_effective_rtn_model_selects_anime_pack_independently():
    movie_pack = settings_manager.get_effective_rtn_model(for_anime=False)
    anime_pack = settings_manager.get_effective_rtn_model(for_anime=True)

    assert movie_pack.model_dump() == settings_manager.settings.ranking.model_dump()
    assert anime_pack.model_dump() == settings_manager.settings.ranking_anime.model_dump()
    assert anime_pack.custom_ranks.extras.dubbed.fetch is True


def test_prepare_context_uses_ranking_anime_for_anime_items():
    _rtn, active, _title, _aliases = _prepare_rtn_ranking_context(AnimeItem())  # type: ignore[arg-type]

    assert active.options.title_similarity == (
        settings_manager.settings.ranking_anime.options.title_similarity
    )
    assert active.custom_ranks.extras.dubbed.fetch is True


def test_prepare_context_uses_ranking_for_non_anime_items():
    _rtn, active, _title, _aliases = _prepare_rtn_ranking_context(NonAnimeItem())  # type: ignore[arg-type]

    assert (
        active.options.title_similarity
        == settings_manager.settings.ranking.options.title_similarity
    )
    assert active.model_dump() == settings_manager.settings.ranking.model_dump()


def test_anime_and_movie_ranking_are_independently_mutable():
    ranking = settings_manager.settings.ranking
    ranking_anime = settings_manager.settings.ranking_anime
    prev_movie_sim = ranking.options.title_similarity
    prev_anime_sim = ranking_anime.options.title_similarity

    try:
        ranking.options.title_similarity = 0.99
        ranking_anime.options.title_similarity = 0.55

        movie = settings_manager.get_effective_rtn_model(for_anime=False)
        anime = settings_manager.get_effective_rtn_model(for_anime=True)

        assert movie.options.title_similarity == 0.99
        assert anime.options.title_similarity == 0.55
    finally:
        ranking.options.title_similarity = prev_movie_sim
        ranking_anime.options.title_similarity = prev_anime_sim
