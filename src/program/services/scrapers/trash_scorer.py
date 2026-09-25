"""TRaSH Guides Custom Formats scoring and evaluation engine.

Evaluates raw release titles against TRaSH conditions, profiles, and score tables.
Provides high-throughput regex evaluation with LRU pattern compilation.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache

import regex as regex_lib
from loguru import logger

from program.settings.trash_catalog import (
    get_default_trash_custom_formats,
)
from program.settings.trash_models import (
    TrashCondition,
    TrashCustomFormat,
    TrashEvaluationSummary,
    TrashFormatMatchResult,
    TrashProfile,
)


@lru_cache(maxsize=512)
def _compile_pattern(pattern: str) -> regex_lib.Pattern[str]:
    """Compile regex pattern with case-insensitivity unless case-flagged."""
    flags = regex_lib.IGNORECASE
    clean_pattern = pattern
    if len(pattern) >= 2 and pattern.startswith("/") and pattern.endswith("/"):
        clean_pattern = pattern[1:-1]
        flags = 0
    return regex_lib.compile(clean_pattern, flags)


def clear_trash_cache() -> None:
    """Explicitly clear cached compiled regex patterns."""
    _compile_pattern.cache_clear()


def evaluate_trash_condition(condition: TrashCondition, raw_title: str) -> bool:
    """Evaluate a single condition against the raw release title."""
    if not condition.pattern:
        return True

    try:
        compiled = _compile_pattern(condition.pattern)
        matched = bool(compiled.search(raw_title))
    except Exception as e:
        logger.trace(f"Failed regex evaluation for pattern '{condition.pattern}': {e}")
        matched = False

    return not matched if condition.negate else matched


def evaluate_trash_custom_format(
    custom_format: TrashCustomFormat,
    raw_title: str,
    score_override: int | None = None,
) -> TrashFormatMatchResult:
    """Evaluate whether a custom format matches the given release title."""
    effective_score = score_override if score_override is not None else custom_format.score

    if not custom_format.enabled or not custom_format.conditions:
        return TrashFormatMatchResult(
            trash_id=custom_format.trash_id,
            name=custom_format.name,
            category=custom_format.category,
            score=effective_score,
            matched=False,
        )

    required_conditions = [c for c in custom_format.conditions if c.required]
    optional_conditions = [c for c in custom_format.conditions if not c.required]

    # 1. All required conditions must evaluate to True
    for cond in required_conditions:
        if not evaluate_trash_condition(cond, raw_title):
            return TrashFormatMatchResult(
                trash_id=custom_format.trash_id,
                name=custom_format.name,
                category=custom_format.category,
                score=effective_score,
                matched=False,
            )

    # 2. If optional conditions exist, at least one must evaluate to True
    if optional_conditions:
        any_optional_passed = any(
            evaluate_trash_condition(cond, raw_title) for cond in optional_conditions
        )
        if not any_optional_passed:
            return TrashFormatMatchResult(
                trash_id=custom_format.trash_id,
                name=custom_format.name,
                category=custom_format.category,
                score=effective_score,
                matched=False,
            )

    return TrashFormatMatchResult(
        trash_id=custom_format.trash_id,
        name=custom_format.name,
        category=custom_format.category,
        score=effective_score,
        matched=True,
    )


def evaluate_trash_release(
    raw_title: str,
    formats: Sequence[TrashCustomFormat] | None = None,
    profile: TrashProfile | None = None,
    *,
    min_score: int | None = None,
    reject_negative_scores: bool = False,
    reject_unwanted_sources: bool = True,
) -> TrashEvaluationSummary:
    """Evaluate all active TRaSH Custom Formats for a release and compute composite score.

    Args:
        raw_title: The release title string.
        formats: List of custom formats to evaluate (defaults to standard catalog).
        profile: Optional active profile with score overrides and cutoff rules.
        min_score: Minimum total score threshold (releases below are rejected).
        reject_negative_scores: If True, net negative scores trigger rejection.
        reject_unwanted_sources: If True, CAM/TS sources with score <= -10000 trigger instant rejection.
    """
    if formats is None:
        formats = get_default_trash_custom_formats()

    score_overrides = profile.format_scores if profile else {}
    effective_min_score = min_score if min_score is not None else (profile.min_score if profile else None)
    effective_reject_neg = reject_negative_scores or (profile.reject_negative_scores if profile else False)

    total_score = 0
    matched_results: list[TrashFormatMatchResult] = []
    rejected = False
    rejection_reason = None

    for cf in formats:
        override = score_overrides.get(cf.trash_id)
        result = evaluate_trash_custom_format(cf, raw_title, score_override=override)
        if result.matched:
            matched_results.append(result)
            total_score += result.score

            # Check for critical rejection criteria
            if reject_unwanted_sources and result.score <= -10000:
                rejected = True
                rejection_reason = f"Rejected by critical unwanted source format: {result.name}"

    if not rejected and effective_reject_neg and total_score < 0:
        rejected = True
        rejection_reason = f"Rejected due to negative TRaSH net score ({total_score})"

    if not rejected and effective_min_score is not None and total_score < effective_min_score:
        rejected = True
        rejection_reason = f"Rejected: TRaSH score ({total_score}) is below minimum threshold ({effective_min_score})"

    return TrashEvaluationSummary(
        total_score=total_score,
        matched_formats=matched_results,
        rejected_by_lq=rejected,
        rejection_reason=rejection_reason,
    )
