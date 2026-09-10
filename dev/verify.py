"""Run the backend quality checks required before pushing and in CI."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run_check(name: str, command: Sequence[str]) -> int:
    """Run one quality check and preserve its failure code."""
    print(f"\n==> {name}")
    return subprocess.run(command, cwd=ROOT, check=False).returncode


def main() -> int:
    checks = (
        ("uv.lock is in sync", ("uv", "lock", "--check")),
        ("Ruff", ("uv", "run", "ruff", "check", "src")),
        ("Pyright", ("uv", "run", "--with", "pyright==1.1.400", "pyright")),
        ("CI unit tests", ("uv", "run", "pytest", "-q", "-m", "not db and not fuse")),
    )
    for name, command in checks:
        if returncode := run_check(name, command):
            return returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
