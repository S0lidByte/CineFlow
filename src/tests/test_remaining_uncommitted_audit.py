"""Regression coverage for the non-streaming uncommitted audit."""

import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

from program.db import db_functions
from program.db.base_model import get_base_metadata
from program.media.item import Episode, Movie, Season, Show
from program.utils import connection_tests as ct


@pytest.fixture
def probe_settings(monkeypatch):
    monkeypatch.setattr(
        ct.settings_manager,
        "settings",
        SimpleNamespace(
            downloaders=SimpleNamespace(
                proxy_url="http://proxy:8080",
                all_debrid=SimpleNamespace(api_key="ad-secret-key"),
            )
        ),
    )
    monkeypatch.delenv("CINEFLOW_E2E_FIXTURES", raising=False)
    monkeypatch.delenv("CINEFLOW_E2E_ALLDEBRID_ORIGIN", raising=False)


@pytest.mark.parametrize(
    "payload,message",
    [
        ([], "Invalid response format"),
        (None, "Invalid response format"),
        ({"status": "success"}, "Missing data in response"),
        ({"status": "success", "data": []}, "Missing data in response"),
        (
            {"status": "success", "data": {"user": []}},
            "Missing user details in response",
        ),
        (
            {"status": "success", "data": {"user": {"isPremium": "true"}}},
            "Account is not premium",
        ),
        (
            {"status": "success", "data": {"user": {"isPremium": 1}}},
            "Account is not premium",
        ),
        ({"status": "error", "error": {"message": "ad-secret-key"}}, "AllDebrid error"),
        ({"status": "error", "error": []}, "AllDebrid error"),
        ({}, "AllDebrid error"),
    ],
)
def test_payload_validation_and_cleanup(probe_settings, payload, message):
    client = MagicMock()
    client.__enter__.return_value = client
    client.get.return_value = (
        httpx.Response(200, content=b"null")
        if payload is None
        else httpx.Response(200, json=payload)
    )
    with patch.object(ct.httpx, "Client", return_value=client):
        result = ct._probe_all_debrid()
    assert not result.ok
    assert result.message == message
    client.__exit__.assert_called_once()


@pytest.mark.parametrize(
    "enabled,origin,expected",
    [
        ("false", "http://fixture:8765", "https://api.alldebrid.com"),
        ("TRUE", "http://fixture:8765", "https://api.alldebrid.com"),
        ("true", "", "https://api.alldebrid.com"),
        ("true", "http://fixture:8765", "http://fixture:8765"),
    ],
)
def test_fixture_opt_in_preserves_proxy(
    probe_settings, monkeypatch, enabled, origin, expected
):
    monkeypatch.setenv("CINEFLOW_E2E_FIXTURES", enabled)
    monkeypatch.setenv("CINEFLOW_E2E_ALLDEBRID_ORIGIN", origin)
    client = MagicMock()
    client.__enter__.return_value = client
    client.get.return_value = httpx.Response(
        200, json={"status": "success", "data": {"user": {"isPremium": True}}}
    )
    with patch.object(ct.httpx, "Client", return_value=client) as constructor:
        assert ct._probe_all_debrid().ok
    assert constructor.call_args.kwargs["base_url"] == expected
    assert constructor.call_args.kwargs["proxy"] == (
        None if expected.startswith("http://fixture:") else "http://proxy:8080"
    )
    client.__exit__.assert_called_once()


@pytest.mark.parametrize(
    "origin",
    [
        "http://fixture:8765@evil.example",
        "http://fixture:invalid",
        "http://fixture:8765/path",
        "https://fixture:8765",
        "http://fixture:8765?key=x",
        "http://fixture:8765#x",
    ],
)
def test_invalid_fixture_rejected_before_network(probe_settings, monkeypatch, origin):
    monkeypatch.setenv("CINEFLOW_E2E_FIXTURES", "true")
    monkeypatch.setenv("CINEFLOW_E2E_ALLDEBRID_ORIGIN", origin)
    with patch.object(ct.httpx, "Client") as constructor:
        assert ct._probe_all_debrid().message == "Invalid test fixture origin"
    constructor.assert_not_called()


