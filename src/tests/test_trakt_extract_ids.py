"""Trakt _extract_ids skip logging stays quiet for empty wrappers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from program.services.content.trakt import TraktContent


def test_extract_ids_summarizes_empty_show_skips():
    content = object.__new__(TraktContent)
    items = [
        SimpleNamespace(show=None, seasons=None),
        SimpleNamespace(show=None, seasons=None),
        SimpleNamespace(show=SimpleNamespace(ids=SimpleNamespace(tvdb=81189))),
    ]

    with patch("program.services.content.trakt.logger") as mock_logger:
        ids = content._extract_ids(items)

    assert ids == [(81189, "show")]
    debug_msgs = [str(c.args[0]) for c in mock_logger.debug.call_args_list]
    assert len(debug_msgs) == 1
    assert "lacking media payload" in debug_msgs[0]
    assert "no_show=2" in debug_msgs[0]
    assert "no_movie=0" in debug_msgs[0]
