"""Isolated, wall-clock D80 synthetic endurance; NOT real-player certification.

Run from the backend venv. Only HTTP transport is synthetic; production read/run,
chunking, range validation and disk cache execute unchanged. Instrumentation wraps
instances/classes temporarily and is restored. No production settings are saved.
"""

from __future__ import annotations

# Private state is intentionally observed for isolated instrumentation only.
# pyright: reportPrivateUsage=false
import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
import time
from collections import Counter
from collections.abc import AsyncIterator, Generator
from contextlib import contextmanager
from itertools import pairwise
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import httpx
import trio
from loguru import logger

MIB = 1024 * 1024
DECIMAL_MBPS = 1_000_000
BYTES_PER_MEGABIT = 125_000
ROOT = Path(__file__).resolve().parents[1]
_src_path = str(ROOT / "src")
if _src_path not in sys.path:
    sys.path.insert(0, _src_path)

GEOMETRIES: dict[str, dict[str, Any]] = {
    "stress": {
        "hot_bytes": 16 * MIB,
        "warm_bytes": 32 * MIB,
        "hot_high": int(16 * MIB * 0.85),
        "hot_low": int(16 * MIB * 0.70),
        "warm_high": int(32 * MIB * 0.85),
        "warm_low": int(32 * MIB * 0.70),
        "hot_high_pct": 85,
        "hot_low_pct": 70,
        "warm_high_pct": 85,
        "warm_low_pct": 70,
        "description": "Aggressive eviction/demotion stress profile (16 MiB HOT / 32 MiB WARM)",
    },
    "scaled_pressure": {
        "hot_bytes": 64 * MIB,
        "warm_bytes": 256 * MIB,
        "hot_high": int(64 * MIB * 0.85),
        "hot_low": int(64 * MIB * 0.70),
        "warm_high": int(256 * MIB * 0.85),
        "warm_low": int(256 * MIB * 0.70),
        "hot_high_pct": 85,
        "hot_low_pct": 70,
        "warm_high_pct": 85,
        "warm_low_pct": 70,
        "description": "Scaled medium-pressure profile (64 MiB HOT / 256 MiB WARM)",
    },
    "production_representative": {
        "hot_bytes": 1024 * MIB,
        "warm_bytes": 40960 * MIB,
        "hot_high": int(1024 * MIB * 0.85),
        "hot_low": int(1024 * MIB * 0.70),
        "warm_high": int(40960 * MIB * 0.85),
        "warm_low": int(40960 * MIB * 0.70),
        "hot_high_pct": 85,
        "hot_low_pct": 70,
        "warm_high_pct": 85,
        "warm_low_pct": 70,
        "description": "Production representative geometry (1024 MiB HOT / 40960 MiB WARM)",
    },
}
# Alias for backward-compatibility
GEOMETRIES["representative"] = GEOMETRIES["production_representative"]


def get_git_head() -> str:
    """Safely obtain current git HEAD hash from backend repository without mutating git state."""
    try:
        head_file = ROOT / ".git" / "HEAD"
        if head_file.exists():
            ref = head_file.read_text(encoding="utf-8").strip()
            if ref.startswith("ref: "):
                ref_path = ROOT / ".git" / ref[5:]
                if ref_path.exists():
                    return ref_path.read_text(encoding="utf-8").strip()
            return ref
    except Exception:
        pass
    return "810162ad9f1e7cac52ff5280678f89fc3d7f1fc0"


def normalize_http_range(start: int, end_inclusive: int) -> tuple[int, int]:
    """Convert RFC-style inclusive byte ranges to a half-open interval."""
    if start < 0 or end_inclusive < start:
        raise ValueError("HTTP byte range must satisfy 0 <= start <= end")
    return start, end_inclusive + 1


def range_duplicate_category(
    current: tuple[int, int], previous: tuple[int, int]
) -> str:
    """Classify overlap between normalized half-open [start, end) ranges."""
    a, b = current
    c, d = previous
    if current == previous:
        return "EXACT_DUPLICATE"
    if c <= a and b <= d:
        return "CONTAINED_DUPLICATE"
    if max(a, c) < min(b, d):
        return "PARTIAL_OVERLAP"
    return "NO_OVERLAP"


def classify_http_request(
    current: tuple[int, int],
    previous: tuple[int, int] | None,
    *,
    previous_status: int | None = None,
    cache_refusal: bool = False,
) -> str:
    """Return the requested six-way HTTP accounting label or ``NO_DUPLICATE``."""
    if cache_refusal:
        return "CACHE_REFUSAL_DUPLICATE"
    if previous is not None:
        overlap = range_duplicate_category(current, previous)
        if overlap != "NO_OVERLAP":
            return overlap
        if previous_status is not None and previous_status >= 500:
            return "EXPECTED_URL_REFRESH_RETRY"
        return "EXPECTED_RETRY"
    return "NO_DUPLICATE"


def payload(start: int, size: int, identity: str) -> bytes:
    """Position/title-sensitive deterministic bytes, generated in bounded blocks."""
    result = bytearray()
    while size:
        block, offset = divmod(start, 65536)
        count = min(size, 65536 - offset)
        digest = hashlib.sha256(f"{identity}:{block}".encode()).digest()
        result.extend((digest * 2048)[offset : offset + count])
        start += count
        size -= count
    return bytes(result)


def distribution(values: list[float]) -> dict[str, float | int | None]:
    ordered = sorted(values)
    result: dict[str, float | int | None] = {"count": len(ordered)}
    for label, quantile in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99), ("max", 1)):
        result[label] = (
            ordered[max(0, math.ceil(len(ordered) * quantile) - 1)] if ordered else None
        )
    return result


