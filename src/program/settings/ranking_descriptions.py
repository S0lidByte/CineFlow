"""Human-readable descriptions for RTN ranking schema fields.

RTN's SettingsModel ships without Field(description=...). Logs report rejects as
`denied by: {category}_{attribute}` (e.g. audio_dolby_digital_plus). These strings
are injected into the settings JSON schema so the UI can explain that mapping.
"""

from __future__ import annotations

from typing import Any

# Log deny key → short explanation (also used as property description text).
DENY_KEY_HELP: dict[str, str] = {
    # audio
    "audio_aac": "AAC audio. Log: denied by: audio_aac when fetch is off.",
    "audio_atmos": "Dolby Atmos. Log: denied by: audio_atmos when fetch is off.",
    "audio_dolby_digital": "Dolby Digital / AC3 / DD. Log: denied by: audio_dolby_digital. Common on WEB-DL — leave fetch on to keep those releases.",
    "audio_dolby_digital_plus": "Dolby Digital Plus / DDP / DD+. Log: denied by: audio_dolby_digital_plus. Typical for Disney+/Amazon WEB-DL — leave fetch on if you want those.",
    "audio_dts_lossy": "Lossy DTS. Log: denied by: audio_dts_lossy when fetch is off.",
    "audio_dts_lossless": "Lossless DTS-HD. Log: denied by: audio_dts_lossless when fetch is off.",
    "audio_flac": "FLAC audio. Log: denied by: audio_flac when fetch is off.",
    "audio_mono": "Mono audio. Log: denied by: audio_mono when fetch is off.",
    "audio_mp3": "MP3 audio. Log: denied by: audio_mp3 when fetch is off.",
    "audio_stereo": "Stereo audio. Log: denied by: audio_stereo when fetch is off.",
    "audio_surround": "Surround (generic). Log: denied by: audio_surround when fetch is off.",
    "audio_truehd": "TrueHD. Log: denied by: audio_truehd when fetch is off.",
    # quality
    "quality_av1": "AV1 codec. Log: denied by: quality_av1 / codec_av1 when fetch is off.",
    "quality_avc": "AVC / H.264. Log: denied by: quality_avc when fetch is off.",
    "quality_bluray": "BluRay source tag. Log: denied by: quality_bluray when fetch is off.",
    "quality_dvd": "DVD source. Log: denied by: quality_dvd when fetch is off.",
    "quality_hdtv": "HDTV source. Log: denied by: quality_hdtv when fetch is off.",
    "quality_hevc": "HEVC / H.265. Log: denied by: quality_hevc when fetch is off.",
    "quality_mpeg": "MPEG codec. Log: denied by: quality_mpeg when fetch is off.",
    "quality_remux": "Remux releases. Log: denied by: quality_remux when fetch is off.",
    "quality_vhs": "VHS source. Log: denied by: quality_vhs when fetch is off.",
    "quality_web": "WEB (generic). Log: denied by: quality_web when fetch is off.",
    "quality_webdl": "WEB-DL. Log: denied by: quality_webdl when fetch is off.",
    "quality_webmux": "WEBMux. Log: denied by: quality_webmux when fetch is off.",
    "quality_xvid": "XviD codec. Log: denied by: codec_xvid / quality_xvid when fetch is off.",
    # hdr
    "hdr_bit10": "10-bit video. Log: denied by: hdr_bit10 when fetch is off.",
    "hdr_dolby_vision": "Dolby Vision. Log: denied by: hdr_dolby_vision when fetch is off.",
    "hdr_hdr": "HDR. Log: denied by: hdr_hdr when fetch is off.",
    "hdr_hdr10plus": "HDR10+. Log: denied by: hdr_hdr10plus when fetch is off.",
    "hdr_sdr": "SDR. Log: denied by: hdr_sdr when fetch is off.",
    # rips
    "rips_bdrip": "BDRip. Log: denied by: rips_bdrip when fetch is off.",
    "rips_brrip": "BRRip. Log: denied by: rips_brrip when fetch is off.",
    "rips_dvdrip": "DVDRip. Log: denied by: rips_dvdrip when fetch is off.",
    "rips_hdrip": "HDRip. Log: denied by: rips_hdrip when fetch is off.",
    "rips_ppvrip": "PPVRip. Log: denied by: rips_ppvrip when fetch is off.",
    "rips_satrip": "SATRip. Log: denied by: rips_satrip when fetch is off.",
    "rips_tvrip": "TVRip. Log: denied by: rips_tvrip when fetch is off.",
    "rips_uhdrip": "UHDRip. Log: denied by: rips_uhdrip when fetch is off.",
    "rips_vhsrip": "VHSRip. Log: denied by: rips_vhsrip when fetch is off.",
    "rips_webdlrip": "WEBDLRip. Log: denied by: rips_webdlrip when fetch is off.",
    "rips_webrip": "WEBRip. Log: denied by: rips_webrip when fetch is off.",
    # extras
    "extras_three_d": "3D releases. Log: denied by: extras_three_d when fetch is off.",
    "extras_converted": "Converted flag. Log: denied by: extras_converted when fetch is off.",
    "extras_documentary": "Documentary edition. Log: denied by: extras_documentary when fetch is off.",
    "extras_dubbed": "Dubbed / MULTi / Dual Audio. Log: denied by: extras_dubbed when fetch is off.",
    "extras_edition": "Special edition tags. Log: denied by: extras_edition when fetch is off.",
    "extras_hardcoded": "Hardcoded subs. Log: denied by: extras_hardcoded when fetch is off.",
    "extras_network": "Network tag. Log: denied by: extras_network when fetch is off.",
    "extras_proper": "PROPER. Log: denied by: extras_proper when fetch is off.",
    "extras_repack": "REPACK. Log: denied by: extras_repack when fetch is off.",
    "extras_retail": "Retail. Log: denied by: extras_retail when fetch is off.",
    "extras_site": "Site / YTS / RARBG-style tags. Log: denied by: extras_site when fetch is off.",
    "extras_subbed": "Subbed. Log: denied by: extras_subbed when fetch is off.",
    "extras_upscaled": "Upscaled. Log: denied by: extras_upscaled when fetch is off.",
    "extras_scene": "Scene release. Log: denied by: extras_scene when fetch is off.",
    "extras_uncensored": "Uncensored. Log: denied by: extras_uncensored when fetch is off.",
    # trash
    "trash_cam": "CAM. Log: denied by: trash_cam when fetch is off.",
    "trash_clean_audio": "Clean audio trash flag. Log: denied by: trash_clean_audio when fetch is off.",
    "trash_pdtv": "PDTV. Log: denied by: trash_pdtv when fetch is off.",
    "trash_r5": "R5. Log: denied by: trash_r5 when fetch is off.",
    "trash_screener": "Screener / SCR. Log: denied by: trash_quality / trash_screener when fetch is off.",
    "trash_size": "Abnormally small size. Log: denied by: trash_size when fetch is off.",
    "trash_telecine": "Telecine. Log: denied by: trash_telecine when fetch is off.",
    "trash_telesync": "Telesync. Log: denied by: trash_telesync when fetch is off.",
}

