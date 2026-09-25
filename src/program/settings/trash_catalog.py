"""TRaSH Guides official custom format rule definitions and profile presets.

Standardized against TRaSH Guides (https://trash-guides.info/) specifications for
Sonarr, Radarr, and next-generation media orchestrators.
"""

from __future__ import annotations

from program.settings.trash_models import (
    TrashCondition,
    TrashCustomFormat,
    TrashProfile,
)


def get_default_trash_custom_formats() -> list[TrashCustomFormat]:
    """Return the complete catalog of default TRaSH Custom Formats."""
    return [
        # --- HDR / Dolby Vision ---
        TrashCustomFormat(
            trash_id="dv-hdr10-fallback",
            name="Dolby Vision with HDR10 Fallback",
            category="hdr_dv",
            description="Dolby Vision release that includes standard HDR10/HDR10+ fallback layers (Profiles 7/8)",
            default_score=800,
            score=800,
            enabled=True,
            conditions=[
                TrashCondition(
                    name="Has DV and HDR tags",
                    pattern=r"\b(?:DV|Dolby[ .-]Vision|DoVi)\b.*\b(?:HDR10(?:\+)?|HDR)\b|\b(?:HDR10(?:\+)?|HDR)\b.*\b(?:DV|Dolby[ .-]Vision|DoVi)\b",
                    required=True,
                ),
            ],
        ),
        TrashCustomFormat(
            trash_id="dv-no-fallback",
            name="Dolby Vision Profile 5 / Mel (No Fallback)",
            category="hdr_dv",
            description="Single-layer Dolby Vision (Profile 5) without HDR10 fallback (may show green/purple tint on non-DV displays)",
            default_score=200,
            score=200,
            enabled=True,
            conditions=[
                TrashCondition(
                    name="Has DV tag",
                    pattern=r"\b(?:DV|Dolby[ .-]Vision|DoVi|Profile[ .-]5)\b",
                    required=True,
                ),
                TrashCondition(
                    name="Lacks HDR10 fallback",
                    pattern=r"\b(?:HDR10(?:\+)?|HDR)\b",
                    negate=True,
                    required=True,
                ),
            ],
        ),
        TrashCustomFormat(
            trash_id="hdr10plus",
            name="HDR10+",
            category="hdr_dv",
            description="Dynamic metadata HDR10+ format",
            default_score=600,
            score=600,
            enabled=True,
            conditions=[
                TrashCondition(
                    name="Has HDR10+ tag",
                    pattern=r"\b(?:HDR10\+|HDR10Plus|HDR10-Plus)\b",
                    required=True,
                ),
            ],
        ),
        TrashCustomFormat(
            trash_id="hdr10",
            name="HDR10 (Standard HDR)",
            category="hdr_dv",
            description="Standard 10-bit High Dynamic Range",
            default_score=450,
            score=450,
            enabled=True,
            conditions=[
                TrashCondition(
                    name="Has HDR10 / HDR tag",
                    pattern=r"\b(?:HDR10|HDR)\b",
                    required=True,
                ),
            ],
        ),

        # --- Advanced & Immersive Audio ---
        TrashCustomFormat(
            trash_id="truehd-atmos",
            name="Dolby TrueHD Atmos",
            category="audio_advanced",
            description="Lossless Dolby TrueHD stream containing spatial Dolby Atmos metadata",
            default_score=750,
            score=750,
            enabled=True,
            conditions=[
                TrashCondition(
                    name="TrueHD and Atmos",
                    pattern=r"\b(?:TrueHD|True-HD)\b.*\b(?:Atmos)\b|\b(?:Atmos)\b.*\b(?:TrueHD|True-HD)\b",
                    required=True,
                ),
            ],
        ),
        TrashCustomFormat(
            trash_id="dts-x",
            name="DTS:X",
            category="audio_advanced",
            description="DTS:X object-based immersive surround audio",
            default_score=700,
            score=700,
            enabled=True,
            conditions=[
                TrashCondition(
                    name="DTS:X tag",
                    pattern=r"\b(?:DTS-X|DTS:X|DTS_X)\b",
                    required=True,
                ),
            ],
        ),
        TrashCustomFormat(
            trash_id="dts-hd-ma",
            name="DTS-HD Master Audio",
            category="audio_advanced",
            description="Lossless DTS-HD MA master audio stream",
            default_score=500,
            score=500,
            enabled=True,
            conditions=[
                TrashCondition(
                    name="DTS-HD MA tag",
                    pattern=r"\b(?:DTS-HD[ .-]MA|DTS-HD|DTSHDMA|DTS-MA)\b",
                    required=True,
                ),
            ],
        ),
        TrashCustomFormat(
            trash_id="truehd",
            name="Dolby TrueHD",
            category="audio_advanced",
            description="Lossless Dolby TrueHD audio without Atmos",
            default_score=450,
            score=450,
            enabled=True,
            conditions=[
                TrashCondition(
                    name="TrueHD tag",
                    pattern=r"\b(?:TrueHD|True-HD)\b",
                    required=True,
                ),
                TrashCondition(
                    name="Not Atmos",
                    pattern=r"\b(?:Atmos)\b",
                    negate=True,
                    required=True,
                ),
            ],
        ),
        TrashCustomFormat(
            trash_id="flac",
            name="FLAC Lossless Audio",
            category="audio_advanced",
            description="Free Lossless Audio Codec stream",
            default_score=400,
            score=400,
            enabled=True,
            conditions=[
                TrashCondition(
                    name="FLAC tag",
                    pattern=r"\b(?:FLAC)\b",
                    required=True,
                ),
            ],
        ),
        TrashCustomFormat(
            trash_id="ddp-atmos",
            name="Dolby Digital Plus with Atmos",
            category="audio_advanced",
            description="Enhanced AC-3 / E-AC-3 streaming audio with JOC spatial Atmos metadata",
            default_score=350,
            score=350,
            enabled=True,
            conditions=[
                TrashCondition(
                    name="DDP and Atmos",
                    pattern=r"\b(?:DDP|DDP5\.1|DDP7\.1|EAC3|E-AC-3|E-AC3|DD\+)\b.*\b(?:Atmos)\b|\b(?:Atmos)\b.*\b(?:DDP|DDP5\.1|DDP7\.1|EAC3|E-AC-3|E-AC3|DD\+)\b",
                    required=True,
                ),
            ],
        ),
        TrashCustomFormat(
            trash_id="ddp-51-71",
            name="Dolby Digital Plus (E-AC-3)",
            category="audio_advanced",
            description="Enhanced AC-3 high-bitrate streaming surround audio",
            default_score=200,
            score=200,
            enabled=True,
            conditions=[
                TrashCondition(
                    name="DDP tag",
                    pattern=r"\b(?:DDP|DDP5\.1|DDP7\.1|EAC3|E-AC-3|E-AC3|DD\+)\b",
                    required=True,
                ),
            ],
        ),
        TrashCustomFormat(
            trash_id="channels-71",
            name="7.1 Surround Sound",
            category="audio_advanced",
            description="8-channel audio layout (7.1)",
            default_score=200,
            score=200,
            enabled=True,
            conditions=[
                TrashCondition(
                    name="7.1 channel tag",
                    pattern=r"\b(?:7\.1(?:ch)?|8ch)\b",
                    required=True,
                ),
            ],
        ),
        TrashCustomFormat(
            trash_id="channels-51",
            name="5.1 Surround Sound",
            category="audio_advanced",
            description="6-channel audio layout (5.1)",
            default_score=100,
            score=100,
            enabled=True,
            conditions=[
                TrashCondition(
                    name="5.1 channel tag",
                    pattern=r"\b(?:5\.1(?:ch)?|6ch)\b",
                    required=True,
                ),
            ],
        ),

        # --- Source & Remux Tiers ---
        TrashCustomFormat(
            trash_id="remux-tier-01",
            name="Remux Tier 01 (Premier)",
            category="source_remux_tier",
            description="Premier tier 1 Remux groups (FraMeSToR, EPSiLON, CtrlHD, TayTO, decibeL, KRaLiMARKO, NCmt, PlayBD, FLUX)",
            default_score=1500,
            score=1500,
            enabled=True,
            conditions=[
                TrashCondition(
                    name="Is Remux",
                    pattern=r"\b(?:REMUX|BDREMUX|BluRay[ .-]REMUX)\b",
                    required=True,
                ),
                TrashCondition(
                    name="Tier 01 group tag",
                    pattern=r"-(?:FraMeSToR|Framestor|EPSiLON|CtrlHD|TayTO|decibeL|KRaLiMARKO|NCmt|PlayBD|FLUX)\b",
                    required=True,
                ),
            ],
        ),
        TrashCustomFormat(
            trash_id="remux-tier-02",
            name="Remux Tier 02 (Quality)",
            category="source_remux_tier",
            description="High quality tier 2 Remux groups (BHDStudio, BiZKiT, HiFi, BMDru, ZQ, PmP, Flights, SURFCODE, WiLDCAT)",
            default_score=1000,
            score=1000,
            enabled=True,
            conditions=[
                TrashCondition(
                    name="Is Remux",
                    pattern=r"\b(?:REMUX|BDREMUX|BluRay[ .-]REMUX)\b",
                    required=True,
                ),
                TrashCondition(
                    name="Tier 02 group tag",
                    pattern=r"-(?:BHDStudio|BiZKiT|HiFi|BMDru|ZQ|PmP|Flights|SURFCODE|WiLDCAT|iKiW)\b",
                    required=True,
                ),
            ],
        ),
        TrashCustomFormat(
            trash_id="remux-tier-03",
            name="Remux Tier 03 (Standard)",
            category="source_remux_tier",
            description="Standard verified Remux releases",
            default_score=500,
            score=500,
            enabled=True,
            conditions=[
                TrashCondition(
                    name="Is Remux",
                    pattern=r"\b(?:REMUX|BDREMUX|BluRay[ .-]REMUX)\b",
                    required=True,
                ),
            ],
        ),

        # --- WEB-DL Tiers ---
        TrashCustomFormat(
            trash_id="web-tier-01",
            name="WEB-DL Tier 01 (Premier)",
            category="release_group_tier",
            description="Premier WEB-DL groups (FLUX, NTb, HONE, TEKNO, CMRG, CasStudio, DON)",
            default_score=1000,
            score=1000,
            enabled=True,
            conditions=[
                TrashCondition(
                    name="Tier 01 web group tag",
                    pattern=r"-(?:FLUX|NTb|HONE|TEKNO|CMRG|CasStudio|DON)\b",
                    required=True,
                ),
            ],
        ),
        TrashCustomFormat(
            trash_id="web-tier-02",
            name="WEB-DL Tier 02 (Popular)",
            category="release_group_tier",
            description="High quality web release groups and sources (AMZN, DSNP, NF, ATVP, HMAX, MAX, AppleTV, HBO, ROCCAT, MIXED)",
            default_score=500,
            score=500,
            enabled=True,
            conditions=[
                TrashCondition(
                    name="Tier 02 web group/service tag",
                    pattern=r"\b(?:AMZN|DSNP|NF|ATVP|HMAX|MAX|AppleTV|HBO|ROCCAT|MIXED)\b",
                    required=True,
                ),
            ],
        ),
        TrashCustomFormat(
            trash_id="web-tier-03",
            name="WEB-DL Tier 03 (Standard)",
            category="release_group_tier",
            description="Standard WEB groups (PlayWEB, KONTRAST, NTG, GLHF, SiGMA, WDYM, Tears)",
            default_score=250,
            score=250,
            enabled=True,
            conditions=[
                TrashCondition(
                    name="Tier 03 web group tag",
                    pattern=r"-(?:PlayWEB|KONTRAST|NTG|GLHF|SiGMA|WDYM|Tears)\b",
                    required=True,
                ),
            ],
        ),

        # --- Unwanted LQ Penalties & Rejections ---
        TrashCustomFormat(
            trash_id="lq-release-groups",
            name="Low Quality Encoders (YIFY/YTS/PSA/Pahe)",
            category="unwanted_lq",
            description="Extreme micro-encodes and aggressively starved bitrate releases (YIFY, YTS, PSA, Pahe, MeGusta, GalaxyTV, TGx, mSD)",
            default_score=-5000,
            score=-5000,
            enabled=True,
            conditions=[
                TrashCondition(
                    name="LQ group tag",
                    pattern=r"\b(?:YIFY|YTS(?:\.MX|\.LT|\.AG)?|PSA|Pahe|MeGusta|GalaxyTV|TGx|mSD|QxR|SAMPA)\b|-(?:YIFY|YTS|PSA|Pahe|MeGusta|GalaxyTV|TGx|mSD)\b",
                    required=True,
                ),
            ],
        ),
        TrashCustomFormat(
            trash_id="unwanted-video-sources",
            name="Cam / Telesync / Screener Sources",
            category="unwanted_lq",
            description="Unreleased/theatrical recording sources (CAM, HDCAM, TS, TELESYNC, HDTS, WORKPRINT, SCR, SCREENER, R5, TC, TELECINE)",
            default_score=-10000,
            score=-10000,
            enabled=True,
            conditions=[
                TrashCondition(
                    name="Cam/TS/Screener tag",
                    pattern=r"\b(?:CAM|HDCAM|CAMRip|TS|TELESYNC|HDTS|WORKPRINT|WP|SCR|SCREENER|DVD-SCR|R5|TC|TELECINE)\b",
                    required=True,
                ),
            ],
        ),
        TrashCustomFormat(
            trash_id="ai-upscaled-fake",
            name="AI / Artificial Upscales",
            category="unwanted_lq",
            description="Algorithmic or artificial upscales (AI.Upscale, Topaz, Upscaled)",
            default_score=-3000,
            score=-3000,
            enabled=True,
            conditions=[
                TrashCondition(
                    name="AI Upscale tag",
                    pattern=r"\b(?:AI[ .-]Upscale(?:d)?|Topaz|Upscaled)\b",
                    required=True,
                ),
            ],
        ),
        TrashCustomFormat(
            trash_id="extras-samples-unwanted",
            name="Extras / Samples / Non-Feature Releases",
            category="unwanted_lq",
            description="Standalone samples, trailers, featurettes, and commentary-only tracks",
            default_score=-2500,
            score=-2500,
            enabled=True,
            conditions=[
                TrashCondition(
                    name="Extras/sample tag",
                    pattern=r"\b(?:Sample|Trailer|Featurette|Deleted[ .-]Scenes|Behind[ .-]the[ .-]Scenes|Commentary[ .-]Only)\b",
                    required=True,
                ),
            ],
        ),

        # --- Anime Fansubs & Dual Audio ---
        TrashCustomFormat(
            trash_id="anime-tier-01",
            name="Anime Tier 01 (Premier Fansubs & Encoders)",
            category="anime_tier",
            description="Premier anime release groups (Erai-raws, SubsPlease, Judas, Beatrice-Raws, ASW, SSA, DKB, Moozzi2, Cleo)",
            default_score=1200,
            score=1200,
            enabled=True,
            conditions=[
                TrashCondition(
                    name="Premier anime group tag",
                    pattern=r"\[(?:Erai-raws|SubsPlease|Judas|Beatrice-Raws|ASW|SSA|DKB|Moozzi2|Cleo)\]|\b(?:Erai-raws|SubsPlease|Judas|Beatrice-Raws|ASW|SSA|DKB|Moozzi2|Cleo)\b",
                    required=True,
                ),
            ],
        ),
        TrashCustomFormat(
            trash_id="anime-tier-02",
            name="Anime Tier 02 (Standard Fansubs)",
            category="anime_tier",
            description="Standard anime release groups (HorribleSubs, Commie, Dame-Desu-Yo, GJM, Coalgirls)",
            default_score=600,
            score=600,
            enabled=True,
            conditions=[
                TrashCondition(
                    name="Standard anime group tag",
                    pattern=r"\[(?:HorribleSubs|Commie|Dame-Desu-Yo|GJM|Coalgirls)\]|\b(?:HorribleSubs|Commie|Dame-Desu-Yo|GJM|Coalgirls)\b",
                    required=True,
                ),
            ],
        ),
        TrashCustomFormat(
            trash_id="anime-dual-audio",
            name="Anime Dual Audio / Multi-Audio",
            category="anime_tier",
            description="Dual audio or multi-language dubbed anime releases",
            default_score=500,
            score=500,
            enabled=True,
            conditions=[
                TrashCondition(
                    name="Dual/Multi audio tag",
                    pattern=r"\b(?:Dual[ .-]Audio|Multi[ .-]Audio|Dubbed)\b",
                    required=True,
                ),
            ],
        ),
    ]


