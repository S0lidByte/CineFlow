"""Regression tests for the backend entrypoint process supervisor."""

# These tests execute the POSIX entrypoint and are run in Linux CI; Windows skips them.
from __future__ import annotations

import os
import signal
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="entrypoint requires POSIX")

BACKEND_ROOT = Path(__file__).parents[2]
ENTRYPOINT = BACKEND_ROOT / "entrypoint.sh"


def _entrypoint_environment(shim_dir: Path, *, mount_present: bool, timeout: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "PUID": "0",
            "PGID": "0",
            "MOUNT_WAIT_TIMEOUT": timeout,
            "MOUNT_PRESENT": "1" if mount_present else "0",
            "PATH": f"{shim_dir}{os.pathsep}{env['PATH']}",
        }
    )
    return env


def _run_entrypoint(
    tmp_path: Path,
    python_code: str,
    *,
    timeout: str = "5",
    mount_present: bool = False,
) -> subprocess.CompletedProcess[str]:
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "grep"
    shim.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  *'rivenvfs'*) [ \"$MOUNT_PRESENT\" = 1 ] && exit 0 || exit 1 ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)

    python = shim_dir / "python"
    python.write_text(
        "#!/bin/sh\n" + textwrap.dedent(python_code),
        encoding="utf-8",
    )
    python.chmod(0o755)

    env = _entrypoint_environment(
        shim_dir, mount_present=mount_present, timeout=timeout
    )
    env["RIVEN_PYTHON"] = str(python)
    return subprocess.run(
        ["/bin/sh", str(ENTRYPOINT)],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def test_child_exit_before_mount_propagates_status(tmp_path: Path):
    result = _run_entrypoint(tmp_path, "exit 17\n")

    assert result.returncode == 17
    assert "exit code 17" in result.stdout


def test_mount_timeout_terminates_and_reaps_child(tmp_path: Path):
    result = _run_entrypoint(tmp_path, "trap 'exit 0' TERM\nwhile :; do sleep 1; done\n", timeout="0")

    assert result.returncode == 1
    assert "Timed out waiting" in result.stdout


def test_invalid_mount_timeout_is_characterized_without_hanging(tmp_path: Path):
    result = _run_entrypoint(tmp_path, "exit 17\n", timeout="not-a-number")

    assert result.returncode == 17
    assert "exit code 17" in result.stdout


def test_mount_present_waits_for_and_returns_child_status(tmp_path: Path):
    result = _run_entrypoint(tmp_path, "exit 23\n", mount_present=True)

    assert result.returncode == 23


def test_sigterm_forwards_shutdown_and_exits_cleanly(tmp_path: Path):
    shim_dir = tmp_path / "signal-shim"
    shim_dir.mkdir()
    python = shim_dir / "python"
    python.write_text("#!/bin/sh\ntrap 'exit 0' TERM\nwhile :; do sleep 1; done\n", encoding="utf-8")
    python.chmod(0o755)
    grep = shim_dir / "grep"
    grep.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    grep.chmod(0o755)

    env = _entrypoint_environment(
        shim_dir, mount_present=False, timeout="30"
    )
    env["RIVEN_PYTHON"] = str(python)
    process = subprocess.Popen(
        ["/bin/sh", str(ENTRYPOINT)],
        cwd=BACKEND_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        time.sleep(0.2)
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.kill()
