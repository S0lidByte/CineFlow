"""Unit tests for CineFlow logging color palette, channel defaults, state inline tags, and WCAG AA compliance."""

import os
from unittest.mock import patch

import pytest
from loguru import logger

from program.media.state import States
from program.utils.logging import (
    STATE_COLORS,
    escape_loguru_tags,
    format_blacklisted,
    format_excluded,
    format_state,
    get_state_color,
    setup_logger,
)


def srgb_to_lin(c: float) -> float:
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(hex_code: str) -> float:
    hex_clean = hex_code.lstrip("#")
    r = int(hex_clean[0:2], 16)
    g = int(hex_clean[2:4], 16)
    b = int(hex_clean[4:6], 16)
    return 0.2126 * srgb_to_lin(r) + 0.7152 * srgb_to_lin(g) + 0.0722 * srgb_to_lin(b)


def contrast_ratio(hex1: str, hex2: str) -> float:
    l1 = relative_luminance(hex1)
    l2 = relative_luminance(hex2)
    lighter = max(l1, l2)
    darker = min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def test_channel_level_colors():
    """Verify standard CineFlow custom Loguru level colors match the approved palette."""
    setup_logger("DEBUG")

    expected_channels = {
        "SCRAPER": "<fg #06B6D4>",
        "DEBRID": "<fg #FB923C>",
        "COMPLETED": "<fg #10B981>",
        "NOT_FOUND": "<fg #94A3B8>",
        "VFS": "<fg #C084FC>",
        "STREAM": "<fg #C084FC>",
        "CACHE": "<fg #4ADE80>",
        "DATABASE": "<fg #E879F9>",
        "PROGRAM": "<fg #FB923C>",
    }

    for channel_name, expected_color in expected_channels.items():
        lvl = logger.level(channel_name)
        assert (
            lvl.color == expected_color
        ), f"Channel {channel_name} color mismatch: got {lvl.color}, expected {expected_color}"


def test_state_colors_enum_coverage():
    """Verify STATE_COLORS covers all 12 members of the States enum and default fallback."""
    assert len(STATE_COLORS) == 12
    assert set(STATE_COLORS.keys()) == {s.name for s in States}
    assert get_state_color("NonExistentState") == "#94A3B8"


def test_lifecycle_state_formatting():
    """Verify inline color tags for all 12 media lifecycle states."""
    expected_states = {
        States.Unknown: "<fg #94A3B8>Unknown</fg #94A3B8>",
        States.Unreleased: "<fg #A78BFA>Unreleased</fg #A78BFA>",
        States.Ongoing: "<fg #38BDF8>Ongoing</fg #38BDF8>",
        States.Requested: "<fg #F59E0B>Requested</fg #F59E0B>",
        States.Indexed: "<fg #38BDF8>Indexed</fg #38BDF8>",
        States.Scraped: "<fg #818CF8>Scraped</fg #818CF8>",
        States.Downloaded: "<fg #34D399>Downloaded</fg #34D399>",
        States.Symlinked: "<fg #C084FC>Symlinked</fg #C084FC>",
        States.Completed: "<fg #10B981>Completed</fg #10B981>",
        States.PartiallyCompleted: "<fg #FBBF24>PartiallyCompleted</fg #FBBF24>",
        States.Paused: "<fg #94A3B8>Paused</fg #94A3B8>",
        States.Failed: "<fg #F87171>Failed</fg #F87171>",
    }

    assert len(expected_states) == len(States)

    for state_enum, expected_tag in expected_states.items():
        assert format_state(state_enum) == expected_tag
        assert format_state(state_enum.name) == expected_tag


def test_blacklist_and_exclusion_formatting():
    """Verify distinct styling between permanent blacklisting (#FB7185) and temporary exclusions (#FBBF24)."""
    assert format_blacklisted() == "<fg #FB7185>BLACKLISTED</fg #FB7185>"
    assert format_blacklisted("blacklisting") == "<fg #FB7185>blacklisting</fg #FB7185>"
    assert format_excluded() == "<fg #FBBF24>EXCLUDED</fg #FBBF24>"
    assert (
        format_excluded("excluding locally")
        == "<fg #FBBF24>excluding locally</fg #FBBF24>"
    )


def test_wcag_aa_contrast_compliance():
    """Verify all palette colors achieve WCAG AA contrast ratio >= 4.5:1 against dark backgrounds."""
    palette = {
        "SCRAPER": "06B6D4",
        "DEBRID": "FB923C",
        "COMPLETED": "10B981",
        "NOT_FOUND": "94A3B8",
        "VFS": "C084FC",
        "STREAM": "C084FC",
        "CACHE": "4ADE80",
        "DATABASE": "E879F9",
        "PROGRAM": "FB923C",
        **STATE_COLORS,
        "BLACKLISTED": "FB7185",
        "EXCLUDED": "FBBF24",
    }

    dark_backgrounds = ["000000", "1E1E1E"]

    for name, hex_val in palette.items():
        for bg in dark_backgrounds:
            cr = contrast_ratio(hex_val, bg)
            assert (
                cr >= 4.5
            ), f"Color {name} (#{hex_val}) failed WCAG AA on #{bg}: contrast ratio {cr:.2f} < 4.5:1"


def test_environment_variable_overrides():
    """Verify user override support via CINEFLOW_LOGGER_* environment variables."""
    with patch.dict(os.environ, {"CINEFLOW_LOGGER_SCRAPER_FG": "AABBCC"}):
        setup_logger("DEBUG")
        assert logger.level("SCRAPER").color == "<fg #AABBCC>"

    # Reset
    setup_logger("DEBUG")
    assert logger.level("SCRAPER").color == "<fg #06B6D4>"

    with patch.dict(os.environ, {"CINEFLOW_LOGGER_STATE_REQUESTED_FG": "112233"}):
        assert get_state_color("Requested") == "#112233"
        assert format_state(States.Requested) == "<fg #112233>Requested</fg #112233>"


def test_escape_loguru_tags_prevents_parsing_errors():
    """Verify escape_loguru_tags escapes unformatted brackets to avoid Loguru ValueError."""
    raw_title = "Movie <2024> [1080p] <special>"
    escaped = escape_loguru_tags(raw_title)
    assert r"\<" in escaped

    logs = []
    logger_id = logger.add(lambda msg: logs.append(msg))
    try:
        # Should not raise ValueError with opt(colors=True)
        logger.opt(colors=True).debug(
            f"Title: {escaped} -> {format_state(States.Scraped)}"
        )
    finally:
        logger.remove(logger_id)

    assert any("Movie <2024>" in line for line in logs)
