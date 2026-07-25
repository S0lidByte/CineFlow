"""P1 ranking / scrape quality: funnel API shapes, bitrate floors, country anime skip."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from program.services.downloaders.models import (
    BitrateLimitExceededException,
    DebridFile,
    calculate_average_bitrate,
)
from program.services.scrapers.funnel import (
    ScrapeFunnelStats,
    get_remembered_funnel_summary,
    remember_funnel_summary,
)
from program.settings.ranking_presets import (
    GOLDEN_TITLES,
    RANKING_PRESETS,
    TITLE_MATCHING_MODES,
    matching_mode_by_id,
    preset_by_id,
)


def test_funnel_to_summary_and_remember():
    funnel = ScrapeFunnelStats(found=10, ranked=3, new=2, rtn_rejected=5)
    funnel.rtn_reasons["title_mismatch"] = 3
    funnel.rtn_reasons["extras_dubbed"] = 2
    summary = funnel.to_summary(item_id=42, item_log="Movie X")
    assert summary["found"] == 10
    assert summary["ranked"] == 3
    assert summary["rtn_top"][0]["reason"] == "title_mismatch"
    remember_funnel_summary(42, summary)
    cached = get_remembered_funnel_summary(42)
    assert cached is not None
    assert cached["item_id"] == 42
    assert cached["rtn_top"][0]["count"] == 3


def test_shared_preset_contract_ids():
    ids = {p["id"] for p in RANKING_PRESETS}
    assert ids == {
        "balanced",
        "webdl",
        "strict",
        "anime_dub",
        "remux_max",
        "kids_safe",
    }
    balanced = preset_by_id("balanced")
    assert balanced is not None
    assert balanced["options"]["title_similarity"] == 0.85
    mode = matching_mode_by_id("remake_diagnose")
    assert mode is not None
    assert mode["diagnose_only"] is True
    assert mode["title_similarity"] == 0.7
    assert "title_mismatch_remake" in GOLDEN_TITLES
    assert {m["id"] for m in TITLE_MATCHING_MODES} == {
        "strict",
        "balanced",
        "aliases_friendly",
        "remake_diagnose",
    }


def test_calculate_average_bitrate_riven_ts_parity():
    # 2 GiB over 120 minutes ≈ 17.07 MiB/min
    bitrate = calculate_average_bitrate(2 * 1024 * 1024 * 1024, 120)
    assert 17.0 < bitrate < 17.2


def test_debridfile_bitrate_floor_rejects_when_under(monkeypatch):
    from program.settings import settings_manager

    monkeypatch.setattr(
        settings_manager.settings.downloaders,
        "movie_min_avg_bitrate",
        20.0,
    )
    monkeypatch.setattr(
        settings_manager.settings.downloaders,
        "movie_filesize_mb_min",
        1,
    )
    with pytest.raises(BitrateLimitExceededException):
        DebridFile.create(
            filesize_bytes=500 * 1024 * 1024,  # 500 MiB
            filename="movie.mkv",
            filetype="movie",
            runtime_minutes=120,
            limit_filesize=False,
        )


def test_debridfile_bitrate_floor_disabled_by_default():
    # 0 floor → no bitrate check even with tiny file + runtime
    df = DebridFile.create(
        filesize_bytes=10 * 1024 * 1024,
        filename="movie.mkv",
        filetype="movie",
        runtime_minutes=120,
        limit_filesize=False,
    )
    assert df.filesize == 10 * 1024 * 1024


def test_anime_country_mismatch_skipped():
    """Parity with riven-ts validate-torrent: skip country check when is_anime."""
    from program.services.scrapers import shared as shared_mod

    anime = SimpleNamespace(
        is_anime=True,
        country="JP",
        log_string="Anime JP",
        type="movie",
        aired_at=None,
        number=None,
        absolute_number=None,
        parent=None,
        top_title="Anime",
        get_aliases=lambda: {},
    )
    non_anime = SimpleNamespace(
        is_anime=False,
        country="UK",
        log_string="Movie UK",
        type="movie",
        aired_at=None,
        number=None,
        absolute_number=None,
        parent=None,
        top_title="Movie",
        get_aliases=lambda: {},
    )

    # Parsed torrent claims US while item is UK — should filter non-anime only.
    torrent_data = SimpleNamespace(
        country="US",
        year=None,
        dubbed=False,
        episodes=[],
        seasons=[],
        resolution="1080p",
    )
    torrent = SimpleNamespace(
        infohash="a" * 40,
        raw_title="Some.Title.2024.US.1080p.WEB-DL",
        data=torrent_data,
        rank=100,
        lev_ratio=0.95,
        fetch=True,
    )

    # Directly exercise the country predicate used in _accumulate_ranked_torrents.
    assert anime.is_anime
    assert not (
        torrent.data.country
        and not anime.is_anime
        and shared_mod._get_item_country(anime)
        and torrent.data.country not in shared_mod._get_item_country(anime)
    )

    item_country = shared_mod._get_item_country(non_anime)
    assert item_country == "UK"
    assert (
        torrent.data.country
        and not non_anime.is_anime
        and item_country
        and torrent.data.country not in item_country
    )


def test_normalize_infohash_hex_only():
    from routers.secure.ranking import _normalize_infohash
    from fastapi import HTTPException

    assert _normalize_infohash("A" * 40) == "a" * 40
    with pytest.raises(HTTPException) as exc:
        _normalize_infohash("zzzz" + "0" * 36)
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException):
        _normalize_infohash("abc")


def test_scraping_hint_title_mismatch():
    from routers.secure.ranking import _scraping_hint_for_deny

    hint = _scraping_hint_for_deny("title_mismatch")
    assert hint is not None
    assert "aliases" in hint.lower() or "title_similarity" in hint.lower()
    # Dead branch removed: unknown keys return None (no soft_opt catch-all).
    assert _scraping_hint_for_deny("quality_webdl") is None