ATTRIBUTE_TITLES: dict[str, str] = {
    "aac": "AAC",
    "atmos": "Dolby Atmos",
    "dolby_digital": "Dolby Digital (DD/AC3)",
    "dolby_digital_plus": "Dolby Digital Plus (DDP)",
    "dts_lossy": "DTS (lossy)",
    "dts_lossless": "DTS-HD (lossless)",
    "flac": "FLAC",
    "mono": "Mono",
    "mp3": "MP3",
    "stereo": "Stereo",
    "surround": "Surround",
    "truehd": "TrueHD",
    "av1": "AV1",
    "avc": "AVC / H.264",
    "bluray": "BluRay",
    "dvd": "DVD",
    "hdtv": "HDTV",
    "hevc": "HEVC / H.265",
    "mpeg": "MPEG",
    "remux": "Remux",
    "vhs": "VHS",
    "web": "WEB",
    "webdl": "WEB-DL",
    "webmux": "WEBMux",
    "xvid": "XviD",
    "bit10": "10-bit",
    "dolby_vision": "Dolby Vision",
    "hdr": "HDR",
    "hdr10plus": "HDR10+",
    "sdr": "SDR",
    "bdrip": "BDRip",
    "brrip": "BRRip",
    "dvdrip": "DVDRip",
    "hdrip": "HDRip",
    "ppvrip": "PPVRip",
    "satrip": "SATRip",
    "tvrip": "TVRip",
    "uhdrip": "UHDRip",
    "vhsrip": "VHSRip",
    "webdlrip": "WEBDLRip",
    "webrip": "WEBRip",
    "three_d": "3D",
    "converted": "Converted",
    "documentary": "Documentary",
    "dubbed": "Dubbed / Dual / MULTi",
    "edition": "Edition",
    "hardcoded": "Hardcoded subs",
    "network": "Network",
    "proper": "PROPER",
    "repack": "REPACK",
    "retail": "Retail",
    "site": "Site tags (YTS, RARBG, …)",
    "subbed": "Subbed",
    "upscaled": "Upscaled",
    "scene": "Scene",
    "uncensored": "Uncensored",
    "cam": "CAM",
    "clean_audio": "Clean audio",
    "pdtv": "PDTV",
    "r5": "R5",
    "screener": "Screener",
    "size": "Trash size",
    "telecine": "Telecine",
    "telesync": "Telesync",
}

CATEGORY_HELP: dict[str, str] = {
    "quality": "Source/codec quality ranks. Rejects appear as denied by: quality_<name>.",
    "rips": "Rip-type filters. Rejects appear as denied by: rips_<name>.",
    "hdr": "HDR / bit-depth filters. Rejects appear as denied by: hdr_<name>.",
    "audio": "Audio codec filters. Rejects appear as denied by: audio_<name> (e.g. audio_dolby_digital_plus).",
    "extras": "Extras / release flags. Rejects appear as denied by: extras_<name>.",
    "trash": "Low-quality trash filters. Rejects appear as denied by: trash_<name>.",
}

