"""Time one 1 MiB CDN sample without writing data or exposing the signed URL.

Run inside the same network namespace as CineFlow. For example, in Bash on the
VPS with this source checkout, run the script using the running backend's Python:

    docker exec -it riven python -c "$(< scripts/stream_route_probe.py)" --offset 2820931584

To avoid copying a signed link, add --stored-item-id ITEM_ID. The probe resolves
the settings file as CineFlow does under /riven/data, or accepts --settings-file
for a different mount. It selects only a persisted Real-Debrid URL in a read-only
database transaction and never refreshes the URL.

Default mode matches a primary HTTP/1.1 range. Add --bounded to model a
speculative worker's 1 MiB range. Share the JSON output only, never the URL.
The probe honors HTTPX's environment proxy/CA settings like the backend but
does not follow redirects; use the final signed Real-Debrid URL.
Times are cumulative from request start; subtract first_body_s from
sample_complete_s to see how long the rest of the sampled body took.

Use --resources-only instead of --offset to sample cache free space and
cgroup CPU counters over five seconds without a URL or a network request.
"""

import argparse
import getpass
import json
import os
import re
import shutil
import warnings
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit

import httpx
import trio

SAMPLE_BYTES = 1024 * 1024
TIME_LIMIT = 25
RESOURCE_SAMPLE_SECONDS = 5
CPU_STAT = Path("/sys/fs/cgroup/cpu.stat")
CPU_FIELDS = {"usage_usec", "nr_periods", "nr_throttled", "throttled_usec"}
CACHE_PATHS = ("/mnt/cache", "/dev/shm/riven-cache")
SETTINGS_DIR = Path("/riven/data")


class StoredLinkError(Exception):
    """Known, safe-to-report lookup failures."""


def stored_link_settings_file() -> Path:
    for key in ("CINEFLOW_SETTINGS_FILENAME", "SETTINGS_FILENAME"):
        name = os.getenv(key, "").strip()
        if name:
            return SETTINGS_DIR / name
    current = SETTINGS_DIR / "cineflow.json"
    return current if current.exists() else SETTINGS_DIR / "settings.json"


def read_stored_link(settings_file: Path, item_id: int) -> str:
    """Read exactly one existing Real-Debrid link without importing the app."""
    import psycopg2

    try:
        dsn = json.loads(settings_file.read_text(encoding="utf-8"))["database"]["host"]
        forced = os.getenv("CINEFLOW_FORCE_ENV", os.getenv("RIVEN_FORCE_ENV", ""))
        if forced.strip().lower() == "true":
            dsn = os.getenv(
                "CINEFLOW_DATABASE_HOST", os.getenv("RIVEN_DATABASE_HOST", dsn)
            )
        if not isinstance(dsn, str):
            raise TypeError
        dsn = dsn.replace("postgresql+psycopg2://", "postgresql://", 1)
        if not dsn.startswith("postgresql://"):
            raise ValueError
    except (OSError, KeyError, TypeError, ValueError):
        raise StoredLinkError("settings_database_unavailable") from None

    try:
        conn = psycopg2.connect(dsn, connect_timeout=5)
        try:
            conn.set_session(readonly=True, autocommit=False)
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '3s'")
                cur.execute(
                    """SELECT media.unrestricted_url
                       FROM "MediaEntry" AS media
                       JOIN "FilesystemEntry" AS fs ON fs.id = media.id
                       WHERE fs.media_item_id = %s AND media.provider = 'realdebrid'
                       LIMIT 2""",
                    (item_id,),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
    except psycopg2.Error:
        raise StoredLinkError("stored_link_lookup_failed") from None

    if len(rows) > 1:
        raise StoredLinkError("ambiguous_stored_links")
    if not rows or not rows[0][0]:
        raise StoredLinkError("no_stored_link")
    return rows[0][0]


def cpu_counters() -> dict[str, int]:
    """Read the container's cgroup v2 counters; do not guess when unavailable."""
    try:
        entries = CPU_STAT.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError):
        return {}
    counters: dict[str, int] = {}
    for entry in entries:
        key, _, value = entry.partition(" ")
        if key in CPU_FIELDS and value.isdecimal():
            counters[key] = int(value)
    return counters


def cache_free_bytes() -> dict[str, int | None]:
    free: dict[str, int | None] = {}
    for path in CACHE_PATHS:
        try:
            free[path] = shutil.disk_usage(path).free
        except OSError:
            free[path] = None
    return free


