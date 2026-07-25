"""Scrape funnel telemetry — log-only counters for diagnose-before-change."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from program.media.stream import Stream

# Prefer stable RTN / trash tokens seen in production debug lines.
_REASON_RE = re.compile(
    r"(extras_\w+|lang_\w+|remove_all_trash|trash|adult|remux|"
    r"title[_\s]?mismatch|incorrect[_\s]?\w+)",
    re.IGNORECASE,
)


def bucket_rtn_reason(exc: BaseException) -> str:
    """Map an RTN rejection into a short, aggregatable reason bucket."""

    msg = str(exc).strip()
    if not msg:
        return type(exc).__name__

    match = _REASON_RE.search(msg)
    if match:
        return match.group(1).lower().replace(" ", "_")

    # Prefer message body (after optional "Type: " prefix) over bare type name.
    body = msg.split(": ", 1)[-1].strip() if ": " in msg else msg
    cleaned = re.sub(r"[^\w]+", "_", body).strip("_").lower()
    if cleaned:
        return cleaned[:48]

    return type(exc).__name__


@dataclass
class ScrapeFunnelStats:
    """Per-scrape funnel counts (one item, one scrape pass)."""

    found: int = 0
    rtn_rejected: int = 0
    content_filtered: int = 0
    ranked: int = 0
    already_known: int = 0
    blacklisted: int = 0
    new: int = 0
    rtn_reasons: Counter[str] = field(default_factory=Counter)

    def record_rtn_reject(self, exc: BaseException) -> None:
        self.rtn_rejected += 1
        self.rtn_reasons[bucket_rtn_reason(exc)] += 1

    def record_content_filter(self) -> None:
        self.content_filtered += 1

    def classify_ranked_against_item(
        self,
        ranked_streams: dict[str, Stream],
        existing_streams: Sequence[Stream],
        blacklisted_streams: Sequence[Stream],
    ) -> None:
        """Split ranked streams into new / already_known / blacklisted."""

        self.ranked = len(ranked_streams)
        for stream in ranked_streams.values():
            if stream in blacklisted_streams:
                self.blacklisted += 1
            elif stream in existing_streams:
                self.already_known += 1
            else:
                self.new += 1

    def summary_line(self, item_log: str) -> str:
        reasons = ""
        if self.rtn_reasons:
            top = self.rtn_reasons.most_common(5)
            reasons = " rtn_top=[" + ", ".join(f"{k}:{v}" for k, v in top) + "]"
        return (
            f"Scrape funnel for {item_log}: "
            f"found={self.found} ranked={self.ranked} new={self.new} "
            f"already_known={self.already_known} blacklisted={self.blacklisted} "
            f"rtn_rejected={self.rtn_rejected} "
            f"content_filtered={self.content_filtered}{reasons}"
        )