OPTIONS_HELP: dict[str, str] = {
    "title_similarity": "Minimum title similarity (Levenshtein) required to accept a release.",
    "remove_all_trash": "When true, apply trash custom_ranks fetch=false rules and other trash heuristics.",
    "remove_ranks_under": "Drop torrents whose computed rank is below this threshold (log: does not meet the minimum rank requirement).",
    "remove_unknown_languages": "Reject releases with unrecognized language tags.",
    "allow_english_in_languages": "Treat untagged releases as English when English is required.",
    "enable_fetch_speed_mode": "Prefer faster fetch decisions over exhaustive ranking.",
    "remove_adult_content": "Reject adult-flagged releases.",
}

CUSTOM_RANK_FIELD_HELP: dict[str, str] = {
    "fetch": (
        "Allow this attribute. When false, matching torrents are rejected "
        "(DEBUG log: denied by: <category>_<attribute>)."
    ),
    "use_custom_rank": "Override the default rank score with the rank value below.",
    "rank": "Custom rank score used when use_custom_rank is enabled (higher is better).",
}

LANGUAGES_HELP: dict[str, str] = {
    "required": "Languages that must be present (ISO codes). Missing required language rejects the torrent.",
    "exclude": "Languages to reject. Cyrillic/localized packs often surface as lang_ru or similar in logs.",
    "preferred": "Preferred languages for ranking boosts.",
    "allowed": "Optional allow-list of languages.",
}

RESOLUTION_HELP: dict[str, str] = {
    "r2160p": "Allow 2160p / 4K. Log may say denied by: resolution when disabled.",
    "r1080p": "Allow 1080p.",
    "r720p": "Allow 720p.",
    "r480p": "Allow 480p.",
    "r360p": "Allow 360p.",
    "unknown": "Allow unknown / unparsed resolution.",
}

DEF_ATTRIBUTE_MAP: dict[str, str] = {
    "AudioRankModel": "audio",
    "QualityRankModel": "quality",
    "HdrRankModel": "hdr",
    "RipsRankModel": "rips",
    "ExtrasRankModel": "extras",
    "TrashRankModel": "trash",
}


def _set_desc(node: dict[str, Any], text: str) -> None:
    if text and not node.get("description"):
        node["description"] = text


def _enrich_custom_rank_def(defn: dict[str, Any]) -> None:
    props = defn.get("properties")
    if not isinstance(props, dict):
        return
    for key, help_text in CUSTOM_RANK_FIELD_HELP.items():
        prop = props.get(key)
        if isinstance(prop, dict):
            _set_desc(prop, help_text)


def _enrich_rank_model_def(defn: dict[str, Any], category: str) -> None:
    props = defn.get("properties")
    if not isinstance(props, dict):
        return
    for attr, prop in props.items():
        if not isinstance(prop, dict):
            continue
        deny_key = f"{category}_{attr}"
        title = ATTRIBUTE_TITLES.get(attr, attr.replace("_", " ").title())
        help_text = DENY_KEY_HELP.get(
            deny_key,
            f"{title}. When fetch is false, RTN rejects with denied by: {deny_key}.",
        )
        if "title" not in prop:
            prop["title"] = title
        _set_desc(prop, help_text)


def _enrich_named_props(defn: dict[str, Any], help_map: dict[str, str]) -> None:
    props = defn.get("properties")
    if not isinstance(props, dict):
        return
    for key, help_text in help_map.items():
        prop = props.get(key)
        if isinstance(prop, dict):
            _set_desc(prop, help_text)


def enrich_ranking_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Mutate (and return) a settings schema fragment to add ranking field docs."""
    ranking = schema.get("properties", {}).get("ranking")
    if isinstance(ranking, dict):
        _set_desc(
            ranking,
            "RTN result ranking and trash filters. Reject reasons in logs map to "
            "custom_ranks.<category>.<attribute> (denied by: <category>_<attribute>).",
        )

    defs = schema.get("$defs")
    if not isinstance(defs, dict):
        return schema

    for def_name, defn in defs.items():
        if not isinstance(defn, dict):
            continue
        if def_name == "CustomRank":
            _enrich_custom_rank_def(defn)
        elif def_name == "CustomRanksConfig":
            _enrich_named_props(defn, CATEGORY_HELP)
        elif def_name == "OptionsConfig":
            _enrich_named_props(defn, OPTIONS_HELP)
        elif def_name == "LanguagesConfig":
            _enrich_named_props(defn, LANGUAGES_HELP)
        elif def_name == "ResolutionConfig":
            _enrich_named_props(defn, RESOLUTION_HELP)
        elif def_name in DEF_ATTRIBUTE_MAP:
            _enrich_rank_model_def(defn, DEF_ATTRIBUTE_MAP[def_name])

    return schema
