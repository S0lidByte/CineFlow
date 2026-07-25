"""Ranking / RTN helper endpoints for the settings UI."""

from __future__ import annotations

import re
import time
from collections import defaultdict, deque
from threading import Lock
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from RTN import RTN, DefaultRanking, parse
from RTN.exceptions import GarbageTorrent

from program.services.scrapers.funnel import get_remembered_funnel_summary
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
from program.settings.ranking_presets import (
    GOLDEN_TITLES,
    RANKING_PRESETS,
    TITLE_MATCHING_MODES,
)

router = APIRouter(
    prefix="/ranking",
    tags=["ranking"],
    responses={404: {"description": "Not found"}},
)

_DENY_KEY_RE = re.compile(r"denied by:\s*([a-z0-9_]+)", re.IGNORECASE)
_INFOHASH_RE = re.compile(r"^[0-9a-fA-F]{40}$")

# Soft rate limit for expensive ranking helper endpoints (per process).
_RATE_LIMIT_LOCK = Lock()
_RATE_LIMIT_HITS: dict[str, deque[float]] = defaultdict(deque)
_RATE_LIMIT_WINDOW_SEC = 60.0
_RATE_LIMIT_MAX_HITS = 30


def _enforce_ranking_rate_limit(bucket: str) -> None:
    now = time.monotonic()
    with _RATE_LIMIT_LOCK:
        hits = _RATE_LIMIT_HITS[bucket]
        while hits and now - hits[0] > _RATE_LIMIT_WINDOW_SEC:
            hits.popleft()
        if len(hits) >= _RATE_LIMIT_MAX_HITS:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Rate limit exceeded for {bucket} "
                    f"({_RATE_LIMIT_MAX_HITS}/{int(_RATE_LIMIT_WINDOW_SEC)}s). Retry later."
                ),
            )
        hits.append(now)


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
    aliases: dict[str, list[str]] | None = Field(
        default=None,
        description=(
            "Optional title aliases (country → names) for remake / alias diagnose. "
            "Empty dict disables aliases for this test."
        ),
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
    title_similarity_threshold: float | None = None
    aliases_used: bool = False
    parsed: dict[str, Any] | None = None


class RankingMetaResponse(BaseModel):
    message: str
    deny_keys: dict[str, str]
    attribute_titles: dict[str, str]
    categories: dict[str, str]
    soft_opt_in_links: dict[str, dict[str, str]]
    pattern_limits: dict[str, int]
    title_matching_modes: list[dict[str, Any]] = Field(default_factory=list)
    presets: list[dict[str, Any]] = Field(default_factory=list)
    golden_titles: dict[str, str] = Field(default_factory=dict)


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


class FunnelReasonCount(BaseModel):
    reason: str
    count: int


class FunnelSummaryResponse(BaseModel):
    message: str
    found: bool = False
    item_id: int | None = None
    item_log: str | None = None
    found_count: int = 0
    ranked: int = 0
    new: int = 0
    already_known: int = 0
    blacklisted: int = 0
    rtn_rejected: int = 0
    content_filtered: int = 0
    rtn_top: list[FunnelReasonCount] = Field(default_factory=list)


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
    if key == "title_mismatch":
        return (
            "Title similarity failed. Try Ranking Studio matching modes / aliases, "
            "lower title_similarity temporarily for diagnose, or enable Scraping → "
            "enable_aliases. Do not silently accept remake mismatches."
        )
    return None


def _normalize_infohash(raw: str | None) -> str:
    infohash = (raw or "0" * 40).strip().lower()
    if not _INFOHASH_RE.fullmatch(infohash):
        raise HTTPException(
            status_code=400,
            detail="infohash must be exactly 40 hexadecimal characters",
        )
    return infohash


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
            "title_mismatch": {
                "scraping_path": "scraping.enable_aliases",
                "ranking_path": "ranking.options.title_similarity",
                "label": "Aliases + title similarity (remake diagnose)",
            },
        },
        pattern_limits={
            "max_patterns_per_list": MAX_PATTERNS_PER_LIST,
            "max_pattern_length": MAX_PATTERN_LENGTH,
        },
        title_matching_modes=list(TITLE_MATCHING_MODES),
        presets=list(RANKING_PRESETS),
        golden_titles=dict(GOLDEN_TITLES),
    )


@router.get(
    "/presets",
    operation_id="get_ranking_presets",
    response_model=dict[str, Any],
)
async def get_ranking_presets() -> dict[str, Any]:
    """Shared Ranking Studio preset contract (ids + options) for FE alignment."""
    return {
        "message": "Ranking presets",
        "presets": RANKING_PRESETS,
        "title_matching_modes": TITLE_MATCHING_MODES,
        "golden_titles": GOLDEN_TITLES,
    }


@router.get(
    "/funnel/{item_id}",
    operation_id="get_scrape_funnel_summary",
    response_model=FunnelSummaryResponse,
)
async def get_scrape_funnel_summary(item_id: int) -> FunnelSummaryResponse:
    """Return the last remembered scrape funnel summary for an item (process-local)."""
    cached = get_remembered_funnel_summary(item_id)
    if not cached:
        return FunnelSummaryResponse(
            message="No recent scrape funnel for this item",
            found=False,
            item_id=item_id,
        )
    return FunnelSummaryResponse(
        message="Scrape funnel summary",
        found=True,
        item_id=cached.get("item_id", item_id),
        item_log=cached.get("item_log"),
        found_count=int(cached.get("found", 0)),
        ranked=int(cached.get("ranked", 0)),
        new=int(cached.get("new", 0)),
        already_known=int(cached.get("already_known", 0)),
        blacklisted=int(cached.get("blacklisted", 0)),
        rtn_rejected=int(cached.get("rtn_rejected", 0)),
        content_filtered=int(cached.get("content_filtered", 0)),
        rtn_top=[
            FunnelReasonCount(reason=str(r.get("reason", "")), count=int(r.get("count", 0)))
            for r in (cached.get("rtn_top") or [])
            if isinstance(r, dict)
        ],
    )


@router.post(
    "/validate-patterns",
    operation_id="validate_ranking_patterns",
    response_model=PatternValidateResponse,
)
async def validate_ranking_patterns(body: PatternValidateRequest) -> PatternValidateResponse:
    """Validate require/exclude/preferred regex lists (length, compile, ReDoS heuristics)."""
    _enforce_ranking_rate_limit("validate-patterns")
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
    _enforce_ranking_rate_limit("test")
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
        infohash = _normalize_infohash(body.infohash)
        aliases = body.aliases if body.aliases is not None else {}
        threshold = float(getattr(settings_model.options, "title_similarity", 0.85))

        try:
            torrent = rtn_instance.rank(
                raw_title=body.raw_title,
                infohash=infohash,
                correct_title=body.correct_title or "",
                remove_trash=body.remove_trash,
                aliases=aliases,
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
                title_similarity_threshold=threshold,
                aliases_used=bool(aliases),
                parsed=parsed,
            )
        except GarbageTorrent as exc:
            msg = str(exc)
            match = _DENY_KEY_RE.search(msg)
            deny_key = match.group(1).lower() if match else None
            # Language rejects often surface as plain messages without "denied by:".
            if deny_key is None and "missing_required_language" in msg.lower():
                deny_key = "missing_required_language"
            if deny_key is None and re.search(
                r"does not match the correct title", msg, re.IGNORECASE
            ):
                deny_key = "title_mismatch"
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
                title_similarity_threshold=threshold,
                aliases_used=bool(aliases),
                parsed=parsed,
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Ranking test failed: {exc}") from exc