async def sample_with_resources(
    fetch: Callable[[], Awaitable[dict[str, int | float | str]]] | None = None,
) -> dict[str, object]:
    """Sample the entire cgroup, not just this process or the streaming thread."""
    started_at = datetime.now(UTC).isoformat(timespec="milliseconds")
    start = trio.current_time()
    before = cpu_counters()
    report: dict[str, object]
    if fetch is None:
        await trio.sleep(RESOURCE_SAMPLE_SECONDS)
        report = {"result": "ok"}  # Resources-only mode makes no HTTP request.
    else:
        report = dict(await fetch())
    after = cpu_counters()
    report["resources"] = {
        "started_at_utc": started_at,
        "window_s": round(trio.current_time() - start, 6),
        "cache_free_bytes": cache_free_bytes(),
        "cgroup_cpu_delta": {
            key: after[key] - before[key]
            for key in before.keys() & after.keys()
            if after[key] >= before[key]
        }
        or None,
    }
    return report


async def probe(
    url: str,
    offset: int,
    bounded: bool,
    *,
    allow_loopback: bool = False,
    trust_env: bool = True,
) -> dict[str, int | float | str]:
    """Report timing only; never log request data, headers, or exception text."""
    result: dict[str, int | float | str] = {
        "offset": offset,
        "range_mode": "bounded" if bounded else "open-ended",
        "sample_bytes": 0,
    }
    try:
        parsed = urlsplit(url)
        loopback = allow_loopback and parsed.hostname in {"127.0.0.1", "::1"}
        if (
            offset < 0
            or not parsed.hostname
            or not (parsed.scheme == "https" or loopback and parsed.scheme == "http")
            or parsed.username is not None
            or any(ord(char) < 32 for char in url)
        ):
            raise ValueError
    except ValueError:
        return result | {"result": "invalid_input"}

    start = monotonic()
    end = offset + SAMPLE_BYTES - 1
    http_range = f"bytes={offset}-{end if bounded else ''}"
    milestones = {
        "connection.connect_tcp.complete": "tcp_ready_s",
        "connection.start_tls.complete": "tls_ready_s",
    }

    def mark(field: str) -> None:
        result[field] = round(monotonic() - start, 6)

    async def trace(name: str, _details: object) -> None:
        if field := milestones.get(name):
            mark(field)

    try:
        with trio.fail_after(TIME_LIMIT):
            async with httpx.AsyncClient(
                http2=False,
                trust_env=trust_env,
                follow_redirects=False,
                timeout=TIME_LIMIT,
            ) as client:
                async with client.stream(
                    "GET",
                    url,
                    headers={"Range": http_range, "Accept-Encoding": "identity"},
                    extensions={"trace": trace},
                ) as response:
                    mark("headers_s")
                    result["http_status"] = response.status_code
                    content_range = re.fullmatch(
                        r"bytes (\d+)-(\d+)/(\d+)",
                        response.headers.get("Content-Range", ""),
                    )
                    if response.status_code != 206 or content_range is None:
                        return result | {"result": "invalid_range_response"}
                    first, last, total = map(int, content_range.groups())
                    if (
                        first != offset
                        or not end <= last < total
                        or (bounded and last != end)
                        or response.headers.get("Content-Encoding", "identity").lower()
                        != "identity"
                    ):
                        return result | {"result": "invalid_range_response"}

                    received = 0
                    async for part in response.aiter_raw():
                        if not part:
                            continue
                        if received == 0:
                            mark("first_body_s")
                        received = min(SAMPLE_BYTES, received + len(part))
                        result["sample_bytes"] = received
                        if received == SAMPLE_BYTES:
                            mark("sample_complete_s")
                            return result | {"result": "ok"}
                    return result | {"result": "truncated_body"}
    except (trio.TooSlowError, httpx.TimeoutException):
        return result | {"result": "timeout"}
    except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
        return result | {"result": type(exc).__name__}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--offset", type=int)
    mode.add_argument("--resources-only", action="store_true")
    parser.add_argument("--bounded", action="store_true")
    parser.add_argument("--stored-item-id", type=int)
    parser.add_argument("--settings-file", type=Path)
    args = parser.parse_args()
    if args.settings_file is not None and args.stored_item_id is None:
        parser.error("--settings-file requires --stored-item-id")
    if args.stored_item_id is not None and (
        args.stored_item_id < 1 or args.offset is None
    ):
        parser.error("--stored-item-id requires a positive item ID and --offset")
    offset = args.offset
    report: dict[str, object]
    if offset is None:
        if args.bounded:
            parser.error("--bounded requires --offset")
        report = trio.run(sample_with_resources)
    else:
        try:
            if args.stored_item_id is not None:
                url = read_stored_link(
                    args.settings_file or stored_link_settings_file(),
                    args.stored_item_id,
                )
            else:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", getpass.GetPassWarning)
                    url = getpass.getpass(
                        "Signed CDN URL (hidden; do not paste it in chat): "
                    )

            async def fetch():
                return await probe(url.strip(), offset, args.bounded)

            report = trio.run(sample_with_resources, fetch)
        except (EOFError, getpass.GetPassWarning):
            report = {"result": "interactive_terminal_required"}
        except StoredLinkError as exc:
            report = {"result": str(exc)}
    print(json.dumps(report, sort_keys=True))
    return 0 if report["result"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
