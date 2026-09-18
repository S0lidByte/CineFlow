"""Tests for multi-season scraper year tolerance and title alias resolution."""

from datetime import datetime
from unittest.mock import MagicMock

from RTN import ParsedData

from program.media.item import Episode, MediaItem, Movie, Season, Show
from program.services.scrapers.shared import (
    _check_item_year,
    _extract_year,
    _resolve_scrape_aliases,
    get_year_candidates,
    parse_results,
)
from program.settings import settings_manager


def test_get_year_candidates():
    """Verify year candidate set formula (year - 1, year, year + 1)."""
    assert get_year_candidates(2020) == [2019, 2020, 2021]
    assert get_year_candidates(1999) == [1998, 1999, 2000]


def test_extract_year_various_types():
    """Verify year extraction across datetime, MediaItem, int, and missing."""
    assert _extract_year(datetime(2015, 6, 1)) == 2015

    movie = Movie({"title": "Inception", "year": 2010, "aired_at": datetime(2010, 7, 16)})
    assert _extract_year(movie) == 2010

    show = Show({"title": "Breaking Bad", "aired_at": datetime(2008, 1, 20)})
    assert _extract_year(show) == 2008

    assert _extract_year(None) is None
    assert _extract_year("invalid") is None


def test_check_item_year_movie():
    """Verify movie year tolerance checks movie's own year +- 1."""
    movie = Movie({"title": "Dune", "year": 2021, "aired_at": datetime(2021, 10, 22)})

    # Valid matches (2020, 2021, 2022)
    assert _check_item_year(movie, ParsedData(raw_title="", year=2020)) is True
    assert _check_item_year(movie, ParsedData(raw_title="", year=2021)) is True
    assert _check_item_year(movie, ParsedData(raw_title="", year=2022)) is True

    # Invalid matches (< 2020 or > 2022)
    assert _check_item_year(movie, ParsedData(raw_title="", year=2019)) is False
    assert _check_item_year(movie, ParsedData(raw_title="", year=2023)) is False

    # Torrent without year metadata is not rejected
    assert _check_item_year(movie, ParsedData(raw_title="", year=0)) is True


def test_check_item_year_multi_season_show():
    """
    Verify multi-season tolerance allows releases tagged with:
    1. The root show's premiere year (+- 1)
    2. The season's own air year (+- 1)
    """
    # Show premiered in 2008 (Breaking Bad)
    show = Show({"title": "Breaking Bad", "year": 2008, "aired_at": datetime(2008, 1, 20)})

    # Season 4 aired in 2011
    season4 = Season({"number": 4, "year": 2011, "aired_at": datetime(2011, 7, 17)})
    season4.parent = show

    # Candidates: {2007, 2008, 2009} (show) ∪ {2010, 2011, 2012} (season)
    # 1. Tagged with show premiere year (e.g. Breaking.Bad.2008.S04.1080p)
    assert _check_item_year(season4, ParsedData(raw_title="", year=2008)) is True
    assert _check_item_year(season4, ParsedData(raw_title="", year=2007)) is True
    assert _check_item_year(season4, ParsedData(raw_title="", year=2009)) is True

    # 2. Tagged with season air year (e.g. Breaking.Bad.2011.S04.1080p)
    assert _check_item_year(season4, ParsedData(raw_title="", year=2011)) is True
    assert _check_item_year(season4, ParsedData(raw_title="", year=2010)) is True
    assert _check_item_year(season4, ParsedData(raw_title="", year=2012)) is True

    # 3. Tagged with an unrelated year (e.g. 2015 or 2005) -> Rejected
    assert _check_item_year(season4, ParsedData(raw_title="", year=2015)) is False
    assert _check_item_year(season4, ParsedData(raw_title="", year=2005)) is False


def test_check_item_year_episode():
    """Verify episode checks both episode/season air year and root show premiere year."""
    show = Show({"title": "Better Call Saul", "year": 2015, "aired_at": datetime(2015, 2, 8)})
    season3 = Season({"number": 3, "year": 2017, "aired_at": datetime(2017, 4, 10)})
    season3.parent = show
    episode5 = Episode({"number": 5, "year": 2017, "aired_at": datetime(2017, 5, 8)})
    episode5.parent = season3

    # Candidate years: {2014, 2015, 2016} (show) ∪ {2016, 2017, 2018} (episode)
    assert _check_item_year(episode5, ParsedData(raw_title="", year=2015)) is True
    assert _check_item_year(episode5, ParsedData(raw_title="", year=2017)) is True
    assert _check_item_year(episode5, ParsedData(raw_title="", year=2014)) is True
    assert _check_item_year(episode5, ParsedData(raw_title="", year=2018)) is True
    assert _check_item_year(episode5, ParsedData(raw_title="", year=2021)) is False


def test_check_item_year_missing_metadata():
    """Verify missing year metadata on item does not reject torrents."""
    item = MediaItem({"title": "Unknown Media"})
    assert _check_item_year(item, ParsedData(raw_title="", year=2022)) is True


def test_resolve_scrape_aliases_synthesizes_variants():
    """Verify _resolve_scrape_aliases injects synthesized title variants into xx aliases."""
    show = Show({"title": "Marvel's Agents of S.H.I.E.L.D.", "aliases": {}})
    active_settings = settings_manager.get_effective_rtn_model(for_anime=False)

    aliases = _resolve_scrape_aliases(show, active_settings)
    xx_aliases = aliases.get("xx", [])

    assert any("SHIELD" in a for a in xx_aliases)
    assert any("Marvels Agents of S H I E L D" in a or "Marvels Agents of SHIELD" in a for a in xx_aliases)


def test_parse_results_with_multi_season_year_tolerance():
    """Verify end-to-end parse_results accepts a season torrent tagged with the root show's premiere year."""
    show = Show({"title": "Breaking Bad", "year": 2008, "aired_at": datetime(2008, 1, 20)})
    season4 = Season({"number": 4, "year": 2011, "aired_at": datetime(2011, 7, 17)})
    season4.parent = show

    # Torrent named with show premiere year (2008) and S04
    infohash = "a" * 40
    raw_title = "Breaking.Bad.2008.S04.1080p.BluRay.x264-ROVERS"
    results = {infohash: raw_title}

    streams = parse_results(season4, results)
    assert len(streams) == 1
    assert infohash in streams
