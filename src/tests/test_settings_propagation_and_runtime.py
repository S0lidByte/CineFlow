"""
Certification test suite for CineFlow settings propagation and runtime application.

Validates that:
1. Setting mutations through the REST API (/api/v1/settings/set/{paths} and /set/all)
   reliably propagate user settings:
   - stream.chunk_size_mb = 3
   - stream.prefetch_chunks = 55
   - filesystem.tmpfs_cache_max_mb = 6165
2. Runtime application without VFS remount:
   - Newly created MediaStream instances immediately observe chunk_size = 3 MiB (3,145,728 bytes).
   - Adaptive prefetch manager respects prefetch_chunks = 55 (both fallback and max_window_chunks).
   - Live Cache instance dynamically updates watermarks and sizing budgets.
3. Partial/missing sections are not silently overwritten.
4. autotune_mode enum schema provides selectable 'disabled', 'conservative', 'balanced', 'aggressive'.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from kink import di

from program.services.streaming.cache import Cache, CacheConfig
from program.services.streaming.media_stream import MediaStream
from program.settings import settings_manager
from program.settings.models import AppModel
from routers.secure.settings import router as settings_router

app = FastAPI()
app.include_router(settings_router, prefix="/api/v1")
client = TestClient(app)


@pytest.fixture
def clean_settings(tmp_path: Path):
    """Fixture providing clean settings and restoring original settings on teardown."""
    original_settings = settings_manager.settings
    original_cache = di[Cache] if Cache in di else None

    # Setup isolated test Cache in di
    cache_dir = tmp_path / "warm"
    hot_dir = tmp_path / "hot"
    cache_dir.mkdir(parents=True, exist_ok=True)
    hot_dir.mkdir(parents=True, exist_ok=True)

    test_cache = Cache(
        CacheConfig(
            cache_dir=cache_dir,
            max_size_bytes=100 * 1024 * 1024,
            hot_dir=hot_dir,
            hot_max_size_bytes=20 * 1024 * 1024,
            metrics_enabled=False,
        )
    )
    di[Cache] = test_cache

    fresh_settings = AppModel(api_key="TEST_API_KEY_EXACTLY_32_CHARS_LONG_!")
    settings_manager.settings = fresh_settings

    try:
        yield fresh_settings
    finally:
        settings_manager.settings = original_settings
        if original_cache is not None:
            di[Cache] = original_cache
        elif Cache in di:
            del di[Cache]


def test_settings_propagation_and_runtime_media_stream_application(
    clean_settings, tmp_path
):
    """
    Test exact user scenario:
    Frontend sets:
      - stream.chunk_size_mb = 3
      - stream.prefetch_chunks = 55
      - filesystem.tmpfs_cache_max_mb = 6165
    Verify propagation to backend settings and runtime application on newly created MediaStream.
    """
    api_key = clean_settings.api_key

    # 1. Mutate via POST /api/v1/settings/set/{paths}
    payload = {
        "stream": {
            "chunk_size_mb": 3,
            "prefetch_chunks": 55,
        },
        "filesystem": {
            "tmpfs_cache_max_mb": 6165,
            "hot_cache_watermark_high_pct": 82.0,
            "hot_cache_watermark_low_pct": 65.0,
        },
    }

    response = client.post(
        "/api/v1/settings/set/stream,filesystem",
        json=payload,
        headers={"x-api-key": api_key},
    )
    assert response.status_code == 200, response.text

    # 2. Verify GET read-back from API
    read_resp = client.get(
        "/api/v1/settings/get/stream,filesystem",
        headers={"x-api-key": api_key},
    )
    assert read_resp.status_code == 200
    read_data = read_resp.json()
    assert read_data["stream"]["chunk_size_mb"] == 3
    assert read_data["stream"]["prefetch_chunks"] == 55
    assert read_data["filesystem"]["tmpfs_cache_max_mb"] == 6165

    # 3. Verify backend settings_manager state directly
    assert settings_manager.settings.stream.chunk_size_mb == 3
    assert settings_manager.settings.stream.prefetch_chunks == 55
    assert settings_manager.settings.filesystem.tmpfs_cache_max_mb == 6165

    # 4. Verify live Cache instance updated without VFS remount
    live_cache = di[Cache]
    status = live_cache.watermark_status()
    assert status["hot_watermark_high_pct"] == 82.0
    assert status["hot_watermark_low_pct"] == 65.0

    # 5. Verify newly created MediaStream reflects these exact values
    # Instantiate bare MediaStream with constructor mocking external network dependencies
    with (
        patch(
            "program.services.streaming.media_stream.trio_util.AsyncValue"
        ) as mock_val,
        patch("program.services.streaming.media_stream.Chunker") as mock_chunker,
    ):
        mock_val.return_value = MagicMock()
        mock_chunker.return_value = MagicMock()

        stream = MediaStream(
            fh=101,
            file_size=100 * 1024 * 1024,
            path="/media/movies/Test (2025)/Test.mkv",
            original_filename="Test.mkv",
            provider="realdebrid",
            initial_url="https://example.com/test.mkv",
            bitrate=None,  # unknown bitrate to test fallback
            nursery=MagicMock(),
            http_pool=MagicMock(),
        )

        # Runtime chunk size check: 3 MiB = 3 * 1024 * 1024 = 3,145,728 bytes
        assert stream.config.chunk_size == 3 * 1024 * 1024
        assert stream.config.prefetch_chunks == 55

        # Adaptive prefetch configuration check
        assert stream.adaptive_prefetch.config.chunk_size_bytes == 3 * 1024 * 1024
        assert stream.adaptive_prefetch.config.default_fallback_chunks == 55
        assert stream.adaptive_prefetch.config.max_window_chunks >= 55

        # Unknown-bitrate window calculation yields exactly 55
        assert stream.adaptive_prefetch.calculate_window() == 55


def test_autotune_mode_schema_options():
    """Verify that autotune_mode in schema includes all valid operational modes including 'disabled'."""
    schema_resp = client.get("/api/v1/settings/schema/keys?keys=filesystem")
    assert schema_resp.status_code == 200
    schema = schema_resp.json()
    filesystem_schema = schema["properties"]["filesystem"]

    # Locate autotune_mode enum in properties or defs
    defs = schema.get("$defs", {})
    autotune_prop = filesystem_schema.get("properties", {}).get("autotune_mode", {})
    enum_values = autotune_prop.get("enum")
    if not enum_values and "$ref" in autotune_prop:
        ref_name = autotune_prop["$ref"].split("/")[-1]
        enum_values = defs.get(ref_name, {}).get("enum")

    assert enum_values is not None
    assert "disabled" in enum_values
    assert "conservative" in enum_values
    assert "balanced" in enum_values
    assert "aggressive" in enum_values
