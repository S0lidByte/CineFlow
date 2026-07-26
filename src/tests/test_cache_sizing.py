"""Tests for tmpfs-aware streaming cache size resolution (OOM clamp)."""

from __future__ import annotations

from pathlib import Path

from program.services.streaming.cache_sizing import (
    TMPFS_CACHE_HARD_CAP_BYTES,
    resolve_cache_max_bytes,
)


def test_disk_cache_clamps_to_90_percent_free() -> None:
    free = 10 * 1024 * 1024 * 1024  # 10 GiB free
    configured_mb = 10240  # 10 GiB configured
    result = resolve_cache_max_bytes(
        Path("/riven/data/cache"),
        configured_mb,
        free_bytes=free,
        tmpfs=False,
    )
    assert result.is_tmpfs is False
    assert result.clamped is True
    assert result.effective_max_bytes == int(free * 0.9)
    assert result.reason is not None
    assert "available space" in result.reason


def test_disk_cache_keeps_budget_when_under_free() -> None:
    free = 50 * 1024 * 1024 * 1024
    configured_mb = 1024
    result = resolve_cache_max_bytes(
        Path("/riven/data/cache"),
        configured_mb,
        free_bytes=free,
        tmpfs=False,
    )
    assert result.clamped is False
    assert result.effective_max_bytes == configured_mb * 1024 * 1024


def test_tmpfs_hard_caps_despite_huge_free_and_config() -> None:
    """Reproduces prod OOM signature: /dev/shm with ~12GiB free + 10GiB config."""

    free = 12_800 * 1024 * 1024  # ~12.8 GiB (matches ~11520 MB * 0.9-style free)
    configured_mb = 10240
    result = resolve_cache_max_bytes(
        Path("/dev/shm/riven-cache"),
        configured_mb,
        free_bytes=free,
        tmpfs=True,
    )
    assert result.is_tmpfs is True
    assert result.clamped is True
    # Must NOT authorize ~11.5 GiB RAM (old free*0.9 behavior on tmpfs).
    assert result.effective_max_bytes == TMPFS_CACHE_HARD_CAP_BYTES
    assert result.effective_max_bytes < 2 * 1024 * 1024 * 1024
    assert result.reason is not None
    assert "tmpfs" in result.reason.lower() or "hard-capped" in result.reason.lower()


def test_tmpfs_respects_half_free_when_smaller_than_hard_cap() -> None:
    free = 800 * 1024 * 1024  # 800 MiB free shm
    configured_mb = 10240
    result = resolve_cache_max_bytes(
        Path("/dev/shm/riven-cache"),
        configured_mb,
        free_bytes=free,
        tmpfs=True,
    )
    assert result.effective_max_bytes == int(free * 0.5)
    assert result.effective_max_bytes < TMPFS_CACHE_HARD_CAP_BYTES


def test_tmpfs_zero_free_caps_to_zero() -> None:
    result = resolve_cache_max_bytes(
        Path("/dev/shm/riven-cache"),
        10240,
        free_bytes=0,
        tmpfs=True,
    )
    assert result.clamped is True
    assert result.effective_max_bytes == 0


def test_disk_zero_free_caps_to_zero() -> None:
    result = resolve_cache_max_bytes(
        Path("/riven/data/cache"),
        10240,
        free_bytes=0,
        tmpfs=False,
    )
    assert result.clamped is True
    assert result.effective_max_bytes == 0


def test_is_tmpfs_path_detects_dev_shm_prefix() -> None:
    from program.services.streaming.cache_sizing import is_tmpfs_path

    assert is_tmpfs_path(Path("/dev/shm/riven-cache")) is True
