"""Ranking / RTN helper endpoints for the settings UI."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from RTN import RTN, DefaultRanking, parse
from RTN.exceptions import GarbageTorrent

from program.services.scrapers.shared import (
    _normalize_rtn_language_settings,
    get_ranking_overrides,
    ranking_model,
    ranking_settings,
)
from program.settings.models import RTNSettingsModel
from program.settings.ranking_descriptions import (
    ATTRIBUTE_TITLES,
    CATEGORY_HELP,
    DENY_KEY_HELP,
)

router = APIRouter(
    prefix="/ranking",
    tags=["ranking"],
    responses={404: {"description": "Not found"}},
)

_DENY_KEY_RE = re.compile(r"denied by:\s*([a-z0-9_]+)", re.IGNORECASE)


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
    parsed: dict[str, Any] | None = None


class RankingMetaResponse(BaseModel):
    message: str
    deny_keys: dict[str, str]
    attribute_titles: dict[str, str]
    categories: dict[str, str]


@router.get("/meta", operation_id="get_ranking_meta", response_model=RankingMetaResponse)
async def get_ranking_meta() -> RankingMetaResponse:
    """Deny-key map and attribute titles for the Ranking settings panel."""
    return RankingMetaResponse(
        message="Ranking metadata",
        deny_keys=dict(DENY_KEY_HELP),
        attribute_titles=dict(ATTRIBUTE_TITLES),
        categories=dict(CATEGORY_HELP),
    )


@router.post("/test", operation_id="test_ranking", response_model=RankingTestResponse)
async def test_ranking(body: RankingTestRequest) -> RankingTestResponse:
    """Run a release title through RTN using current (or provided) ranking settings."""
    try:
        if body.ranking is not None:
            settings_model = RTNSettingsModel(**body.ranking)
        else:
            settings_model = RTNSettingsModel(**ranking_settings.model_dump())
            if body.ranking_overrides:
                overridden = get_ranking_overrides(body.ranking_overrides)
                if overridden is not None:
                    settings_model = RTNSettingsModel(**overridden.model_dump())

        _normalize_rtn_language_settings(settings_model)
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
                parsed=parsed,
            )
        except GarbageTorrent as exc:
            msg = str(exc)
            match = _DENY_KEY_RE.search(msg)
            deny_key = match.group(1).lower() if match else None
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
                parsed=parsed,
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Ranking test failed: {exc}") from exc