@pytest.mark.parametrize(
    "response,expected",
    [
        (httpx.Response(200, content=b"not json"), "Invalid response"),
        (httpx.Response(500), "HTTP 500"),
    ],
)
def test_invalid_json_and_http_cleanup(probe_settings, response, expected):
    client = MagicMock()
    client.__enter__.return_value = client
    client.get.return_value = response
    with patch.object(ct.httpx, "Client", return_value=client):
        assert ct._probe_all_debrid().message == expected
    client.__exit__.assert_called_once()


def test_proxy_failure_closes_client(probe_settings):
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    client.get.side_effect = httpx.ProxyError("ad-secret-key")
    with patch.object(ct.httpx, "Client", return_value=client):
        assert ct._probe_all_debrid().message == "Connection failed"
    client.__exit__.assert_called_once()


def test_calendar_real_sqlite_window_memory_and_connection_cleanup(
    monkeypatch, tmp_path
):
    import tracemalloc

    engine = create_engine(f"sqlite:///{tmp_path / 'calendar.db'}")
    get_base_metadata().create_all(engine)
    start, end = datetime(2026, 10, 1), datetime(2026, 10, 31)
    with Session(engine) as session:
        for i in range(1200):
            session.add(
                Movie(
                    {
                        "title": f"Calendar {i}",
                        "tmdb_id": str(i),
                        "aired_at": start if i < 600 else datetime(2020, 1, 1),
                    }
                )
            )
        session.commit()
    checked_out = []
    event.listen(engine, "checkout", lambda *args: checked_out.append(1))
    event.listen(engine, "checkin", lambda *args: checked_out.pop())

    @contextmanager
    def owned_session():
        with Session(engine, expire_on_commit=False) as session:
            yield session

    monkeypatch.setattr(db_functions, "db_session", owned_session)
    tracemalloc.start()
    try:
        calendar = db_functions.create_calendar(start_date=start, end_date=end)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(calendar) == 600
    assert all(item["aired_at"] == start for item in calendar.values())
    assert checked_out == []
    # A realistic multi-batch library, not a mocked Result; no constant-memory claim.
    assert peak < 128 * 1024 * 1024
    print(f"calendar 1200 movies / 600 results peak traced bytes: {peak}")
    with Session(engine) as borrowed:
        assert (
            len(
                db_functions.create_calendar(
                    session=borrowed, start_date=start, end_date=end
                )
            )
            == 600
        )
        assert borrowed.execute(text("SELECT 1")).scalar_one() == 1
    assert checked_out == []
    engine.dispose()