def get_default_trash_profiles() -> list[TrashProfile]:
    """Return default TRaSH scoring profiles (e.g. Remux Tier, Balanced, Anime)."""
    return [
        TrashProfile(
            profile_id="trash_balanced",
            name="TRaSH Balanced (Recommended)",
            description="Balanced profile with quality bonuses for Dolby Vision, Atmos, Tier 1/2 web groups, and penalties for low quality rips.",
            min_score=-1000,
            cutoff_score=2500,
            reject_negative_scores=False,
            format_scores={
                "dv-hdr10-fallback": 800,
                "dv-no-fallback": 200,
                "hdr10plus": 600,
                "hdr10": 450,
                "truehd-atmos": 750,
                "dts-x": 700,
                "dts-hd-ma": 500,
                "truehd": 450,
                "flac": 400,
                "ddp-atmos": 350,
                "ddp-51-71": 200,
                "channels-71": 200,
                "channels-51": 100,
                "remux-tier-01": 1500,
                "remux-tier-02": 1000,
                "remux-tier-03": 500,
                "web-tier-01": 1000,
                "web-tier-02": 500,
                "web-tier-03": 250,
                "lq-release-groups": -5000,
                "unwanted-video-sources": -10000,
                "ai-upscaled-fake": -3000,
                "extras-samples-unwanted": -2500,
                "anime-tier-01": 1200,
                "anime-dual-audio": 500,
            },
        ),
        TrashProfile(
            profile_id="trash_remux",
            name="TRaSH Remux Priority",
            description="Optimized for high-fidelity theater setups. Heavily prioritizes Tier 1/2 Remuxes and lossless spatial audio.",
            min_score=0,
            cutoff_score=3500,
            reject_negative_scores=True,
            format_scores={
                "dv-hdr10-fallback": 1000,
                "dv-no-fallback": 100,
                "hdr10plus": 700,
                "hdr10": 500,
                "truehd-atmos": 1200,
                "dts-x": 1000,
                "dts-hd-ma": 800,
                "truehd": 600,
                "flac": 500,
                "remux-tier-01": 2500,
                "remux-tier-02": 1800,
                "remux-tier-03": 1000,
                "web-tier-01": 400,
                "web-tier-02": 200,
                "lq-release-groups": -10000,
                "unwanted-video-sources": -10000,
                "ai-upscaled-fake": -5000,
            },
        ),
        TrashProfile(
            profile_id="trash_webdl",
            name="TRaSH WEB-DL Focus",
            description="Prioritizes clean streaming web releases (FLUX, NTb, DSNP, AMZN) with DDP Atmos and Dolby Vision.",
            min_score=-500,
            cutoff_score=2000,
            reject_negative_scores=False,
            format_scores={
                "dv-hdr10-fallback": 800,
                "dv-no-fallback": 300,
                "hdr10plus": 600,
                "ddp-atmos": 600,
                "ddp-51-71": 300,
                "web-tier-01": 1500,
                "web-tier-02": 800,
                "web-tier-03": 400,
                "lq-release-groups": -5000,
                "unwanted-video-sources": -10000,
            },
        ),
        TrashProfile(
            profile_id="trash_anime",
            name="TRaSH Anime Fansubs",
            description="Prioritizes premier anime fansub groups, Dual Audio, and lossless audio encodings.",
            min_score=0,
            cutoff_score=2000,
            reject_negative_scores=False,
            format_scores={
                "anime-tier-01": 2000,
                "anime-tier-02": 800,
                "anime-dual-audio": 800,
                "flac": 600,
                "lq-release-groups": -5000,
                "unwanted-video-sources": -10000,
            },
        ),
    ]
