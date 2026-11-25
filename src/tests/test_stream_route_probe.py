import json
import runpy
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import trio
from trio.testing import MockClock

probe = runpy.run_path(
    str(Path(__file__).resolve().parents[2] / "scripts" / "stream_route_probe.py")
)["probe"]


@pytest.mark.parametrize(
    "rows,error",
    [
        ([("https://cdn.example.test/media?token=private",)], None),
        ([], "no_stored_link"),
        ([(None,)], "no_stored_link"),
        ([("https://cdn.example.test/one",), (None,)], "ambiguous_stored_links"),
    ],
)
def test_stored_link_lookup_is_read_only_and_refuses_ambiguous_records(
    tmp_path, monkeypatch, rows, error
):
    import psycopg2

    monkeypatch.setenv("CINEFLOW_FORCE_ENV", "false")
    runtime = probe.__globals__
    path = tmp_path / "cineflow.json"
    path.write_text(
        json.dumps(
            {"database": {"host": "postgresql+psycopg2://test@localhost/cineflow"}}
        ),
        encoding="utf-8",
    )
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = rows
    connect = MagicMock(return_value=conn)
    monkeypatch.setattr(psycopg2, "connect", connect)

    if error is None:
        assert runtime["read_stored_link"](path, 12853) == rows[0][0]
    else:
        with pytest.raises(runtime["StoredLinkError"], match=f"^{error}$"):
            runtime["read_stored_link"](path, 12853)

    connect.assert_called_once_with(
        "postgresql://test@localhost/cineflow", connect_timeout=5
    )
    conn.set_session.assert_called_once_with(readonly=True, autocommit=False)
    assert cursor.execute.call_count == 2
    sql, params = cursor.execute.call_args.args
    assert sql.startswith("SELECT ") and "LIMIT 2" in sql
    assert params == (12853,)
    conn.commit.assert_not_called()
    conn.close.assert_called_once()


def test_stored_link_lookup_never_emits_database_credentials(tmp_path, monkeypatch):
    import psycopg2

    monkeypatch.setenv("CINEFLOW_FORCE_ENV", "false")
    path = tmp_path / "cineflow.json"
    secret = "private-database-password"
    path.write_text(
        json.dumps({"database": {"host": f"postgresql://user:{secret}@localhost/db"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        psycopg2, "connect", MagicMock(side_effect=psycopg2.OperationalError(secret))
    )
    runtime = probe.__globals__
    with pytest.raises(runtime["StoredLinkError"], match="^stored_link_lookup_failed$"):
        runtime["read_stored_link"](path, 12853)


def test_stored_link_lookup_uses_forced_env_without_settings_file(
    tmp_path, monkeypatch
):
    import psycopg2

    monkeypatch.setenv("CINEFLOW_FORCE_ENV", "true")
    monkeypatch.setenv(
        "CINEFLOW_DATABASE_HOST", "postgresql+psycopg2://env@db/cineflow"
    )
    runtime = probe.__globals__
    missing_file = tmp_path / "missing-cineflow.json"

    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [("https://cdn.example.test/from-env",)]
    connect = MagicMock(return_value=conn)
    monkeypatch.setattr(psycopg2, "connect", connect)

    assert runtime["read_stored_link"](missing_file, 42) == (
        "https://cdn.example.test/from-env"
    )
    connect.assert_called_once_with("postgresql://env@db/cineflow", connect_timeout=5)


def test_stored_link_cli_passes_exact_range_without_echoing_link(monkeypatch, capsys):
    runtime = probe.__globals__
    url = "https://cdn.example.test/movie?token=stored-private"
    lookup = MagicMock(return_value=url)

    async def check_probe(target, offset, bounded):
        assert (target, offset, bounded) == (url, 3342266829, False)
        return {"result": "ok", "sample_bytes": 1048576}

    monkeypatch.setitem(runtime, "read_stored_link", lookup)
    monkeypatch.setitem(runtime, "probe", check_probe)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "stream_route_probe.py",
            "--stored-item-id",
            "12853",
            "--settings-file",
            "cineflow.json",
            "--offset",
            "3342266829",
        ],
    )

    assert runtime["main"]() == 0
    lookup.assert_called_once_with(Path("cineflow.json"), 12853)
    out = capsys.readouterr()
    assert "stored-private" not in out.out and not out.err
    assert json.loads(out.out)["sample_bytes"] == 1048576


def test_stored_link_settings_file_uses_cineflow_filename_precedence(
    tmp_path, monkeypatch
):
    runtime = probe.__globals__
    monkeypatch.setitem(runtime, "SETTINGS_DIR", tmp_path)
    monkeypatch.delenv("CINEFLOW_SETTINGS_FILENAME", raising=False)
    monkeypatch.delenv("SETTINGS_FILENAME", raising=False)
    resolve = runtime["stored_link_settings_file"]
    assert resolve() == tmp_path / "settings.json"
    (tmp_path / "cineflow.json").touch()
    assert resolve() == tmp_path / "cineflow.json"
    monkeypatch.setenv("SETTINGS_FILENAME", "legacy-name.json")
    assert resolve() == tmp_path / "legacy-name.json"
    monkeypatch.setenv("CINEFLOW_SETTINGS_FILENAME", "config-current.json")
    assert resolve() == tmp_path / "config-current.json"


@pytest.mark.parametrize("bounded", [False, True])
def test_sample_timing_separates_headers_and_body(bounded):
    sample = 1024 * 1024
    offset = 4096
    end = offset + sample - 1
    seen = []

    class DelayedRange(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("Range"))
            assert self.headers.get("Accept-Encoding") == "identity"
            time.sleep(0.05)
            last = end if bounded else end + 1
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {offset}-{last}/{end + 2}")
            self.send_header("Content-Length", str(last - offset + 1))
            self.end_headers()
            time.sleep(0.05)
            self.wfile.write(b"x" * (last - offset + 1))

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), DelayedRange) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/sample?token=probe-secret"

            async def run():
                return await probe(
                    url, offset, bounded, allow_loopback=True, trust_env=False
                )

            report = trio.run(run)
        finally:
            server.shutdown()
            thread.join(timeout=5)

    assert seen == [f"bytes={offset}-{end if bounded else ''}"]
    assert report["result"] == "ok"
    assert report["http_status"] == 206
    assert report["sample_bytes"] == sample
    assert report["headers_s"] - report["tcp_ready_s"] >= 0.04
    assert report["first_body_s"] - report["headers_s"] >= 0.04
    assert report["sample_complete_s"] >= report["first_body_s"]
    assert "probe-secret" not in json.dumps(report)


