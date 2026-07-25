"""Unit tests for scrape funnel telemetry (Phase 1 — log-only)."""

from __future__ import annotations

from types import SimpleNamespace

from program.services.scrapers.funnel import (
    ScrapeFunnelStats,
    bucket_rtn_reason,
)
from program.services.scrapers.shared import parse_results
from program.settings import settings_manager


class _HashStream:
    """Minimal stream stand-in keyed by infohash (avoids SQLAlchemy Stream init)."""

    def __init__(self, infohash: str):
        self.infohash = infohash

    def __hash__(self) -> int:
        return hash(self.infohash)

    def __eq__(self, other: object) -> bool:
        return getattr(other, "infohash", None) == self.infohash


def test_bucket_rtn_reason_extracts_known_tokens():
    assert (
        bucket_rtn_reason(Exception("GarbageTorrent: extras_dubbed")) == "extras_dubbed"
    )
    assert bucket_rtn_reason(Exception("lang_ja not allowed")) == "lang_ja"
    assert bucket_rtn_reason(ValueError("mystery failure")).startswith("mystery")


def test_classify_ranked_against_item_splits_buckets():
    existing = _HashStream("a" * 40)
    blacklisted = _HashStream("b" * 40)
    fresh = _HashStream("c" * 40)

    funnel = ScrapeFunnelStats(found=10)
    funnel.classify_ranked_against_item(
        {"a" * 40: existing, "b" * 40: blacklisted, "c" * 40: fresh},
        [existing],
        [blacklisted],
    )

    assert funnel.ranked == 3
    assert funnel.already_known == 1
    assert funnel.blacklisted == 1
    assert funnel.new == 1


def test_summary_line_includes_rtn_top():
    funnel = ScrapeFunnelStats(
        found=5,
        ranked=1,
        new=0,
        already_known=1,
        blacklisted=0,
        rtn_rejected=3,
        content_filtered=1,
    )
    funnel.rtn_reasons["extras_dubbed"] = 2
    funnel.rtn_reasons["lang_ja"] = 1

    line = funnel.summary_line("Movie X")
    assert "found=5" in line
    assert "ranked=1" in line
    assert "already_known=1" in line
    assert "rtn_rejected=3" in line
    assert "content_filtered=1" in line
    assert "extras_dubbed:2" in line
    assert "lang_ja:1" in line


def test_record_rtn_reject_increments_and_buckets():
    funnel = ScrapeFunnelStats()
    funnel.record_rtn_reject(Exception("extras_dubbed trash"))
    funnel.record_rtn_reject(Exception("extras_dubbed again"))
    funnel.record_content_filter()

    assert funnel.rtn_rejected == 2
    assert funnel.content_filtered == 1
    assert funnel.rtn_reasons["extras_dubbed"] == 2


def test_parse_results_accounts_for_every_input_in_funnel():
    """Every raw result is accepted, RTN-rejected, or content-filtered."""

    item = SimpleNamespace(
        top_title="Totally Fake Title That Will Never Match 2099",
        log_string="Fake Title",
        country=None,
        is_anime=False,
        aired_at=None,
        get_aliases=lambda: {},
    )
    results = {
        "e" * 40: "Completely Unrelated Junk 480p CAM XXX",
        "f" * 40: "Another Trash Release 240p",
    }
    funnel = ScrapeFunnelStats(found=len(results))

    with settings_manager.override(languages={"required": []}):
        streams = parse_results(item, results, manual=False, funnel=funnel)

    assert (
        funnel.rtn_rejected + funnel.content_filtered + len(streams) == funnel.found
    )
