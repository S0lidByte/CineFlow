"""Validate RTN require/exclude/preferred regex pattern lists.

RTN compiles patterns with the ``regex`` package:
- ``/pattern/`` → case-sensitive
- bare string → case-insensitive

This module adds count/length limits and ReDoS heuristics before compile,
so Ranking Studio and settings save reject unsafe user patterns early.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal, cast

import regex as regex_lib

PatternListName = Literal["require", "exclude", "preferred"]

MAX_PATTERNS_PER_LIST = 32
MAX_PATTERN_LENGTH = 200
MAX_GROUP_NESTING = 6

# Nested quantifiers / classic ReDoS shapes, e.g. (a+)+, (a|a)+, (.*)*.
_NESTED_QUANTIFIER = re.compile(
    r"(?:\([^)]*[+*{][^)]*\)|\[[^\]]*[+*{][^\]]*\]|"
    r"(?:\.|\w|\[[^\]]+\])[+*]\??)\s*[+*{]"
)
_OVERLAPPING_ALTERNATION = re.compile(r"\((?:[^)]*\|){8,}[^)]*\)[+*]")


@dataclass
class PatternIssue:
    field: PatternListName
    index: int
    pattern: str
    message: str


@dataclass
class PatternPreview:
    require_matches: list[str] = field(default_factory=list[str])
    exclude_matches: list[str] = field(default_factory=list[str])
    preferred_matches: list[str] = field(default_factory=list[str])


@dataclass
class PatternValidationResult:
    valid: bool
    errors: list[PatternIssue] = field(default_factory=list[PatternIssue])
    preview: PatternPreview | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": [
                {
                    "field": e.field,
                    "index": e.index,
                    "pattern": e.pattern,
                    "message": e.message,
                }
                for e in self.errors
            ],
            "preview": (
                {
                    "require_matches": self.preview.require_matches,
                    "exclude_matches": self.preview.exclude_matches,
                    "preferred_matches": self.preview.preferred_matches,
                }
                if self.preview is not None
                else None
            ),
        }


def _strip_case_markers(pattern: str) -> str:
    if len(pattern) >= 2 and pattern.startswith("/") and pattern.endswith("/"):
        return pattern[1:-1]
    return pattern


def _group_nesting_depth(pattern: str) -> int:
    depth = 0
    max_depth = 0
    escaped = False
    for ch in pattern:
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == "(":
            depth += 1
            max_depth = max(max_depth, depth)
        elif ch == ")" and depth > 0:
            depth -= 1
    return max_depth


def _looks_like_redos(pattern: str) -> str | None:
    body = _strip_case_markers(pattern)
    if _NESTED_QUANTIFIER.search(body):
        return "Pattern looks like a nested-quantifier ReDoS risk"
    if _OVERLAPPING_ALTERNATION.search(body):
        return "Pattern has heavy overlapping alternation (ReDoS risk)"
    if _group_nesting_depth(body) > MAX_GROUP_NESTING:
        return f"Group nesting exceeds {MAX_GROUP_NESTING}"
    return None


def compile_rtn_pattern(pattern: str) -> regex_lib.Pattern[str]:
    """Compile a single RTN-style pattern string."""
    if pattern.startswith("/") and pattern.endswith("/") and len(pattern) > 2:
        return regex_lib.compile(pattern[1:-1])
    return regex_lib.compile(pattern, regex_lib.IGNORECASE)


def validate_pattern_string(
    pattern: str,
    *,
    field: PatternListName,
    index: int,
) -> PatternIssue | None:
    if not pattern.strip():
        return PatternIssue(field, index, pattern, "Pattern cannot be empty")
    if len(pattern) > MAX_PATTERN_LENGTH:
        return PatternIssue(
            field,
            index,
            pattern,
            f"Pattern exceeds max length ({MAX_PATTERN_LENGTH})",
        )
    redos = _looks_like_redos(pattern)
    if redos:
        return PatternIssue(field, index, pattern, redos)
    try:
        compile_rtn_pattern(pattern)
    except Exception as exc:  # noqa: BLE001 — surface compile errors to UI
        return PatternIssue(field, index, pattern, f"Invalid regex: {exc}")
    return None


def validate_pattern_lists(
    *,
    require: list[str] | None = None,
    exclude: list[str] | None = None,
    preferred: list[str] | None = None,
    preview_title: str | None = None,
) -> PatternValidationResult:
    """Validate RTN pattern lists; optionally preview matches against a title."""
    lists: dict[PatternListName, list[str]] = {
        "require": list(require or []),
        "exclude": list(exclude or []),
        "preferred": list(preferred or []),
    }
    errors: list[PatternIssue] = []

    for name, patterns in lists.items():
        if len(patterns) > MAX_PATTERNS_PER_LIST:
            errors.append(
                PatternIssue(
                    name,
                    -1,
                    "",
                    f"At most {MAX_PATTERNS_PER_LIST} patterns allowed in {name}",
                )
            )
            continue
        for idx, pattern in enumerate(patterns):
            issue = validate_pattern_string(pattern, field=name, index=idx)
            if issue is not None:
                errors.append(issue)

    preview: PatternPreview | None = None
    if preview_title is not None and not errors:
        preview = PatternPreview()
        for name, patterns in lists.items():
            matches: list[str] = []
            for pattern in patterns:
                compiled = compile_rtn_pattern(pattern)
                if compiled.search(preview_title):
                    matches.append(pattern)
            if name == "require":
                preview.require_matches = matches
            elif name == "exclude":
                preview.exclude_matches = matches
            else:
                preview.preferred_matches = matches

    return PatternValidationResult(valid=not errors, errors=errors, preview=preview)


def validate_ranking_payload_patterns(ranking: dict[str, Any]) -> None:
    """Raise ValueError with a concise message if ranking patterns are invalid."""
    result = validate_pattern_lists(
        require=_as_str_list(ranking.get("require")),
        exclude=_as_str_list(ranking.get("exclude")),
        preferred=_as_str_list(ranking.get("preferred")),
    )
    if result.valid:
        return
    first = result.errors[0]
    where = first.field if first.index < 0 else f"{first.field}[{first.index}]"
    raise ValueError(f"Invalid ranking pattern ({where}): {first.message}")


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("require/exclude/preferred must be lists of strings")
    out: list[str] = []
    for item in cast(list[Any], value):
        out.append(str(item))
    return out