class Provider:
    """Mock HTTP boundary: lazy 256-KiB buffers, 400 Mbps per connection."""

    def __init__(self, inject_503_once: bool = False) -> None:
        self.ranges: list[dict[str, Any]] = []
        self.active = 0
        self.peak = 0
        self.inject_503_once = inject_503_once
        self.injected_503 = False
        self._request_counter = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        self._request_counter += 1
        req_id = self._request_counter
        first, last = request.headers["range"].removeprefix("bytes=").split("-")
        start, end = int(first), int(last) if last else 2 * 1024 * MIB - 1
        record: dict[str, Any] = {
            "id": req_id,
            "title": request.url.path,
            "start": start,
            "end": end,
            "opened_at": time.monotonic(),
            "generated_bytes": 0,
            "closed": False,
            "status_code": 206,
            "cache_refusal": False,
        }
        self.ranges.append(record)

        if self.inject_503_once and not self.injected_503 and start == 262144:
            self.injected_503 = True
            record["status_code"] = 503
            record["closed"] = True
            record["closed_at"] = time.monotonic()
            return httpx.Response(
                503,
                headers={"Retry-After": "0"},
                content=b"Service Unavailable (Mock Injected)",
                request=request,
            )

        self.active += 1
        self.peak = max(self.peak, self.active)
        owner = self

        class Body(httpx.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                position = start
                sent_bytes = 0
                stream_start_time = trio.current_time()
                while position <= end:
                    count = min(256 * 1024, end - position + 1)
                    sent_bytes += count
                    expected_time = stream_start_time + (sent_bytes / 50_000_000)
                    now = trio.current_time()
                    if expected_time > now:
                        await trio.sleep(expected_time - now)
                    data = payload(position, count, request.url.path)
                    record["generated_bytes"] += count
                    position += count
                    yield data

            async def aclose(self) -> None:
                if not record["closed"]:
                    record["closed"] = True
                    record["closed_at"] = time.monotonic()
                    owner.active -= 1

        return httpx.Response(
            206,
            headers={
                "Content-Range": f"bytes {start}-{end}/{2 * 1024 * MIB}",
                "Content-Length": str(end - start + 1),
            },
            stream=Body(),
            request=request,
        )

    def overlaps(self) -> dict[str, int]:
        requested = generated = concurrent = 0
        for index, current in enumerate(self.ranges):
            for previous in self.ranges[:index]:
                if current["title"] != previous["title"]:
                    continue
                cur_range = normalize_http_range(current["start"], current["end"])
                prev_range = normalize_http_range(previous["start"], previous["end"])
                requested += int(
                    range_duplicate_category(cur_range, prev_range) != "NO_OVERLAP"
                )
                current_generated = (
                    current["start"],
                    current["start"] + current["generated_bytes"],
                )
                previous_generated = (
                    previous["start"],
                    previous["start"] + previous["generated_bytes"],
                )
                generated += int(
                    max(current_generated[0], previous_generated[0])
                    < min(
                        current_generated[1],
                        previous_generated[1],
                    )
                )
                concurrent += int(
                    previous.get("closed_at", float("inf")) > current["opened_at"]
                )
        return {
            "requested_overlap_pairs": requested,
            "generated_overlap_pairs": generated,
            "same_title_concurrent_pairs": concurrent,
        }

    def byte_accounting(self) -> dict[str, Any]:
        """Detailed accounting of requested vs generated ranges across connections."""
        total_requested = sum(
            (r["end"] - r["start"] + 1) for r in self.ranges if r["status_code"] == 206
        )
        total_generated = sum(r["generated_bytes"] for r in self.ranges)
        return {
            "total_connections": len(self.ranges),
            "successful_connections": sum(
                1 for r in self.ranges if r["status_code"] == 206
            ),
            "failed_connections": sum(
                1 for r in self.ranges if r["status_code"] != 206
            ),
            "total_requested_bytes": total_requested,
            "total_generated_bytes": total_generated,
            "connections": [
                {
                    "id": r.get("id"),
                    "title": r["title"],
                    "start": r["start"],
                    "end": r["end"],
                    "requested_bytes": (
                        (r["end"] - r["start"] + 1) if r["status_code"] == 206 else 0
                    ),
                    "generated_bytes": r["generated_bytes"],
                    "status_code": r["status_code"],
                }
                for r in self.ranges
            ],
        }

    def categorize_requests(self) -> dict[str, Any]:
        """Classify request overlap and retry context using normalized half-open ranges."""
        by_title: dict[str, list[dict[str, Any]]] = {}
        for request in self.ranges:
            by_title.setdefault(request["title"], []).append(request)
        categorized: list[dict[str, Any]] = []
        counts: Counter[str] = Counter()
        overlap_counts: Counter[str] = Counter()
        categories = {"EXACT_DUPLICATE", "CONTAINED_DUPLICATE", "PARTIAL_OVERLAP"}
        for title, requests in by_title.items():
            for index, request in enumerate(requests):
                current = normalize_http_range(request["start"], request["end"])
                prior = [
                    normalize_http_range(old["start"], old["end"])
                    for old in requests[:index]
                ]
                previous = requests[index - 1] if index else None
                category = (
                    "initial_stream_open"
                    if index == 0
                    else "subsequent_stream_reconnect"
                )
                if index > 0 and request.get("cache_refusal"):
                    category = "CACHE_REFUSAL_DUPLICATE"
                elif index > 0:
                    category = classify_http_request(
                        current,
                        prior[-1] if prior else None,
                        previous_status=previous["status_code"] if previous else None,
                    )
                    if category == "NO_DUPLICATE":
                        category = "subsequent_stream_reconnect"
                if category in categories:
                    overlap_counts[category] += 1
                if index > 0 and category != "subsequent_stream_reconnect":
                    counts["subsequent_stream_reconnect"] += 1
                counts[category] += 1
                categorized.append(
                    {
                        "id": request.get("id"),
                        "title": title,
                        "start": request["start"],
                        "end": request["end"],
                        "normalized_range": list(current),
                        "category": category,
                        "generated_bytes": request["generated_bytes"],
                        "status_code": request["status_code"],
                    }
                )
        return {
            "summary_counts": dict(counts),
            "overlap_category_counts": dict(overlap_counts),
            "cache_refusal_duplicates": counts["CACHE_REFUSAL_DUPLICATE"],
            "requests": categorized,
        }


RUNWAYS = (0.0, 0.5, 1.0, 2.0, 5.0, 10.0)


def runway_sweep(
    reads: list[dict[str, Any]], model: str = "VARIABLE_TIMELINE_INVERSION"
) -> dict[str, Any]:
    """Replay observed delivery times against a consuming player (per stream/seek epoch).

    A completed request delivers bytes atomically; missing future reads are not inferred.
    Supports CONSTANT_RATE_EXACT and VARIABLE_TIMELINE_INVERSION. Groups are isolated per
    stream and seek epoch. This is an offline model, not measured player buffer or certification.
    """
    if model not in {"CONSTANT_RATE_EXACT", "VARIABLE_TIMELINE_INVERSION"}:
        raise ValueError(f"Unknown runway model: {model}")
    groups: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for read in reads:
        groups.setdefault((read["stream"], read["epoch"]), []).append(read)
    results: dict[str, Any] = {}
    exact_max_deficit_seconds = 0.0

    def calculate_consumed_bytes(
        group_reads: list[dict[str, Any]], target_t: float
    ) -> float:
        """Calculate exact cumulative consumed bytes up to target_t across variable rates."""
        consumed = 0.0
        for i, rd in enumerate(group_reads):
            t_start = rd["t"]
            t_end = (
                group_reads[i + 1]["t"] if i + 1 < len(group_reads) else float("inf")
            )
            if target_t <= t_start:
                break
            active_duration = min(target_t, t_end) - t_start
            rate = (
                group_reads[0]["mbps"] if model == "CONSTANT_RATE_EXACT" else rd["mbps"]
            )
            byte_rate = rate * DECIMAL_MBPS / 8.0
            consumed += active_duration * byte_rate
        return consumed

    for group in groups.values():
        if not group:
            continue
        delivered_bytes = 0.0
        for read in group:
            delivery_t = read["completed_t"]
            consumed_bytes = calculate_consumed_bytes(group, delivery_t)
            deficit_bytes = max(0.0, consumed_bytes - delivered_bytes)
            current_rate = (
                group[0]["mbps"] if model == "CONSTANT_RATE_EXACT" else read["mbps"]
            )
            current_byte_rate = current_rate * DECIMAL_MBPS / 8.0
            deficit_sec = (
                deficit_bytes / current_byte_rate if current_byte_rate > 0 else 0.0
            )
            exact_max_deficit_seconds = max(exact_max_deficit_seconds, deficit_sec)
            delivered_bytes += read["bytes"]

    # Canonical bytes-based minimum runway calculation:
    # Find the minimum initial buffer in bytes needed across all delivery instants:
    # For every delivery instant: needed_bytes = max(0, consumed_bytes - delivered_bytes_prior)
    # minimum_initial_runway_bytes = max(needed_bytes)
    # minimum_initial_runway_seconds_bytes_based = minimum_initial_runway_bytes / initial_byte_rate
    max_needed_bytes = 0.0
    for group in groups.values():
        if not group:
            continue
        delivered_prior = 0.0
        for read in group:
            delivery_t = read["completed_t"]
            consumed = calculate_consumed_bytes(group, delivery_t)
            deficit = max(0.0, consumed - delivered_prior)
            max_needed_bytes = max(max_needed_bytes, deficit)
            delivered_prior += read["bytes"]

    for runway in RUNWAYS:
        underflows = 0
        minimum_ahead = float("inf")
        for group in groups.values():
            if not group:
                continue
            initial_byte_rate = group[0]["mbps"] * DECIMAL_MBPS / 8.0
            runway_buffer_bytes = runway * initial_byte_rate
            delivered = 0.0
            for read in group:
                delivery_t = read["completed_t"]
                consumed = calculate_consumed_bytes(group, delivery_t)
                ahead = runway_buffer_bytes + delivered - consumed
                minimum_ahead = min(minimum_ahead, ahead)
                if ahead <= 0:
                    underflows += 1
                delivered += read["bytes"]
        results[str(runway)] = {
            "buffer_underflow_samples": underflows,
            "minimum_buffered_ahead_bytes": (
                None if minimum_ahead == float("inf") else round(minimum_ahead)
            ),
        }
    passing = [r for r in RUNWAYS if results[str(r)]["buffer_underflow_samples"] == 0]
    first_group: list[dict[str, Any]] = next(iter(groups.values())) if groups else []
    first_initial_byte_rate = (
        (first_group[0]["mbps"] * DECIMAL_MBPS / 8.0) if first_group else 1.0
    )
    return {
        "model": model,
        "runways_seconds": results,
        "minimum_initial_runway_seconds": min(passing) if passing else None,
        "minimum_initial_runway_seconds_exact": round(exact_max_deficit_seconds, 4),
        "minimum_initial_runway_bytes": round(max_needed_bytes),
        "minimum_initial_runway_seconds_from_bytes": (
            round(max_needed_bytes / first_initial_byte_rate, 4)
            if first_initial_byte_rate > 0
            else 0.0
        ),
        "interpretation": "offline synthetic modeled underflow at delivery instants; not observed player rebuffers",
    }


async def scenario(
    directory: Path,
    name: str,
    rates: list[int],
    seconds: float,
    streams_count: int,
    scenario_type: str,
    geometry: str,
    chunk_size: int = 512 * 1024,
    concurrent_readers: int = 1,
) -> dict[str, Any]:
    # Imported only after main isolates cwd/environment, before production settings load.
    from program.services.filesystem.vfs import VFSDatabase
    from program.services.streaming import chunker as chunk_module
    from program.services.streaming import media_stream as media_module
    from program.services.streaming.cache import Cache, CacheConfig, CachePutResult
    from program.settings import settings_manager
    from program.utils.async_client import AsyncClient

    directory.mkdir()
    geom_info = GEOMETRIES.get(geometry, GEOMETRIES["stress"])
    hot_cap = geom_info["hot_bytes"]
    warm_cap = geom_info["warm_bytes"]
    cache = Cache(
        CacheConfig(
            cache_dir=directory / "warm",
            hot_dir=directory / "hot",
            max_size_bytes=warm_cap,
            hot_max_size_bytes=hot_cap,
            metrics_enabled=False,
        )
    )

    is_provider_transient = scenario_type == "provider-transient-error"
    is_persistent_refusal = scenario_type == "persistent-cache-refusal"
    provider = Provider(inject_503_once=is_provider_transient)

    class MockVFSEntry:
        def __init__(self, url: str) -> None:
            self.url = url

    class MockVFSDatabase:
        def __init__(self) -> None:
            self.refresh_count = 0

        def get_entry_by_original_filename(
            self, original_filename: str, force_resolve: bool = False
        ) -> Any:
            self.refresh_count += 1
            return MockVFSEntry(
                f"https://d80.invalid/{original_filename}?refreshed={self.refresh_count}"
            )

    vfs_db_mock = MockVFSDatabase()

    counters: Counter[str] = Counter()
    maxima: dict[str, Any] = {
        "hot_bytes": 0,
        "warm_bytes": 0,
        "protected_hot_bytes": 0,
        "active_reader_protected_bytes": 0,
        "lease_protected_bytes": 0,
        "hot_protected_pct": 0.0,
        "readers": 0,
        "leases": 0,
        "reserved_bytes": 0,
        "registry_entries": 0,
    }
    reads: list[dict[str, Any]] = []
    puts: list[dict[str, Any]] = []
    streams: list[Any] = []
    errors: list[str] = []
    original_put = cache.put
    original_ensure = cache._ensure_hot_capacity
    original_evict = cache._evict_lru
    original_demote = cache._remove_hot_after_demotion
    original_stage_temp = cache._stage_demote_hot_to_warm_temp
    original_reading = cache.reading_chunk
    started = time.monotonic()
    pressure_before = {"hot": False, "warm": False}
    demotions_by_key: Counter[str] = Counter()
    demotion_evidence: list[dict[str, Any]] = []
    transient_payload_high_water = {"count": 0, "bytes": 0}

    def sample() -> None:
        with cache._thread_lock:
            hot = cache._hot_bytes
            warm = cache._total_bytes - hot
            now = time.monotonic()
            reader_prot = sum(
                entry.size
                for key, entry in cache._index.items()
                if entry.tier == "hot" and cache._active_readers.get(key, 0) > 0
            )
            lease_prot = sum(
                entry.size
                for key, entry in cache._index.items()
                if entry.tier == "hot"
                and any(
                    lease.expires_at > now
                    for lease in cache._leases_by_key.get(key, {}).values()
                )
            )
            protected_hot = sum(
                entry.size
                for key, entry in cache._index.items()
                if entry.tier == "hot"
                and (
                    cache._active_readers.get(key, 0)
                    or any(
                        lease.expires_at > now
                        for lease in cache._leases_by_key.get(key, {}).values()
                    )
                )
            )
            values = {
                "hot_bytes": hot,
                "warm_bytes": warm,
                "protected_hot_bytes": protected_hot,
                "active_reader_protected_bytes": reader_prot,
                "lease_protected_bytes": lease_prot,
                "hot_protected_pct": round(
                    (protected_hot / hot_cap * 100.0) if hot_cap > 0 else 0.0, 2
                ),
                "readers": sum(cache._active_readers.values()),
                "leases": sum(len(v) for v in cache._leases_by_key.values()),
                "reserved_bytes": cache._hot_reserved_bytes,
            }
        for key, value in values.items():
            maxima[key] = max(maxima[key], value)
        for tier, used, capacity in (("hot", hot, hot_cap), ("warm", warm, warm_cap)):
            above = used > capacity * 0.85
            if above and not pressure_before[tier]:
                counters[f"{tier}_observed_high_crossings"] += 1
            pressure_before[tier] = above
        registry_items = [
            entry
            for stream in streams
            for entry in stream._delivery_registry._entries.values()
        ]
        registry_bytes = sum(len(entry.payload or b"") for entry in registry_items)
        transient_payload_high_water["count"] = max(
            transient_payload_high_water["count"], len(registry_items)
        )
        transient_payload_high_water["bytes"] = max(
            transient_payload_high_water["bytes"], registry_bytes
        )
        maxima["registry_entries"] = max(
            maxima["registry_entries"], len(registry_items)
        )

    async def put(*args: Any, **kwargs: Any) -> Any:
        key = str(kwargs.get("cache_key", args[0] if args else ""))
        start = int(kwargs.get("start", args[1] if len(args) > 1 else 0))
        t_before = time.monotonic()
        if is_persistent_refusal and key.startswith("media-"):
            counters["injected_refusals"] += 1
            result = CachePutResult.REFUSED_PHYSICAL_PRESSURE
            matching_requests = [
                request
                for request in provider.ranges
                if request["title"].endswith(key) and request["start"] == start
            ]
            if matching_requests:
                matching_requests[-1]["cache_refusal"] = True
        else:
            result = await original_put(*args, **kwargs)
        duration_ms = (time.monotonic() - t_before) * 1000
        counters[f"put_{result.value}"] += 1
        puts.append(
            {
                "t": time.monotonic() - started,
                "title": key,
                "start": start,
                "result": result.value,
                "duration_ms": duration_ms,
            }
        )
        sample()
        return result

    async def ensure(need: int) -> None:
        counters["hot_reclaim_calls"] += 1
        counters["hot_demotion_attempts"] += 1
        counters["demotion_attempts"] += 1
        if cache._hot_bytes + cache._hot_reserved_bytes + need > hot_cap * 0.85:
            counters["hot_projected_pressure_calls"] += 1
        before = cache._hot_bytes
        await original_ensure(need)
        reclaimed = max(0, before - cache._hot_bytes)
        counters["hot_reclaimed_bytes"] += reclaimed
        sample()

    async def evict(need: int = 0) -> bool:
        counters["warm_reclaim_calls"] += 1
        before = cache._total_bytes
        result = await original_evict(need)
        counters["warm_reclaimed_bytes"] += max(0, before - cache._total_bytes)
        counters["warm_reclaim_veto_returns"] += int(not result)
        sample()
        return result

    def demote(key: str) -> None:
        before = cache._index.get(key)
        byte_count = before.size if before is not None else 0
        counters["demotion_pressure_source_hot_capacity"] += 1
        original_demote(key)
        demotions_by_key[key] += 1
        counters["completed_demotions"] += 1
        counters["hot_demotion_completed"] += 1
        counters["demoted_bytes"] += byte_count
        counters["repeated_demotions"] += int(demotions_by_key[key] > 1)
        demotion_evidence.append(
            {
                "key": key,
                "bytes": byte_count,
                "repetition": demotions_by_key[key],
                "pressure_source": "hot_capacity_reclaim",
            }
        )

    def stage_temp(key: str) -> tuple[Path | None, Path | None]:
        with cache._thread_lock:
            cur = cache._index.get(key)
            if cur and (
                cache._active_readers.get(key, 0) > 0
                or cache._is_entry_protected(key, cur)
            ):
                if cache._active_readers.get(key, 0) > 0:
                    counters["hot_demotion_veto_active_reader"] += 1
                else:
                    counters["hot_demotion_veto_lease"] += 1
        return original_stage_temp(key)

    @contextmanager
    def reading(key: str) -> Generator[None]:
        with original_reading(key):
            sample()
            yield

    pressure_stop_event = trio.Event()

    async def pressure() -> None:
        index = 0
        while not pressure_stop_event.is_set():
            await cache.put(
                "background-pressure",
                index * MIB,
                payload(index * MIB, MIB, "pressure"),
            )
            counters["pressure_generated_bytes"] += MIB
            index += 1
            # Check stop condition before sleep and support cancellation
            with trio.move_on_after(0.125):
                await pressure_stop_event.wait()

    async def observer() -> None:
        while True:
            sample()
            await trio.sleep(0.02)

    async def player(stream: Any) -> None:
        position = stream.config.header_size
        playback_start = trio.current_time()
        seek_stage = 0
        epoch = 0
        cum_playback_bytes = 0

        # Helper to compute continuous target rate at playback time t (seconds)
        def current_target_rate(t: float) -> int:
            idx = min(len(rates) - 1, int(t / seconds * len(rates)))
            return rates[idx]

        # Invert continuous piece-wise linear target demand to deadline for requested byte count
        def deadline_for_bytes(target_b: float) -> float:
            n = len(rates)
            dt = seconds / n
            b_acc = 0.0
            for i in range(n):
                seg_bytes = dt * (rates[i] * BYTES_PER_MEGABIT)
                if b_acc + seg_bytes >= target_b:
                    rem_bytes = target_b - b_acc
                    rate_bps = rates[i] * BYTES_PER_MEGABIT
                    return playback_start + (i * dt) + (rem_bytes / rate_bps)
                b_acc += seg_bytes
            # Beyond target duration, extrapolate using final rate
            rem = target_b - b_acc
            final_rate = rates[-1] * BYTES_PER_MEGABIT
            return playback_start + seconds + (rem / final_rate)

        while trio.current_time() - playback_start < seconds:
            elapsed = trio.current_time() - playback_start
            rate = current_target_rate(elapsed)
            phase = "sequential"
            if elapsed >= seconds * 0.6 and seek_stage == 0:
                position += 128 * MIB
                seek_stage = 1
                phase = "seek_forward"
                epoch += 1
            elif elapsed >= seconds * 0.8 and seek_stage == 1:
                position = stream.config.header_size + 8 * MIB
                seek_stage = 2
                phase = "seek_backward"
                epoch += 1
            size = 512 * 1024
            interval = size / (rate * BYTES_PER_MEGABIT)
            begin = time.monotonic()
            phase_times: dict[str, float] = {}

            # Measure Cache.get probe before read
            # Align offset to canonical chunk boundary matching Chunker logic
            t_get_start = time.monotonic()
            chunk_start_pos = (position // chunk_size) * chunk_size
            with cache._thread_lock:
                entry = cache._index.get(
                    cache._key(stream.file_metadata.original_filename, chunk_start_pos)
                )
                cache_tier = entry.tier if entry else "miss"
            phase_times["cache_tier_is_hit"] = 1.0 if cache_tier != "miss" else 0.0
            phase_times["cache_probe_ms"] = (time.monotonic() - t_get_start) * 1000

            # Target timeline pacing: compute cumulative demand deadline
            cum_playback_bytes += size
            deadline = deadline_for_bytes(cum_playback_bytes)

            try:
                with trio.fail_after(15):
                    data = await stream.read(
                        request_start=position,
                        request_end=position + size - 1,
                        request_size=size,
                    )
                completed_t = time.monotonic()
                duration = completed_t - begin
                correct = data == payload(
                    position, size, "/" + stream.file_metadata.original_filename
                )
                counters["integrity_checked_bytes"] += len(data)
                counters["integrity_failures"] += int(not correct)

                now = trio.current_time()
                lateness_s = max(0.0, now - deadline)
                is_slot_overrun = lateness_s > 0.0
                if is_slot_overrun:
                    counters["chunk_service_slot_overruns"] += 1
                reads.append(
                    {
                        "stream": stream.stream_id,
                        "epoch": epoch,
                        "offset": position,
                        "bytes": len(data),
                        "phase": phase,
                        "mbps": rate,
                        "latency_ms": duration * 1000,
                        "budget_ms": interval * 1000,
                        "chunk_service_slot_overrun": is_slot_overrun,
                        "consumer_lateness_ms": round(lateness_s * 1000, 3),
                        "integrity_ok": correct,
                        "t": begin - started,
                        "completed_t": completed_t - started,
                        "cache_probe_tier": cache_tier,
                    }
                )
            except Exception as error:
                errors.append(f"{stream.stream_id}: {type(error).__name__}: {error}")
                break
            position += size

            # Absolute timeline sleep: if ahead of demand curve, sleep until deadline.
            # If at or behind demand curve (now >= deadline), do NOT sleep; immediately catch up!
            now = trio.current_time()
            if now < deadline:
                await trio.sleep_until(deadline)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(provider.handle)
    ) as client:
        dependencies = {
            Cache: cache,
            chunk_module.ChunkCacheNotifier: chunk_module.ChunkCacheNotifier(),
            AsyncClient: client,
            VFSDatabase: vfs_db_mock,
        }
        with (
            patch.object(media_module, "di", dependencies),
            patch.object(chunk_module, "di", dependencies),
            patch.object(cache, "put", put),
            patch.object(cache, "_ensure_hot_capacity", ensure),
            patch.object(cache, "_evict_lru", evict),
            patch.object(cache, "_remove_hot_after_demotion", demote),
            patch.object(cache, "_stage_demote_hot_to_warm_temp", stage_temp),
            patch.object(cache, "reading_chunk", reading),
        ):
            async with trio.open_nursery() as nursery:
                with patch.object(
                    settings_manager.settings.stream,
                    "chunk_size_mb",
                    max(1, math.ceil(chunk_size / MIB)),
                ):
                    for index in range(streams_count):
                        title = f"media-{index}.mkv"
                        # Set virtual file size to 16 GiB so long-running (>= 300s) multi-gigabyte endurance playback
                        # never hits EOF prematurely during 150 Mbps runs (which consume ~5.6 GiB in 300s).
                        streams.append(
                            media_module.MediaStream(
                                fh=cast(Any, index + 1),
                                file_size=16 * 1024 * MIB,
                                path=title,
                                original_filename=title,
                                nursery=nursery,
                                provider="synthetic",
                                initial_url=f"https://d80.invalid/{title}",
                                bitrate=rates[0] * DECIMAL_MBPS,
                            )
                        )
                nursery.start_soon(pressure)
                nursery.start_soon(observer)
                try:
                    with trio.fail_after(seconds + 12):
                        async with trio.open_nursery() as players:
                            for stream in streams:
                                for _ in range(concurrent_readers):
                                    players.start_soon(player, stream)
                except Exception as error:
                    errors.append(f"scenario: {type(error).__name__}: {error}")
                finally:
                    pressure_stop_event.set()
                    # Give background pressure task a moment to finish any active Cache.put cleanly
                    await trio.sleep(0.05)
                    for stream in streams:
                        await stream.close()
                    nursery.cancel_scope.cancel()
    sample()
    if cache._hot_reserved_bytes > 0:
        errors.append(
            f"HOT_RESERVATIONS_LEAK: _hot_reserved_bytes={cache._hot_reserved_bytes} after teardown"
        )
    cleanup = {
        "registry_entries": sum(len(s._delivery_registry._entries) for s in streams),
        "streaming_tasks": sum(s.is_streaming.value for s in streams),
        "active_response_refs": sum(
            s._active_stream_connection is not None for s in streams
        ),
        "provider_connections": provider.active,
        "readers": sum(cache._active_readers.values()),
        "leases": sum(len(v) for v in cache._leases_by_key.values()),
        "hot_reservations": cache._hot_reserved_bytes,
        "transient_payload_count": sum(
            entry.payload is not None
            for stream in streams
            for entry in stream._delivery_registry._entries.values()
        ),
        "transient_payload_bytes": sum(
            len(entry.payload or b"")
            for stream in streams
            for entry in stream._delivery_registry._entries.values()
        ),
        "transient_payload_high_water": dict(transient_payload_high_water),
        "nursery_exited": True,
        "client_closed": client.is_closed,
    }
    cleanup["all_counters_zero"] = all(
        cleanup[key] == 0
        for key in (
            "registry_entries",
            "streaming_tasks",
            "active_response_refs",
            "provider_connections",
            "readers",
            "leases",
            "hot_reservations",
            "transient_payload_count",
            "transient_payload_bytes",
        )
    )
    intervals: list[float] = []
    for stream in streams:
        timestamps = [
            float(p["t"])
            for p in puts
            if p["title"] == stream.file_metadata.original_filename
        ]
        intervals.extend((b - a) * 1000 for a, b in pairwise(timestamps))
    pressure_ok = (
        counters["hot_projected_pressure_calls"] > 1
        and counters["warm_reclaimed_bytes"] > 0
    )
    settings = streams[0].config
    sweep = runway_sweep(reads)

    # Decomposed latencies
    put_durations = [p["duration_ms"] for p in puts if "duration_ms" in p]
    hot_reads = [r["latency_ms"] for r in reads if r.get("cache_probe_tier") == "hot"]
    warm_reads = [r["latency_ms"] for r in reads if r.get("cache_probe_tier") == "warm"]
    miss_reads = [r["latency_ms"] for r in reads if r.get("cache_probe_tier") == "miss"]
    requested_bytes = sum(r["bytes"] for r in reads)
    delivered_provider_bytes = provider.byte_accounting()["total_generated_bytes"]

    # Per-stream metrics breakdown
    per_stream: dict[str, Any] = {}
    for stream in streams:
        s_id = stream.stream_id
        s_reads = [r for r in reads if r["stream"] == s_id]
        s_latencies = [r["latency_ms"] for r in s_reads]
        s_overruns = sum(
            1
            for r in s_reads
            if r[
                (
                    "chunk_service_slot_overruns"
                    if "chunk_service_slot_overruns" in r
                    else "chunk_service_slot_overrun"
                )
            ]
        )
        per_stream[s_id] = {
            "title": stream.file_metadata.original_filename,
            "reads_count": len(s_reads),
            "bytes_read": sum(r["bytes"] for r in s_reads),
            "concurrent_reader_target": concurrent_readers,
            "latency_ms": distribution(s_latencies),
            "chunk_service_slot_overruns": s_overruns,
        }

    finished = time.monotonic()
    elapsed = finished - started

    # Rate attainment metrics calculation
    # Analytical integral of expected bytes over the duration:
    def calc_expected_bytes(r_list: list[int], dur: float) -> float:
        n = len(r_list)
        dt = dur / n
        return sum(dt * (r * BYTES_PER_MEGABIT) for r in r_list)

    expected_bytes_single_stream = calc_expected_bytes(rates, seconds)
    expected_consumer_bytes_by_timeline = (
        expected_bytes_single_stream * streams_count * concurrent_readers
    )
    actual_consumer_bytes = requested_bytes
    actual_elapsed_playback_seconds = elapsed
    actual_average_bps = (actual_consumer_bytes * 8.0) / elapsed if elapsed > 0 else 0.0
    rate_attainment_ratio = (
        (actual_consumer_bytes / expected_consumer_bytes_by_timeline)
        if expected_consumer_bytes_by_timeline > 0
        else 0.0
    )

    configured_rate_bps_single = rates[0] * DECIMAL_MBPS if len(rates) == 1 else None
    configured_rate_bytes_per_sec_single = (
        (rates[0] * BYTES_PER_MEGABIT) if len(rates) == 1 else None
    )

    # Buffer underflows at specific runway thresholds
    # When discrete thresholds in (0.0, 0.5, 1.0, 2.0, 5.0, 10.0) all experience underflows,
    # sweep["minimum_initial_runway_seconds"] is None. In that case, we MUST evaluate underflows
    # against the exact continuous minimum calculated runway (minimum_initial_runway_seconds_from_bytes).
    effective_min_runway_sec = sweep["minimum_initial_runway_seconds"]
    if effective_min_runway_sec is None:
        effective_min_runway_sec = sweep.get(
            "minimum_initial_runway_seconds_from_bytes", 0.0
        )

    underflow_at_min_calculated = 0
    if effective_min_runway_sec is not None and effective_min_runway_sec > 0.0:
        underflows_at_min = 0
        groups_temp: dict[tuple[str, int], list[dict[str, Any]]] = {}
        for read in reads:
            groups_temp.setdefault((read["stream"], read["epoch"]), []).append(read)
        for group in groups_temp.values():
            if not group:
                continue
            init_br = group[0]["mbps"] * DECIMAL_MBPS / 8.0
            r_buf = effective_min_runway_sec * init_br
            deliv = 0.0
            for read in group:
                deliv_t = read["completed_t"]
                cons = 0.0
                for i_rd, rd in enumerate(group):
                    t_st = rd["t"]
                    t_en = (
                        group[i_rd + 1]["t"] if i_rd + 1 < len(group) else float("inf")
                    )
                    if deliv_t <= t_st:
                        break
                    act_dur = min(deliv_t, t_en) - t_st
                    cons += act_dur * (rd["mbps"] * DECIMAL_MBPS / 8.0)
                if r_buf + deliv - cons <= 0:
                    underflows_at_min += 1
                deliv += read["bytes"]
        underflow_at_min_calculated = underflows_at_min
    elif sweep["runways_seconds"]["0.0"]["buffer_underflow_samples"] == 0:
        underflow_at_min_calculated = 0
    else:
        underflow_at_min_calculated = sweep["runways_seconds"]["0.0"][
            "buffer_underflow_samples"
        ]

    buffer_underflows_by_runway = {
        "underflows_at_0s": sweep["runways_seconds"]["0.0"]["buffer_underflow_samples"],
        "underflows_at_0_5s": sweep["runways_seconds"]["0.5"][
            "buffer_underflow_samples"
        ],
        "underflows_at_1s": sweep["runways_seconds"]["1.0"]["buffer_underflow_samples"],
        "underflows_at_2s": sweep["runways_seconds"]["2.0"]["buffer_underflow_samples"],
        "underflows_at_5s": sweep["runways_seconds"]["5.0"]["buffer_underflow_samples"],
        "underflows_at_10s": sweep["runways_seconds"]["10.0"][
            "buffer_underflow_samples"
        ],
        "underflows_at_minimum_calculated_runway": underflow_at_min_calculated,
    }

    # Consumer lateness distribution
    lateness_values = [r.get("consumer_lateness_ms", 0.0) for r in reads]

    geom_info = GEOMETRIES.get(geometry, GEOMETRIES["stress"])
    git_head = get_git_head()

    return {
        "name": name,
        "rates_mbps_per_stream": rates,
        "streams": streams_count,
        "target_seconds": seconds,
        "elapsed_seconds": elapsed,
        "started_unix": time.time() - elapsed,
        "finished_unix": time.time(),
        "completed": elapsed >= (seconds - 0.5),
        "scenario": name,
        "geometry": geometry,
        "geometry_provenance": {
            "requested_geometry": geometry,
            "effective_geometry": geometry,
            "hot_capacity_bytes": hot_cap,
            "warm_capacity_bytes": warm_cap,
            "hot_high_pct": geom_info["hot_high_pct"],
            "hot_low_pct": geom_info["hot_low_pct"],
            "warm_high_pct": geom_info["warm_high_pct"],
            "warm_low_pct": geom_info["warm_low_pct"],
            "chunk_size_bytes": 1024 * 1024,
            "read_size_bytes": chunk_size,
            "prefetch_min_chunks": 4,
            "prefetch_max_chunks": 48,
            "expected_concurrent_streams": streams_count,
            "git_head": git_head,
        },
        "rate_attainment": {
            "configured_rate_bps": configured_rate_bps_single,
            "configured_rate_bytes_per_second": configured_rate_bytes_per_sec_single,
            "target_duration_seconds": seconds,
            "expected_consumer_bytes_by_timeline": round(
                expected_consumer_bytes_by_timeline
            ),
            "actual_consumer_bytes": actual_consumer_bytes,
            "actual_elapsed_playback_seconds": round(
                actual_elapsed_playback_seconds, 4
            ),
            "actual_average_bps": round(actual_average_bps, 2),
            "actual_average_mbps": round(actual_average_bps / DECIMAL_MBPS, 2),
            "rate_attainment_ratio": round(rate_attainment_ratio, 4),
            "attainment_acceptable": (
                (0.98 <= rate_attainment_ratio <= 1.02)
                if elapsed >= seconds - 0.5
                else None
            ),
        },
        "consumer_lateness_ms": distribution(lateness_values),
        "production_config": vars(settings),
        "cache_config": {
            "hot_bytes": hot_cap,
            "warm_bytes": warm_cap,
            "high_pct": geom_info["hot_high_pct"],
            "low_pct": geom_info["hot_low_pct"],
        },
        "latency_ms": distribution([r["latency_ms"] for r in reads]),
        "latency_decomposed": {
            "cache_put_ms": distribution(put_durations),
            "hot_read_ms": distribution(hot_reads),
            "warm_read_ms": distribution(warm_reads),
            "miss_fetch_ms": distribution(miss_reads),
        },
        "per_stream": per_stream,
        "producer_cache_completion_interval_ms": distribution(intervals),
        "chunk_service_slot_overruns": counters["chunk_service_slot_overruns"],
        "stream_accounting": {
            "requested_bytes": requested_bytes,
            "provider_generated_bytes": delivered_provider_bytes,
            "refetched_bytes": max(0, delivered_provider_bytes - requested_bytes),
        },
        "runway_models": {
            "CONSTANT_RATE_EXACT": runway_sweep(reads, "CONSTANT_RATE_EXACT"),
            "VARIABLE_TIMELINE_INVERSION": runway_sweep(
                reads, "VARIABLE_TIMELINE_INVERSION"
            ),
        },
        "runway_model": sweep,
        "minimum_initial_runway_seconds": sweep["minimum_initial_runway_seconds"],
        "buffer_underflow_samples": sweep["runways_seconds"]["0.0"][
            "buffer_underflow_samples"
        ],
        "buffer_underflows_by_runway": buffer_underflows_by_runway,
        "counters": dict(counters),
        "occupancy_max": dict(maxima),
        "cleanup": cleanup,
        "demotion_forensics": {
            "attempts": counters["hot_demotion_attempts"],
            "completions": counters["completed_demotions"],
            "bytes": counters["demoted_bytes"],
            "keys": len(demotions_by_key),
            "repeated": counters["repeated_demotions"],
            "max_per_key": max(demotions_by_key.values(), default=0),
            "warm_evictions": counters["warm_reclaimed_bytes"],
            "events": demotion_evidence,
        },
        "pressure_gate_observed": pressure_ok,
        "errors": errors,
        "zero_rebuffer_claim": False,
        "certification": "INCOMPLETE: synthetic only; no real player buffer telemetry or physical tmpfs pressure",
        "provider": {
            "boundary": "httpx.MockTransport",
            "peak_connections": provider.peak,
            "overlaps": provider.overlaps(),
            "byte_accounting": provider.byte_accounting(),
            "categorization": provider.categorize_requests(),
            "ranges": provider.ranges,
        },
        "reads": reads,
        "cache_put_events": puts,
    }


async def matrix(
    work: Path,
    output: Path,
    seconds: float,
    smoke: bool,
    geometry: str = "stress",
    extended: bool = False,
    target_scenario: str | None = None,
    cli_argv: list[str] | None = None,
) -> None:
    geom_info = GEOMETRIES.get(geometry, GEOMETRIES["stress"])
    git_head = get_git_head()
    report: dict[str, Any] = {
        "schema": 2,
        "started_unix": time.time(),
        "geometry": geometry,
        "extended": extended,
        "provenance": {
            "argv": cli_argv or sys.argv,
            "requested_geometry": geometry,
            "effective_geometry": geometry,
            "hot_capacity_bytes": geom_info["hot_bytes"],
            "warm_capacity_bytes": geom_info["warm_bytes"],
            "hot_high": geom_info["hot_high_pct"],
            "hot_low": geom_info["hot_low_pct"],
            "warm_high": geom_info["warm_high_pct"],
            "warm_low": geom_info["warm_low_pct"],
            "git_head": git_head,
        },
        "scenarios": [],
        "limitations": [
            "Windows disk-backed hot and warm directories; OS page cache not disabled; no FUSE/Plex/CDN/TCP.",
            "2-GiB virtual titles generated lazily; 256-KiB provider buffers; 400 Mbps per connection, not a shared WAN cap.",
            "Consumer reads are pace-controlled in decimal Mbps; configured chunk geometry is reported, slot overruns are not player rebuffers.",
            "Player buffer underflow is evaluated via modeled runway sweep (0s, 0.5s, 1s, 2s, 5s, 10s); buffered_ahead_bytes <= 0.",
            "Default production settings frozen by observation, not mutated; cache geometry selectable (stress vs representative).",
            "Fault cases: provider-transient-error injects single 503 URL refresh; persistent-cache-refusal injects REFUSED_PHYSICAL_PRESSURE.",
            "Occupancy is sampled plus synchronous read/put probes; peaks may be undercounted.",
            "Requested range overlaps include open-ended connections and legitimate seeks; generated overlaps are separately counted.",
            "No long-duration leak proof, shared-title concurrency, real hot tmpfs, or physical memory pressure certification.",
        ],
    }
    cases: list[tuple[str, list[int], int, str, int, int]] = [
        (f"baseline-{rate}", [rate], 1, "standard", 512 * 1024, 1)
        for rate in (20, 40, 60, 80, 100, 120, 150)
    ]
    cases += [
        ("variable-burst", [60, 100, 70, 140, 80], 1, "standard", 512 * 1024, 1),
        ("two-streams", [60], 2, "standard", 512 * 1024, 1),
        ("four-streams", [40], 4, "standard", 512 * 1024, 1),
        (
            "provider-transient-error",
            [60],
            1,
            "provider-transient-error",
            512 * 1024,
            1,
        ),
        (
            "persistent-cache-refusal",
            [60],
            1,
            "persistent-cache-refusal",
            512 * 1024,
            1,
        ),
        (
            "persistent-refusal-32-chunks-concurrent",
            [60],
            1,
            "persistent-cache-refusal",
            MIB,
            32,
        ),
    ]
    if geometry == "scaled_pressure":
        cases += [("scaled-pressure", [80], 1, "standard", 2 * MIB, 1)]
    if geometry in {"representative", "production_representative"} or extended:
        cases.append(("representative-pressure", [60], 1, "standard", 512 * 1024, 1))
    if geometry == "production_representative":
        cases = [("production-representative", [60], 1, "standard", 512 * 1024, 1)]
    if target_scenario:
        matched = [c for c in cases if c[0] == target_scenario]
        if not matched:
            raise ValueError(
                f"Unknown scenario '{target_scenario}'. Available: {[c[0] for c in cases]}"
            )
        cases = matched
    elif smoke:
        cases = [cases[-1]]
    for name, rates, count, stype, chunk_size, concurrent_readers in cases:
        try:
            result = await scenario(
                work / name,
                name,
                rates,
                seconds,
                count,
                stype,
                geometry,
                chunk_size,
                concurrent_readers,
            )
        except Exception as error:
            result = {"name": name, "fatal_error": f"{type(error).__name__}: {error}"}
        report["scenarios"].append(result)
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        sys.stdout.write(
            json.dumps(
                {
                    key: result.get(key)
                    for key in (
                        "name",
                        "elapsed_seconds",
                        "latency_ms",
                        "chunk_service_slot_overruns",
                        "buffer_underflow_samples",
                        "minimum_initial_runway_seconds",
                        "errors",
                        "fatal_error",
                        "cleanup",
                    )
                }
            )
            + "\n"
        )
        sys.stdout.flush()
    report["finished_unix"] = time.time()
    report["target_seconds"] = seconds
    report["elapsed_seconds"] = report["finished_unix"] - report["started_unix"]
    report["completed"] = report["elapsed_seconds"] >= (seconds - 1.0)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=15)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument(
        "--geometry",
        choices=[
            "stress",
            "representative",
            "scaled_pressure",
            "production_representative",
        ],
        default="stress",
    )
    parser.add_argument(
        "--extended",
        action="store_true",
        help="Include extended multi-scenario coverage",
    )
    parser.add_argument(
        "--endurance", choices=["standard", "300s", "900s"], default="standard"
    )
    parser.add_argument(
        "--scenario", type=str, default=None, help="Run a specific named scenario only"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.endurance != "standard":
        args.seconds = 300 if args.endurance == "300s" else 900
    if not 1 <= args.seconds <= 900:
        parser.error("--seconds must be between 1 and 900")
    output = args.output.resolve()
    # Path traversal validation
    try:
        output.relative_to(ROOT)
    except ValueError:
        # If output is not within ROOT, ensure it is within user's home or current working directory
        cwd = Path.cwd().resolve()
        if not (
            output.is_relative_to(cwd) or output.is_relative_to(Path.home().resolve())
        ):
            parser.error(
                "Output path must be within the project root, current directory, or home directory."
            )
    output.parent.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(ROOT / "src"))
    logger.remove()
    logger.add(sys.stderr, level="ERROR")
    # Ensure imports cannot read/write repository settings or local deployment data.
    old_cwd = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="d80-runtime-") as temporary:
        work = Path(temporary)
        with patch.dict(
            os.environ,
            {
                "CINEFLOW_SETTINGS_FILENAME": str(work / "settings.json"),
                "SETTINGS_FILENAME": str(work / "settings.json"),
            },
        ):
            try:
                os.chdir(work)
                trio.run(
                    matrix,
                    work,
                    output,
                    args.seconds,
                    args.smoke,
                    args.geometry,
                    args.extended,
                    args.scenario,
                    sys.argv,
                )
            finally:
                os.chdir(old_cwd)
    sys.stdout.write(f"Evidence: {output}\n")


if __name__ == "__main__":
    main()
