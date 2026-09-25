"""TRaSH Guides Custom Formats (CF) catalog and evaluation schemas.

Defines standardized TRaSH custom format definitions, conditions (regex, required,
negate), profiles (e.g. HD/UHD Bluray Web, Remux Tier, Anime Sonarr/Radarr),
and evaluation score records.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TrashCondition(BaseModel):
    """A single matching condition within a TRaSH custom format."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(default="", description="Name or description of the condition")
    pattern: str = Field(description="Regex pattern for matching release title/raw string")
    negate: bool = Field(default=False, description="If True, match succeeds when pattern does NOT match")
    required: bool = Field(default=True, description="If True, this condition must pass for the format to match")


class TrashCustomFormat(BaseModel):
    """Definition of a TRaSH Custom Format with scoring and conditions."""

    model_config = ConfigDict(extra="ignore")

    trash_id: str = Field(description="Unique TRaSH identifier / slug, e.g. 'dv-hdr10-fallback'")
    name: str = Field(description="Human readable name of the custom format")
    category: Literal[
        "hdr_dv",
        "audio_advanced",
        "release_group_tier",
        "source_remux_tier",
        "unwanted_lq",
        "anime_tier",
        "streaming_service",
        "misc",
    ] = Field(default="misc", description="Category grouping for the format")
    description: str = Field(default="", description="Detailed description of the format intent")
    default_score: int = Field(default=0, description="Default score weight assigned when matched")
    score: int = Field(default=0, description="Active user-configured score weight")
    enabled: bool = Field(default=True, description="Whether this custom format is actively evaluated")
    conditions: list[TrashCondition] = Field(default_factory=list[TrashCondition], description="List of conditions that define this format")

    def evaluate(self, raw_title: str) -> bool:
        """Evaluate if the raw release title matches all required conditions."""
        if not self.enabled or not self.conditions:
            return False

        import regex as regex_lib

        # For a format to match:
        # 1. All 'required' conditions must pass.
        # 2. At least one non-negated condition must match if any non-required conditions exist.
        required_conditions = [c for c in self.conditions if c.required]
        optional_conditions = [c for c in self.conditions if not c.required]

        for cond in required_conditions:
            try:
                matched = bool(regex_lib.search(cond.pattern, raw_title, regex_lib.IGNORECASE))
            except Exception:
                matched = False

            if cond.negate:
                if matched:
                    return False
            elif not matched:
                return False

        if optional_conditions:
            any_optional_passed = False
            for cond in optional_conditions:
                try:
                    matched = bool(regex_lib.search(cond.pattern, raw_title, regex_lib.IGNORECASE))
                except Exception:
                    matched = False

                if cond.negate:
                    if not matched:
                        any_optional_passed = True
                        break
                elif matched:
                    any_optional_passed = True
                    break
            if not any_optional_passed:
                return False

        return True


class TrashFormatMatchResult(BaseModel):
    """Result of evaluating a single custom format against a title."""

    trash_id: str
    name: str
    category: str
    score: int
    matched: bool


class TrashEvaluationSummary(BaseModel):
    """Aggregate evaluation result of all TRaSH Custom Formats for a release."""

    total_score: int = 0
    matched_formats: list[TrashFormatMatchResult] = Field(default_factory=list[TrashFormatMatchResult])
    rejected_by_lq: bool = False
    rejection_reason: str | None = None


class TrashProfile(BaseModel):
    """A collection of custom formats with associated score overrides and minimum/cutoff scores."""

    profile_id: str = Field(description="Unique profile ID, e.g. 'uhd_bluray_web', 'remux_tier', 'anime_tier'")
    name: str = Field(description="Display name of the profile")
    description: str = Field(default="", description="Description of the profile intent")
    min_score: int = Field(default=0, description="Minimum TRaSH score threshold; releases under this are rejected if strict")
    cutoff_score: int = Field(default=10000, description="Score threshold at which a release is considered ideal")
    reject_negative_scores: bool = Field(default=False, description="Automatically reject releases that have a negative net TRaSH score")
    format_scores: dict[str, int] = Field(default_factory=dict, description="Map of trash_id -> score override for this profile")