def test_calendar_show_season_episode_hierarchy_and_deduplication(tmp_path):
    """Verify Show/Season/Episode parent ID propagation, fallback airings, and child deduplication."""
    engine = create_engine(f"sqlite:///{tmp_path / 'calendar_hierarchy.db'}")
    get_base_metadata().create_all(engine)
    start = datetime(2026, 10, 1)
    end = datetime(2026, 10, 31)

    with Session(engine) as session:
        # Show 1: Has an Episode airing in the window -> Episode should be returned, Show fallback suppressed
        show1 = Show(
            {
                "title": "Hierarchy Show With Episode",
                "tmdb_id": "1001",
                "tvdb_id": "2001",
                "release_data": {"next_aired": "2026-10-15T00:00:00Z"},
            }
        )
        s1 = Season({"number": 1})
        ep1 = Episode({"number": 1, "aired_at": datetime(2026, 10, 15)})
        show1.add_season(s1)
        s1.add_episode(ep1)

        # Show 2: Has a Season airing in the window -> Season should be returned, Show fallback suppressed
        show2 = Show(
            {
                "title": "Hierarchy Show With Season",
                "tmdb_id": "1002",
                "tvdb_id": "2002",
                "release_data": {"next_aired": "2026-10-20T00:00:00Z"},
            }
        )
        s2 = Season({"number": 2, "aired_at": datetime(2026, 10, 20)})
        show2.add_season(s2)

        # Show 3: Has NO child airings in window, but valid release_data date -> Show fallback returned
        show3 = Show(
            {
                "title": "Hierarchy Show Fallback Only",
                "tmdb_id": "1003",
                "tvdb_id": "2003",
                "release_data": {"next_aired": "2026-10-25T00:00:00Z"},
            }
        )

        # Show 4: Malformed release_data date -> Gracefully skipped with warning
        show4 = Show(
            {
                "title": "Hierarchy Show Malformed Date",
                "tmdb_id": "1004",
                "tvdb_id": "2004",
                "release_data": {"next_aired": "NOT-A-VALID-DATE"},
            }
        )

        # Show 5: Release date outside window -> Excluded
        show5 = Show(
            {
                "title": "Hierarchy Show Outside Window",
                "tmdb_id": "1005",
                "tvdb_id": "2005",
                "release_data": {"next_aired": "2025-01-01T00:00:00Z"},
            }
        )

        session.add_all([show1, show2, show3, show4, show5])
        session.commit()

        calendar = db_functions.create_calendar(
            session=session, start_date=start, end_date=end
        )

    # Show 1's Episode (ep1.id) is included; show1.id is NOT duplicated
    assert ep1.id in calendar
    assert show1.id not in calendar
    ep_entry = calendar[ep1.id]
    assert ep_entry["item_type"] == "episode"
    assert ep_entry["show_title"] == "Hierarchy Show With Episode"
    assert ep_entry["tmdb_id"] == "1001"
    assert ep_entry["tvdb_id"] == "2001"
    assert ep_entry["season"] == 1
    assert ep_entry["episode"] == 1

    # Show 2's Season (s2.id) is included; show2.id is NOT duplicated
    assert s2.id in calendar
    assert show2.id not in calendar
    season_entry = calendar[s2.id]
    assert season_entry["item_type"] == "season"
    assert season_entry["show_title"] == "Hierarchy Show With Season"
    assert season_entry["tmdb_id"] == "1002"
    assert season_entry["tvdb_id"] == "2002"
    assert season_entry["season"] == 2

    # Show 3 fallback (show3.id) is included
    assert show3.id in calendar
    show3_entry = calendar[show3.id]
    assert show3_entry["item_type"] == "show"
    assert show3_entry["show_title"] == "Hierarchy Show Fallback Only"
    assert show3_entry["tmdb_id"] == "1003"
    assert show3_entry["tvdb_id"] == "2003"

    # Show 4 (malformed) and Show 5 (outside window) are excluded
    assert show4.id not in calendar
    assert show5.id not in calendar

    engine.dispose()


@pytest.mark.parametrize(
    "args,expected_err",
    [
        (["--seconds", "0"], "--seconds must be between 1 and 900"),
        (["--seconds", "901"], "--seconds must be between 1 and 900"),
        (["--seconds", "-5"], "--seconds must be between 1 and 900"),
        (
            [
                "--output",
                str(
                    (
                        Path.home().resolve().parent / "outside_forbidden_location.json"
                    ).resolve()
                ),
            ],
            "Output path must be within the project root",
        ),
    ],
)
def test_d80_endurance_argument_and_path_guards(tmp_path, args, expected_err):
    """Verify D80 CLI argument range validation and path traversal security guards."""
    repo_root = Path(__file__).resolve().parents[2]
    script_path = repo_root / "scripts" / "d80_endurance.py"
    default_output = str(tmp_path / "valid_out.json")

    cmd = [sys.executable, str(script_path)] + args
    if "--output" not in args:
        cmd += ["--output", default_output]

    res = subprocess.run(
        cmd, capture_output=True, text=True, cwd=str(repo_root), check=False
    )
    assert res.returncode != 0
    assert expected_err in res.stderr
