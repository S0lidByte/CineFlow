"""Tests for Prometheus mirrors of streaming cache metrics."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import trio
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from kink import di

from auth import resolve_api_key
from program.services.streaming import prom_cache_metrics as prom
from program.services.streaming.cache import Cache, CacheConfig
from routers.secure import default as default_router_mod


def _make_cache(cache_dir: Path, *, metrics_enabled: bool = True) -> Cache:
    return Cache(
        CacheConfig(
            cache_dir=cache_dir,
            max_size_bytes=10 * 1024 * 1024,
            metrics_enabled=metrics_enabled,
        )
    )


def test_cache_dual_writes_prometheus_counters(tmp_path: Path) -> None:
    cache = _make_cache(tmp_path)
    payload = b"abcdefghij" * 50

    hits_before = prom.REGISTRY.get_sample_value("riven_cache_hits_total") or 0.0
    misses_before = prom.REGISTRY.get_sample_value("riven_cache_misses_total") or 0.0
    written_before = (
        prom.REGISTRY.get_sample_value("riven_cache_bytes_written_total") or 0.0
    )

    async def _run() -> None:
        await cache.put("movie.mkv", 0, payload)
        got = await cache.get("movie.mkv", 0, len(payload) - 1)
        assert got == payload
        miss = await cache.get("movie.mkv", 10_000, 10_100)
        assert miss == b""

    trio.run(_run)

    hits_after = prom.REGISTRY.get_sample_value("riven_cache_hits_total") or 0.0
    misses_after = prom.REGISTRY.get_sample_value("riven_cache_misses_total") or 0.0
    written_after = (
        prom.REGISTRY.get_sample_value("riven_cache_bytes_written_total") or 0.0
    )

    assert hits_after == hits_before + 1
    assert misses_after == misses_before + 1
    assert written_after == written_before + len(payload)


def test_cache_metrics_disabled_skips_prometheus(tmp_path: Path) -> None:
    cache = _make_cache(tmp_path, metrics_enabled=False)
    payload = b"xyz"

    hits_before = prom.REGISTRY.get_sample_value("riven_cache_hits_total") or 0.0
    written_before = (
        prom.REGISTRY.get_sample_value("riven_cache_bytes_written_total") or 0.0
    )

    async def _run() -> None:
        await cache.put("a.mkv", 0, payload)
        await cache.get("a.mkv", 0, len(payload) - 1)

    trio.run(_run)

    hits_after = prom.REGISTRY.get_sample_value("riven_cache_hits_total") or 0.0
    written_after = (
        prom.REGISTRY.get_sample_value("riven_cache_bytes_written_total") or 0.0
    )
    assert hits_after == hits_before
    assert written_after == written_before


def _metrics_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    import auth

    monkeypatch.setattr(
        auth.settings_manager,
        "settings",
        MagicMock(
            api_key="k" * 32,
            filesystem=MagicMock(cache_metrics=True),
        ),
    )
    monkeypatch.setattr(
        default_router_mod.settings_manager,
        "settings",
        MagicMock(
            api_key="k" * 32,
            filesystem=MagicMock(cache_metrics=True),
        ),
    )

    app = FastAPI()
    app.include_router(
        default_router_mod.router,
        dependencies=[Depends(resolve_api_key)],
    )
    return TestClient(app)


def test_prometheus_metrics_endpoint_requires_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _metrics_client(monkeypatch)

    denied = client.get("/metrics")
    assert denied.status_code == 401

    previous_cache = di[Cache] if Cache in di else None
    size_before = prom.REGISTRY.get_sample_value("riven_cache_size_bytes")
    entries_before = prom.REGISTRY.get_sample_value("riven_cache_entries")
    try:
        if Cache in di:
            del di[Cache]

        ok = client.get("/metrics", headers={"x-api-key": "k" * 32})
        assert ok.status_code == 200
        assert "riven_cache_hits_total" in ok.text
        assert "text/plain" in ok.headers.get("content-type", "")
        assert (prom.REGISTRY.get_sample_value("riven_cache_size_bytes") or 0.0) == 0.0
        assert (prom.REGISTRY.get_sample_value("riven_cache_entries") or 0.0) == 0.0
    finally:
        if previous_cache is not None:
            di[Cache] = previous_cache
        elif Cache in di:
            del di[Cache]
        if size_before is not None:
            prom.SIZE_BYTES.set(size_before)
        if entries_before is not None:
            prom.ENTRIES.set(entries_before)


def test_prometheus_metrics_endpoint_with_live_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: live Cache uses trio.Lock; /metrics must not await it under asyncio."""
    client = _metrics_client(monkeypatch)
    cache = _make_cache(tmp_path)
    payload = b"abcdefghij" * 50

    async def _seed() -> None:
        await cache.put("movie.mkv", 0, payload)

    trio.run(_seed)

    previous_cache = di[Cache] if Cache in di else None
    try:
        di[Cache] = cache
        ok = client.get("/metrics", headers={"x-api-key": "k" * 32})
        assert ok.status_code == 200
        assert "riven_cache_hits_total" in ok.text
        assert f"riven_cache_size_bytes {len(payload)}" in ok.text
        assert "riven_cache_entries 1" in ok.text
    finally:
        if previous_cache is not None:
            di[Cache] = previous_cache
        elif Cache in di:
            del di[Cache]


def test_render_metrics_includes_gauge_names() -> None:
    prom.set_size_gauges(total_bytes=1234, entries=7)
    body = prom.render_metrics().decode("utf-8")
    assert "riven_cache_size_bytes 1234" in body
    assert "riven_cache_entries 7" in body
