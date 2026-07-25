"""Scrape funnel telemetry — counters for diagnose-before-change."""

from __future__ import annotations

import re
import threading
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from cachetools import TTLCache

from program.media.stream import Stream

# Prefer stable RTN / trash tokens seen in production debug lines.
_REASON_RE = re.compile(
    r"(extras_\w+|lang_\w+|remove_all_trash|trash|adult|remux|"
    r"title[_\s]?mismatch|incorrect[_\s]?\w+)",
    re.IGNORECASE,
)

# Last completed scrape funnel per media item id (process-local, TTL).
_LAST_FUNNEL_LOCK = threading.RLock()
_LAST_FUNNEL_BY_ITEM: TTLCache[int, dict[str, Any]] = TTLCache(maxsize=2048, ttl=3600)


def bucket_rtn_reason(exc: BaseException) -> str:
    """Map an RTN rejection into a short, aggregatable reason bucket."""

    msg = str(exc).strip()
    if not msg:
        return type(exc).__name__

    # RTN title failures: "… does not match the correct title. correct title: …"
    # Prefer a stable bucket over slugifying the whole message (which produced
    # noisy keys like saint_seiya_legend_of_crimson_youth_parsed_title).
    if re.search(r"does not match the correct title", msg, re.IGNORECASE):
        return "title_mismatch"

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
    rtn_reasons: Counter[str] = field(default_factory=lambda: Counter[str]())

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

    def top_rtn_reasons(self, limit: int = 5) -> list[tuple[str, int]]:
        return self.rtn_reasons.most_common(limit)

    def to_summary(self, *, item_id: int | None = None, item_log: str | None = None) -> dict[str, Any]:
        """JSON-serializable funnel summary for API / UI."""

        return {
            "item_id": item_id,
            "item_log": item_log,
            "found": self.found,
            "ranked": self.ranked,
            "new": self.new,
            "already_known": self.already_known,
            "blacklisted": self.blacklisted,
            "rtn_rejected": self.rtn_rejected,
            "content_filtered": self.content_filtered,
            "rtn_top": [
                {"reason": reason, "count": count}
                for reason, count in self.top_rtn_reasons(5)
            ],
        }

    def summary_line(self, item_log: str) -> str:
        reasons = ""
        if self.rtn_reasons:
            top = self.top_rtn_reasons(5)
            reasons = " rtn_top=[" + ", ".join(f"{k}:{v}" for k, v in top) + "]"
        return (
            f"Scrape funnel for {item_log}: "
            f"found={self.found} ranked={self.ranked} new={self.new} "
            f"already_known={self.already_known} blacklisted={self.blacklisted} "
            f"rtn_rejected={self.rtn_rejected} "
            f"content_filtered={self.content_filtered}{reasons}"
        )


def remember_funnel_summary(item_id: int | None, summary: dict[str, Any]) -> None:
    """Cache the latest funnel summary for an item (best-effort, process-local)."""

    if item_id is None:
        return
    with _LAST_FUNNEL_LOCK:
        _LAST_FUNNEL_BY_ITEM[int(item_id)] = summary


def get_remembered_funnel_summary(item_id: int) -> dict[str, Any] | None:
    with _LAST_FUNNEL_LOCK:
        cached = _LAST_FUNNEL_BY_ITEM.get(int(item_id))
        return dict(cached) if cached is not None else None
