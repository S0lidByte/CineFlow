"""Ranking / RTN helper endpoints for the settings UI."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from RTN import RTN, DefaultRanking, parse
from RTN.exceptions import GarbageTorrent

from program.services.scrapers.shared import (
    get_ranking_overrides,
    normalize_rtn_language_settings,
    ranking_model,
    ranking_settings,
)
from program.settings.models import RTNSettingsModel
from program.settings.ranking_descriptions import (
    ATTRIBUTE_TITLES,
    CATEGORY_HELP,
    DENY_KEY_HELP,
)
from program.settings.ranking_patterns import (
    MAX_PATTERN_LENGTH,
    MAX_PATTERNS_PER_LIST,
    validate_pattern_lists,
    validate_ranking_payload_patterns,
)

router = APIRouter(
    prefix="/ranking",
    tags=["ranking"],
    responses={404: {"description": "Not found"}},
)

_DENY_KEY_RE = re.compile(r"denied by:\s*([a-z0-9_]+)", re.IGNORECASE)

# Non-matrix deny keys that still map to Scraping soft-opt-ins / language editors.
_SOFT_OPT_IN_DENY_KEYS = frozenset(
    {
        "extras_dubbed",
        "missing_required_language",
    }
)


class RankingTestRequest(BaseModel):
    raw_title: str = Field(min_length=1, description="Torrent / release title to test")
    correct_title: str | None = Field(
        default=None, description="Optional media title for similarity scoring"
    )
    infohash: str | None = Field(default=None, description="Optional infohash (40 hex chars)")
    remove_trash: bool = Field(default=True, description="Apply trash heuristics")
    ranking_overrides: dict[str, list[str]] | None = Field(
        default=None,
        description="Optional category→attribute map to force-enable fetch without saving",
    )
    ranking: dict[str, Any] | None = Field(
        default=None,
        description="Optional full ranking settings payload to test against (unsaved edits)",
    )


class RankingTestResponse(BaseModel):
    message: str
    accepted: bool
    rank: int = 0
    lev_ratio: float = 0.0
    fetch: bool = False
    deny_reason: str | None = None
    deny_help: str | None = None
    scraping_hint: str | None = None
    parsed: dict[str, Any] | None = None


class RankingMetaResponse(BaseModel):
    message: str
    deny_keys: dict[str, str]
    attribute_titles: dict[str, str]
    categories: dict[str, str]
    soft_opt_in_links: dict[str, dict[str, str]]
    pattern_limits: dict[str, int]


class PatternValidateRequest(BaseModel):
    require: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    preferred: list[str] = Field(default_factory=list)
    preview_title: str | None = Field(
        default=None, description="Optional release title for match preview"
    )


class PatternIssueModel(BaseModel):
    field: str
    index: int
    pattern: str
    message: str


class PatternPreviewModel(BaseModel):
    require_matches: list[str] = Field(default_factory=list)
    exclude_matches: list[str] = Field(default_factory=list)
    preferred_matches: list[str] = Field(default_factory=list)


class PatternValidateResponse(BaseModel):
    message: str
    valid: bool
    errors: list[PatternIssueModel] = Field(default_factory=list[PatternIssueModel])
    preview: PatternPreviewModel | None = None


def _scraping_hint_for_deny(deny_key: str | None) -> str | None:
    if not deny_key:
        return None
    key = deny_key.lower()
    if key == "extras_dubbed":
        return (
            "Open Scraping settings and consider enabling anime_allow_extras_dubbed "
            "(anime-only soft-opt-in), or turn on Fetch for extras → dubbed in Ranking."
        )
    if key == "missing_required_language":
        return (
            "Adjust Ranking → Languages (required), or for anime MULTI/dual titles "
            "enable Scraping → anime_allow_multi_audio."
        )
    if key in _SOFT_OPT_IN_DENY_KEYS:
        return "See Scraping soft-opt-ins for anime-only ranking retries."
    return None


@router.get("/meta", operation_id="get_ranking_meta", response_model=RankingMetaResponse)
async def get_ranking_meta() -> RankingMetaResponse:
    """Deny-key map and attribute titles for the Ranking settings panel."""
    return RankingMetaResponse(
        message="Ranking metadata",
        deny_keys=dict(DENY_KEY_HELP),
        attribute_titles=dict(ATTRIBUTE_TITLES),
        categories=dict(CATEGORY_HELP),
        soft_opt_in_links={
            "extras_dubbed": {
                "scraping_path": "scraping.anime_allow_extras_dubbed",
                "ranking_path": "ranking.custom_ranks.extras.dubbed",
                "label": "Anime allow extras.dubbed (soft-opt-in)",
            },
            "missing_required_language": {
                "scraping_path": "scraping.anime_allow_multi_audio",
                "ranking_path": "ranking.languages.required",
                "label": "Anime allow MULTI/dual-audio retry (soft-opt-in)",
            },
        },
        pattern_limits={
            "max_patterns_per_list": MAX_PATTERNS_PER_LIST,
            "max_pattern_length": MAX_PATTERN_LENGTH,
        },
    )


@router.post(
    "/validate-patterns",
    operation_id="validate_ranking_patterns",
    response_model=PatternValidateResponse,
)
async def validate_ranking_patterns(body: PatternValidateRequest) -> PatternValidateResponse:
    """Validate require/exclude/preferred regex lists (length, compile, ReDoS heuristics)."""
    result = validate_pattern_lists(
        require=body.require,
        exclude=body.exclude,
        preferred=body.preferred,
        preview_title=body.preview_title,
    )
    preview = None
    if result.preview is not None:
        preview = PatternPreviewModel(
            require_matches=result.preview.require_matches,
            exclude_matches=result.preview.exclude_matches,
            preferred_matches=result.preview.preferred_matches,
        )
    return PatternValidateResponse(
        message="Patterns valid" if result.valid else "Pattern validation failed",
        valid=result.valid,
        errors=[
            PatternIssueModel(
                field=e.field,
                index=e.index,
                pattern=e.pattern,
                message=e.message,
            )
            for e in result.errors
        ],
        preview=preview,
    )


@router.post("/test", operation_id="test_ranking", response_model=RankingTestResponse)
async def test_ranking(body: RankingTestRequest) -> RankingTestResponse:
    """Run a release title through RTN using current (or provided) ranking settings."""
    try:
        if body.ranking is not None:
            try:
                validate_ranking_payload_patterns(body.ranking)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            settings_model = RTNSettingsModel(**body.ranking)
        else:
            settings_model = RTNSettingsModel(**ranking_settings.model_dump())
            if body.ranking_overrides:
                overridden = get_ranking_overrides(body.ranking_overrides)
                if overridden is not None:
                    settings_model = RTNSettingsModel(**overridden.model_dump())

        normalize_rtn_language_settings(settings_model)
        rtn_instance = RTN(settings_model, ranking_model or DefaultRanking())
        infohash = (body.infohash or "0" * 40).lower()
        if len(infohash) != 40:
            raise HTTPException(status_code=400, detail="infohash must be 40 hex characters")

        try:
            torrent = rtn_instance.rank(
                raw_title=body.raw_title,
                infohash=infohash,
                correct_title=body.correct_title or "",
                remove_trash=body.remove_trash,
                aliases={},
            )
            parsed = torrent.data.model_dump() if hasattr(torrent.data, "model_dump") else None
            return RankingTestResponse(
                message="Accepted by RTN",
                accepted=True,
                rank=int(torrent.rank),
                lev_ratio=float(torrent.lev_ratio),
                fetch=bool(torrent.fetch),
                deny_reason=None,
                deny_help=None,
                scraping_hint=None,
                parsed=parsed,
            )
        except GarbageTorrent as exc:
            msg = str(exc)
            match = _DENY_KEY_RE.search(msg)
            deny_key = match.group(1).lower() if match else None
            # Language rejects often surface as plain messages without "denied by:".
            if deny_key is None and "missing_required_language" in msg.lower():
                deny_key = "missing_required_language"
            try:
                parsed_data = parse(body.raw_title)
                parsed = (
                    parsed_data.model_dump()
                    if hasattr(parsed_data, "model_dump")
                    else None
                )
            except Exception:
                parsed = None
            return RankingTestResponse(
                message="Rejected by RTN",
                accepted=False,
                rank=0,
                lev_ratio=0.0,
                fetch=False,
                deny_reason=deny_key or msg,
                deny_help=DENY_KEY_HELP.get(deny_key) if deny_key else None,
                scraping_hint=_scraping_hint_for_deny(deny_key),
                parsed=parsed,
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Ranking test failed: {exc}") from exc