def test_does_not_follow_redirect_or_read_an_invalid_range():
    seen = []

    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/second?token=redirect-secret")
            self.end_headers()

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Redirect) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/first"

            async def run():
                return await probe(url, 0, False, allow_loopback=True, trust_env=False)

            report = trio.run(run)
        finally:
            server.shutdown()
            thread.join(timeout=5)

    assert seen == ["/first"]
    assert report["http_status"] == 302
    assert report["sample_bytes"] == 0
    assert report["result"] == "invalid_range_response"
    assert "redirect-secret" not in json.dumps(report)


def test_rejects_unencrypted_remote_or_credentialed_input_without_a_request():
    for url in ("http://example.test/", "https://user:pw@example.test/"):
        report = trio.run(probe, url, 0, True)
        assert report == {
            "offset": 0,
            "range_mode": "bounded",
            "sample_bytes": 0,
            "result": "invalid_input",
        }


def test_cpu_snapshot_reports_only_allowlisted_kernel_counters(tmp_path, monkeypatch):
    stat = tmp_path / "cpu.stat"
    stat.write_text(
        "usage_usec 1040\nnr_periods 123\nnr_throttled 5\nthrottled_usec 24\n"
        "private_name ignored\nuser_usec 999\n",
        encoding="ascii",
    )
    runtime = probe.__globals__
    monkeypatch.setitem(runtime, "CPU_STAT", stat)
    assert runtime["cpu_counters"]() == {
        "usage_usec": 1040,
        "nr_periods": 123,
        "nr_throttled": 5,
        "throttled_usec": 24,
    }
    monkeypatch.setitem(runtime, "CPU_STAT", tmp_path / "unavailable")
    assert runtime["cpu_counters"]() == {}


@pytest.mark.parametrize("resources_only", [True, False])
def test_resources_are_sampled_over_the_same_window(resources_only, monkeypatch):
    runtime = probe.__globals__
    counters = iter(
        [
            {"usage_usec": 100, "nr_throttled": 4, "throttled_usec": 60},
            {"usage_usec": 250, "nr_throttled": 5, "throttled_usec": 50},
        ]
    )
    monkeypatch.setitem(runtime, "cpu_counters", lambda: next(counters))
    monkeypatch.setitem(runtime, "cache_free_bytes", lambda: {"warm": 42, "hot": None})

    async def fetch():
        await trio.sleep(2)
        return {"result": "ok", "sample_complete_s": 2}

    report = trio.run(
        runtime["sample_with_resources"],
        None if resources_only else fetch,
        clock=MockClock(autojump_threshold=0),
    )
    assert report["result"] == "ok"
    assert ("sample_complete_s" in report) is not resources_only
    resources = report["resources"]
    assert resources.pop("started_at_utc").endswith("+00:00")
    assert resources == {
        "window_s": 5 if resources_only else 2,
        "cache_free_bytes": {"warm": 42, "hot": None},
        "cgroup_cpu_delta": {"usage_usec": 150, "nr_throttled": 1},
    }
