"""Shared Ranking Studio preset contract (FE ↔ BE).

Frontend Ranking Studio applies these packs client-side. The backend exposes the
same ids / title_similarity / enableFetch keys so golden tests and the UI stay
aligned. Presets never auto-enable Scraping soft-opt-ins.
"""

from __future__ import annotations

from typing import Any, Literal

RankingPresetId = Literal[
    "balanced",
    "webdl",
    "strict",
    "anime_dub",
    "remux_max",
    "kids_safe",
]

# Title-matching modes for Ranking Studio.
# Modes with diagnose_only=False write ranking.options.title_similarity (scrape-
# applied when saved). remake_diagnose is tester-only — live remakes use Scraping
# → enable_remake_aliases + remake_alias_groups (never the default).
TitleMatchingModeId = Literal["strict", "balanced", "aliases_friendly", "remake_diagnose"]

TITLE_MATCHING_MODES: list[dict[str, Any]] = [
    {
        "id": "strict",
        "label": "Strict",
        "title_similarity": 0.9,
        "enable_aliases": True,
        "description": (
            "Tight Levenshtein match. Writes ranking.options.title_similarity when "
            "saved — applies to live scrape. Best default when titles are stable."
        ),
        "diagnose_only": False,
        "scrape_applied": True,
    },
    {
        "id": "balanced",
        "label": "Balanced",
        "title_similarity": 0.85,
        "enable_aliases": True,
        "description": (
            "Default RTN threshold. Writes ranking.options.title_similarity when "
            "saved — applies to live scrape. Keep Scraping → enable_aliases on."
        ),
        "diagnose_only": False,
        "scrape_applied": True,
    },
    {
        "id": "aliases_friendly",
        "label": "Aliases friendly",
        "title_similarity": 0.8,
        "enable_aliases": True,
        "description": (
            "Slightly looser match for Trakt/TMDB aliases. Writes "
            "ranking.options.title_similarity when saved — applies to live scrape. "
            "For remakes, also enable Scraping → enable_remake_aliases."
        ),
        "diagnose_only": False,
        "scrape_applied": True,
    },
    {
        "id": "remake_diagnose",
        "label": "Remake diagnose",
        "title_similarity": 0.7,
        "enable_aliases": True,
        "description": (
            "Tester-only temporary threshold for remakes (e.g. Saint Seiya vs "
            "Knights of the Zodiac). Does not change live scrape by itself — for "
            "production remakes use Scraping → enable_remake_aliases + "
            "remake_alias_groups. Do not leave this low permanently."
        ),
        "diagnose_only": True,
        "scrape_applied": False,
    },
]

