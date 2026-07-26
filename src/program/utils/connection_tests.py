"""Thin Settings connection probes for third-party integrations.

Each probe uses saved settings only, enforces a hard ≤5s wall timeout, and
returns safe messages that never include API keys, tokens, or passwords.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from typing import Literal
from xmlrpc.client import ServerProxy

import httpx
from loguru import logger
from pydantic import BaseModel, Field

from program.settings import settings_manager

PROBE_TIMEOUT_SECONDS = 5.0

ConnectionService = Literal[
    "real_debrid",
    "plex",
    "jackett",
    "prowlarr",
    "opensubtitles",
    "subdl",
]

SUPPORTED_SERVICES: tuple[ConnectionService, ...] = (
    "real_debrid",
    "plex",
    "jackett",
    "prowlarr",
    "opensubtitles",
    "subdl",
)


class ConnectionTestResponse(BaseModel):
    """Result of a Settings connection probe. Never includes secrets."""

    ok: bool = Field(description="Whether the probe succeeded")
    latency_ms: int = Field(
        description="Wall-clock probe latency in milliseconds",
        ge=0,
    )
    message: str = Field(
        description="Safe human-readable status (no credentials or secret URLs)"
    )


_SECRET_MARKERS = (
    "api_key",
    "apikey",
    "api-key",
    "token=",
    "password",
    "authorization",
    "bearer ",
    "x-api-key",
    "x-plex-token",
)


def _safe_message(raw: str, fallback: str = "Connection failed") -> str:
    """Return a user-facing message that cannot contain secrets."""
    text = (raw or "").strip()
    if not text:
        return fallback
    lowered = text.lower()
    if any(marker in lowered for marker in _SECRET_MARKERS):
        return fallback
    # Keep messages short and free of query strings / long URLs.
    if "?" in text or "://" in text:
        return fallback
    if len(text) > 160:
        return fallback
    return text


def _latency_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def _fail(started: float, message: str) -> ConnectionTestResponse:
    return ConnectionTestResponse(
        ok=False,
        latency_ms=_latency_ms(started),
        message=_safe_message(message),
    )


def _ok(started: float, message: str) -> ConnectionTestResponse:
    return ConnectionTestResponse(
        ok=True,
        latency_ms=_latency_ms(started),
        message=_safe_message(message, fallback="OK"),
    )


def _run_with_timeout(
    probe: Callable[[], ConnectionTestResponse],
) -> ConnectionTestResponse:
    """Run a sync probe with a hard wall-clock timeout."""
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(probe)
        try:
            return future.result(timeout=PROBE_TIMEOUT_SECONDS)
        except FuturesTimeout:
            future.cancel()
            return _fail(started, "Timed out")
        except Exception as exc:
            logger.debug(f"Connection probe failed: {type(exc).__name__}")
            return _fail(started, "Connection failed")


def _httpx_timeout() -> httpx.Timeout:
    return httpx.Timeout(PROBE_TIMEOUT_SECONDS)


def _probe_real_debrid() -> ConnectionTestResponse:
    started = time.perf_counter()
    settings = settings_manager.settings.downloaders.real_debrid
    api_key = (settings.api_key or "").strip()
    if not api_key:
        return _fail(started, "API key not configured")

    proxy_url = (settings_manager.settings.downloaders.proxy_url or "").strip() or None

    try:
        with httpx.Client(
            base_url="https://api.real-debrid.com/rest/1.0",
            timeout=_httpx_timeout(),
            proxy=proxy_url,
            follow_redirects=True,
        ) as client:
            response = client.get(
                "/user",
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except httpx.TimeoutException:
        return _fail(started, "Timed out")
    except httpx.HTTPError:
        return _fail(started, "Connection failed")

    if response.status_code == 401:
        return _fail(started, "Unauthorized")
    if response.status_code == 403:
        return _fail(started, "Forbidden")
    if response.status_code >= 400:
        return _fail(started, f"HTTP {response.status_code}")

    try:
        payload = response.json()
    except ValueError:
        return _fail(started, "Invalid response")

    username = payload.get("username") if isinstance(payload, dict) else None
    if isinstance(username, str) and username.strip():
        return _ok(started, f"Connected as {username.strip()}")
    return _ok(started, "Connected")


def _probe_plex() -> ConnectionTestResponse:
    started = time.perf_counter()
    settings = settings_manager.settings.updaters.plex
    token = (settings.token or "").strip()
    url = (settings.url or "").strip().rstrip("/")
    if not token:
        return _fail(started, "Token not configured")
    if not url:
        return _fail(started, "URL not configured")

    try:
        with httpx.Client(timeout=_httpx_timeout(), follow_redirects=True) as client:
            response = client.get(
                f"{url}/identity",
                headers={"X-Plex-Token": token, "Accept": "application/json"},
            )
    except httpx.TimeoutException:
        return _fail(started, "Timed out")
    except httpx.HTTPError:
        return _fail(started, "Connection failed")

    if response.status_code in (401, 403):
        return _fail(started, "Unauthorized")
    if response.status_code >= 400:
        return _fail(started, f"HTTP {response.status_code}")
    return _ok(started, "Connected to Plex server")


def _probe_jackett() -> ConnectionTestResponse:
    started = time.perf_counter()
    settings = settings_manager.settings.scraping.jackett
    url = (settings.url or "").strip().rstrip("/")
    api_key = (settings.api_key or "").strip()
    if not url:
        return _fail(started, "URL not configured")
    if not api_key:
        return _fail(started, "API key not configured")

    try:
        with httpx.Client(
            base_url=f"{url}/api/v2.0",
            timeout=_httpx_timeout(),
            follow_redirects=True,
        ) as client:
            response = client.get(
                "/server/config",
                params={"apikey": api_key},
            )
    except httpx.TimeoutException:
        return _fail(started, "Timed out")
    except httpx.HTTPError:
        return _fail(started, "Connection failed")

    if response.status_code in (401, 403):
        return _fail(started, "Unauthorized")
    if response.status_code >= 400:
        return _fail(started, f"HTTP {response.status_code}")
    return _ok(started, "Connected to Jackett")


def _probe_prowlarr() -> ConnectionTestResponse:
    started = time.perf_counter()
    settings = settings_manager.settings.scraping.prowlarr
    url = (settings.url or "").strip().rstrip("/")
    api_key = (settings.api_key or "").strip()
    if not url:
        return _fail(started, "URL not configured")
    if not api_key:
        return _fail(started, "API key not configured")

    try:
        with httpx.Client(
            base_url=url,
            timeout=_httpx_timeout(),
            follow_redirects=True,
        ) as client:
            response = client.get(
                "/ping",
                headers={"X-Api-Key": api_key},
            )
            # Some installs expose ping only under /api; fall back to system status.
            if response.status_code == 404:
                response = client.get(
                    "/api/v1/system/status",
                    headers={"X-Api-Key": api_key},
                )
    except httpx.TimeoutException:
        return _fail(started, "Timed out")
    except httpx.HTTPError:
        return _fail(started, "Connection failed")

    if response.status_code in (401, 403):
        return _fail(started, "Unauthorized")
    if response.status_code >= 400:
        return _fail(started, f"HTTP {response.status_code}")
    return _ok(started, "Connected to Prowlarr")


def _probe_opensubtitles() -> ConnectionTestResponse:
    started = time.perf_counter()
    settings = settings_manager.settings.post_processing.subtitle.providers.opensubtitles
    username = (settings.username or "").strip()
    password = settings.password or ""
    user_agent = (settings.user_agent or "").strip() or "VLSub 0.11.1"
    allow_anonymous = bool(settings.allow_anonymous)

    if bool(username) != bool(password.strip()):
        return _fail(started, "Incomplete credentials")
    if not username and not allow_anonymous:
        return _fail(started, "Credentials not configured")

    try:
        server = ServerProxy(
            "https://api.opensubtitles.org/xml-rpc",
            allow_none=True,
        )
        # xmlrpc has no native timeout; wall timeout is enforced by _run_with_timeout.
        result = server.LogIn(username, password, "eng", user_agent)
    except Exception:
        return _fail(started, "Connection failed")

    if not isinstance(result, dict):
        return _fail(started, "Invalid response")

    status = str(result.get("status", ""))
    if status.startswith("401") or "Unauthorized" in status:
        return _fail(started, "Unauthorized")
    if not status.startswith("200"):
        return _fail(started, "Login failed")
    if not result.get("token"):
        return _fail(started, "Login failed")

    if username:
        return _ok(started, "Authenticated")
    return _ok(started, "Anonymous login OK")


def _probe_subdl() -> ConnectionTestResponse:
    started = time.perf_counter()
    settings = settings_manager.settings.post_processing.subtitle.providers.subdl
    api_key = (settings.api_key or "").strip()
    if not api_key:
        return _fail(started, "API key not configured")

    try:
        with httpx.Client(
            base_url="https://api.subdl.com/api/v1/",
            timeout=_httpx_timeout(),
            follow_redirects=True,
        ) as client:
            # Minimal authenticated request; empty/missing results still prove auth.
            response = client.get(
                "subtitles",
                params={
                    "api_key": api_key,
                    "type": "movie",
                    "tmdb_id": "550",
                    "languages": "en",
                    "subs_per_page": "1",
                },
            )
    except httpx.TimeoutException:
        return _fail(started, "Timed out")
    except httpx.HTTPError:
        return _fail(started, "Connection failed")

    if response.status_code in (401, 403):
        return _fail(started, "Unauthorized")
    if response.status_code >= 400:
        return _fail(started, f"HTTP {response.status_code}")

    try:
        payload = response.json()
    except ValueError:
        return _fail(started, "Invalid response")

    if isinstance(payload, dict):
        status = payload.get("status")
        if status is False:
            err = payload.get("error") or payload.get("message") or "Unauthorized"
            return _fail(started, str(err) if isinstance(err, str) else "Unauthorized")

    return _ok(started, "Connected to SubDL")


_PROBES: dict[ConnectionService, Callable[[], ConnectionTestResponse]] = {
    "real_debrid": _probe_real_debrid,
    "plex": _probe_plex,
    "jackett": _probe_jackett,
    "prowlarr": _probe_prowlarr,
    "opensubtitles": _probe_opensubtitles,
    "subdl": _probe_subdl,
}


def run_connection_test(service: ConnectionService) -> ConnectionTestResponse:
    """Execute a connection probe for ``service`` with a hard ≤5s timeout."""
    probe = _PROBES[service]
    return _run_with_timeout(probe)
