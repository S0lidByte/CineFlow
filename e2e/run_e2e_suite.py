"""Deterministic full-stack E2E smoke verification suite.

Verifies:
1. Functional stack readiness (Backend, Frontend)
2. Deterministic Settings UI / API probe (AllDebrid against fixture)
3. Plex webhook ingestion endpoint response
"""

from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

PROJECT_NAME = "cineflow-e2e"
COMPOSE_FILE = "docker-compose.e2e.yml"
BACKEND_BASE = "http://127.0.0.1:18080"
FRONTEND_BASE = "http://127.0.0.1:3000"
FIXTURE_BASE = "http://127.0.0.1:8765"
API_KEY = "0123456789abcdef0123456789abcdef"
ACTOR_CONTEXT_SECRET = "e2e-disposable-actor-context-secret"  # noqa: S105


def _validate_safe_http_url(url: str) -> str:
    """Validate that the target URL uses http or https scheme before urllib access."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Disallowed URL scheme for test harness: {parsed.scheme!r}")
    return url


def log(msg: str) -> None:
    print(f"[E2E-RUNNER] {msg}", flush=True)


def get_signed_headers(roles: str = "platform:admin,settings:write") -> dict[str, str]:
    actor_id = "e2e-admin"
    actor_client = "cineflow-e2e-runner"
    timestamp = str(int(time.time()))
    payload = json.dumps(
        {
            "actor_id": actor_id,
            "actor_roles": roles,
            "actor_client": actor_client,
            "actor_timestamp": timestamp,
        },
        separators=(",", ":"),
    ).encode()
    signature = hmac.new(
        ACTOR_CONTEXT_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    return {
        "x-api-key": API_KEY,
        "x-actor-id": actor_id,
        "x-actor-roles": roles,
        "x-actor-client": actor_client,
        "x-actor-timestamp": timestamp,
        "x-actor-signature": signature,
        "Content-Type": "application/json",
    }


def run_cmd(cmd: list[str], cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        log(f"Command failed ({res.returncode}): {' '.join(cmd)}")
        if res.stderr:
            log(f"stderr: {res.stderr.strip()[:300]}")
    return res


def wait_for_http(
    url: str, timeout: int = 60, expected_codes: tuple[int, ...] = (200, 302, 307)
) -> bool:
    safe_url = _validate_safe_http_url(url)
    start = time.time()
    while time.time() - start < timeout:
        try:
            req = urllib.request.Request(  # noqa: S310
                safe_url, headers={"User-Agent": "CineFlowE2E"}
            )
            with urllib.request.urlopen(req, timeout=3) as resp:  # noqa: S310
                status_code: int | None = getattr(resp, "status", None)
                if status_code in expected_codes:
                    return True
        except urllib.error.HTTPError as e:
            error_code: int | None = getattr(e, "code", None)
            if error_code in expected_codes:
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def test_settings_connection_probes() -> dict[str, Any]:
    url = _validate_safe_http_url(
        f"{BACKEND_BASE}/api/v1/settings/test-connection/all_debrid"
    )
    headers = get_signed_headers("platform:admin,settings:write")
    req = urllib.request.Request(url, headers=headers, method="POST")  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            data: object = json.loads(
                resp.read().decode()
            )  # pyright: ignore[reportAny]
            if isinstance(data, dict):
                res: dict[str, Any] = {}
                for k, v in data.items():  # pyright: ignore[reportUnknownVariableType]
                    res[str(k)] = v  # pyright: ignore[reportUnknownArgumentType]
                return res
            return {"ok": False, "error": "Invalid format"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def test_plex_webhook_attribution() -> dict[str, Any]:
    url = _validate_safe_http_url(f"{BACKEND_BASE}/api/v1/webhook/plex")
    payload = {
        "event": "media.play",
        "Account": {"title": "E2ETester"},
        "Player": {"title": "E2EPlayerDevice"},
        "Metadata": {
            "type": "movie",
            "title": "Deterministic Test Movie",
            "file": "/data/Deterministic.Test.Movie.2024.mkv",
        },
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(  # noqa: S310
        url,
        data=data,
        headers={"x-api-key": API_KEY, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            status_val: int = getattr(resp, "status", 200)
            return {"status": status_val, "body": resp.read().decode()}
    except Exception as e:
        status_val = getattr(e, "code", 500)
        return {"status": status_val, "error": str(e)}


def main() -> int:
    log("Starting deterministic full-stack E2E gate execution")

    # 1. Health check stack endpoints
    backend_ok = wait_for_http(f"{BACKEND_BASE}/docs", timeout=30)
    frontend_ok = wait_for_http(f"{FRONTEND_BASE}/auth/login", timeout=30)

    log(f"Backend HTTP health: {backend_ok}")
    log(f"Frontend HTTP health: {frontend_ok}")

    # 2. Connection probe test
    probe_res: dict[str, Any] = test_settings_connection_probes()
    log(f"AllDebrid probe response: {probe_res}")

    # 3. Plex webhook test
    webhook_res: dict[str, Any] = test_plex_webhook_attribution()
    log(f"Plex webhook response: {webhook_res}")

    summary: dict[str, Any] = {
        "backend_ready": backend_ok,
        "frontend_ready": frontend_ok,
        "alldebrid_probe": probe_res,
        "plex_webhook": webhook_res,
    }
    print(json.dumps(summary, indent=2))

    failures: list[str] = []
    if not backend_ok:
        failures.append("Backend HTTP healthcheck failed")
    if not frontend_ok:
        failures.append("Frontend HTTP healthcheck failed")
    if not probe_res.get("ok"):
        failures.append(f"AllDebrid probe returned non-ok result: {probe_res}")
    if webhook_res.get("status") != 200:
        failures.append(f"Plex webhook returned non-200 status: {webhook_res}")

    if failures:
        for failure in failures:
            log(f"FAIL: {failure}")
        return 1

    log("SUCCESS: All deterministic smoke checks passed cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