RANKING_PRESETS: list[dict[str, Any]] = [
    {
        "id": "balanced",
        "label": "Balanced",
        "description": "Keep common WEB-DL/BluRay codecs; reject cams and most trash.",
        "enableFetch": {
            "quality": ["avc", "hevc", "bluray", "hdtv", "web", "webdl"],
            "audio": [
                "aac",
                "atmos",
                "dolby_digital",
                "dolby_digital_plus",
                "dts_lossy",
                "truehd",
                "flac",
                "surround",
                "stereo",
            ],
            "hdr": ["hdr", "hdr10plus", "dolby_vision", "sdr", "bit10"],
            "rips": ["brrip", "hdrip", "webrip"],
            "extras": ["proper", "repack", "scene", "edition"],
            "trash": [],
        },
        "options": {
            "title_similarity": 0.85,
            "remove_all_trash": True,
            "remove_adult_content": True,
        },
        "resolutions": {"r2160p": True, "r1080p": True, "r720p": True},
        "exclude": [r"\bmatte\b"],
    },
    {
        "id": "webdl",
        "label": "WEB-DL permissive",
        "description": "Disney+/Amazon friendly — DDP/DD fetch on; remux/AV1/DV allowed.",
        "enableFetch": {
            "quality": ["avc", "hevc", "av1", "web", "webdl", "hdtv", "bluray", "remux"],
            "audio": [
                "aac",
                "atmos",
                "dolby_digital",
                "dolby_digital_plus",
                "dts_lossy",
                "surround",
                "stereo",
            ],
            "hdr": ["hdr", "hdr10plus", "dolby_vision", "sdr", "bit10"],
            "rips": ["webrip", "hdrip", "bdrip", "webdlrip", "uhdrip"],
            "extras": [
                "proper",
                "repack",
                "dubbed",
                "subbed",
                "scene",
                "site",
                "documentary",
            ],
            "trash": [],
        },
        "options": {
            "title_similarity": 0.8,
            "remove_all_trash": True,
            "remove_adult_content": True,
        },
        "resolutions": {"r2160p": True, "r1080p": True, "r720p": True},
        "exclude": [r"\bmatte\b"],
        "preferred": [r"\b4[Kk]|2160p?\b", "HDR|HDR10"],
    },
    {
        "id": "strict",
        "label": "Strict quality",
        "description": "Prefer remux / BluRay / HEVC; reject WEB-DL and dubbed.",
        "enableFetch": {
            "quality": ["hevc", "avc", "bluray", "remux"],
            "audio": ["atmos", "truehd", "dts_lossless", "flac"],
            "hdr": ["hdr", "hdr10plus", "dolby_vision", "bit10"],
            "rips": [],
            "extras": ["proper", "repack"],
            "trash": [],
        },
        "options": {
            "title_similarity": 0.9,
            "remove_all_trash": True,
            "remove_adult_content": True,
        },
        "resolutions": {"r2160p": True, "r1080p": True, "r720p": False},
        "exclude": [r"\bmatte\b", r"\bCAM\b", r"\bTS\b"],
    },
    {
        "id": "anime_dub",
        "label": "Anime Dub Friendly",
        "description": (
            "Allow dual/MULTi/dubbed + common WEB encodes. "
            "Does not change Scraping soft-opt-ins."
        ),
        "enableFetch": {
            "quality": ["avc", "hevc", "web", "webdl", "hdtv", "bluray"],
            "audio": [
                "aac",
                "flac",
                "stereo",
                "surround",
                "dolby_digital",
                "dolby_digital_plus",
            ],
            "hdr": ["sdr", "hdr", "bit10"],
            "rips": ["webrip", "hdrip"],
            "extras": [
                "dubbed",
                "subbed",
                "proper",
                "repack",
                "uncensored",
                "scene",
            ],
            "trash": [],
        },
        "options": {
            "title_similarity": 0.75,
            "remove_all_trash": True,
            "allow_english_in_languages": True,
        },
        "languages": {
            "preferred": ["anime"],
            "required": [],
            "allowed": [],
            "exclude": [],
        },
        "resolutions": {"r2160p": True, "r1080p": True, "r720p": True},
        "exclude": [r"\bmatte\b"],
        "scrapingHints": [
            {
                "path": "scraping.anime_allow_extras_dubbed",
                "label": "Anime allow extras.dubbed (soft-opt-in)",
                "recommended": True,
            },
            {
                "path": "scraping.anime_allow_multi_audio",
                "label": "Anime allow MULTI/dual-audio retry",
                "recommended": True,
            },
        ],
    },
    {
        "id": "remux_max",
        "label": "Remux Max",
        "description": "Remux / BluRay / HEVC / lossless audio first; WEB-DL off.",
        "enableFetch": {
            "quality": ["hevc", "avc", "bluray", "remux"],
            "audio": ["atmos", "truehd", "dts_lossless", "flac"],
            "hdr": ["hdr", "hdr10plus", "dolby_vision", "bit10"],
            "rips": ["bdrip", "uhdrip"],
            "extras": ["proper", "repack"],
            "trash": [],
        },
        "options": {
            "title_similarity": 0.88,
            "remove_all_trash": True,
            "remove_adult_content": True,
        },
        "resolutions": {"r2160p": True, "r1080p": True, "r720p": False},
        "preferred": [r"\bREMUX\b", r"\bBluRay\b", "HDR|HDR10"],
        "exclude": [r"\bmatte\b"],
    },
    {
        "id": "kids_safe",
        "label": "Kids Safe",
        "description": "Hard-reject trash/adult; tighter title match; no CAM/SCR.",
        "enableFetch": {
            "quality": ["avc", "hevc", "web", "webdl", "bluray", "hdtv"],
            "audio": [
                "aac",
                "stereo",
                "surround",
                "dolby_digital",
                "dolby_digital_plus",
            ],
            "hdr": ["sdr", "hdr", "bit10"],
            "rips": ["webrip", "hdrip"],
            "extras": ["proper", "repack", "dubbed", "subbed", "scene"],
            "trash": [],
        },
        "options": {
            "title_similarity": 0.9,
            "remove_all_trash": True,
            "remove_adult_content": True,
        },
        "resolutions": {
            "r2160p": False,
            "r1080p": True,
            "r720p": True,
            "r480p": False,
        },
        "exclude": [r"\bxxx\b", r"\bporn\b", r"\bmatte\b", r"\bCAM\b"],
    },
]

# Golden titles shared with Ranking Studio P0/P1 tests.
GOLDEN_TITLES: dict[str, str] = {
    "webdl_ddp": "The.Movie.2024.2160p.WEB-DL.DDP5.1.Atmos.H.265-GROUP",
    "anime_multi": "Dragon Ball Z Resurrection F 2015 MULTi TRUEFRENCH 1080p BluRay x264",
    "remux": "The.Movie.2024.2160p.BluRay.REMUX.HEVC.TrueHD.Atmos-GROUP",
    "cam": "The.Movie.2024.CAM.XviD-GROUP",
    "matte": "The.Movie.2024.1080p.BluRay.matte.x264-GROUP",
    "title_mismatch_remake": (
        "Knights.of.the.Zodiac.Saint.Seiya.2023.1080p.WEB-DL.DDP5.1.H.264-GROUP"
    ),
}


def preset_by_id(preset_id: str) -> dict[str, Any] | None:
    for preset in RANKING_PRESETS:
        if preset["id"] == preset_id:
            return preset
    return None


def matching_mode_by_id(mode_id: str) -> dict[str, Any] | None:
    for mode in TITLE_MATCHING_MODES:
        if mode["id"] == mode_id:
            return mode
    return None
