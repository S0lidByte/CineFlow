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


def test_tmpfs_respects_custom_hard_cap() -> None:
    """Ops can raise tmpfs_cache_max_mb to authorize a ~10 GiB RAM hot cache."""

    free = 20 * 1024 * 1024 * 1024
    configured_mb = 10240
    custom_cap = 10 * 1024 * 1024 * 1024
    result = resolve_cache_max_bytes(
        Path("/dev/shm/riven-cache"),
        configured_mb,
        free_bytes=free,
        tmpfs=True,
        tmpfs_hard_cap_bytes=custom_cap,
    )
    assert result.is_tmpfs is True
    assert result.effective_max_bytes == custom_cap
    assert result.clamped is False


def test_tmpfs_custom_cap_still_half_free() -> None:
    free = 4 * 1024 * 1024 * 1024  # 4 GiB free
    custom_cap = 10 * 1024 * 1024 * 1024
    result = resolve_cache_max_bytes(
        Path("/dev/shm/riven-cache"),
        10240,
        free_bytes=free,
        tmpfs=True,
        tmpfs_hard_cap_bytes=custom_cap,
    )
    assert result.effective_max_bytes == int(free * 0.5)
    assert result.reason is not None
    assert "50% available tmpfs space" in result.reason
    assert "free tmpfs space=4096 MB" in result.reason


def test_tmpfs_warning_identifies_configured_ceiling_constraint() -> None:
    """Warning identifies configured tmpfs ceiling when smaller than free space fraction and cgroup."""
    free = 16 * 1024 * 1024 * 1024  # 16 GiB free
    configured_mb = 10240  # 10 GiB configured
    hard_cap = 2 * 1024 * 1024 * 1024  # 2 GiB hard cap
    result = resolve_cache_max_bytes(
        Path("/dev/shm/riven-cache"),
        configured_mb,
        free_bytes=free,
        tmpfs=True,
        tmpfs_hard_cap_bytes=hard_cap,
    )
    assert result.clamped is True
    assert result.effective_max_bytes == hard_cap
    assert result.reason is not None
    assert "configured tmpfs ceiling" in result.reason
    assert "filesystem.tmpfs_cache_max_mb ceiling=2048 MB" in result.reason


def test_tmpfs_warning_identifies_cgroup_headroom_constraint() -> None:
    """Warning identifies cgroup headroom when container memory limit is tightest constraint."""
    from unittest.mock import patch

    free = 16 * 1024 * 1024 * 1024  # 16 GiB free
    configured_mb = 10240
    hard_cap = 8 * 1024 * 1024 * 1024  # 8 GiB hard cap

    with patch(
        "program.services.streaming.cache_sizing.get_cgroup_memory_limit"
    ) as mock_cgroup:
        # 3072 MB cgroup limit - 1536 MB headroom = 1536 MB safe tmpfs cap
        mock_cgroup.return_value = 3072 * 1024 * 1024

        result = resolve_cache_max_bytes(
            Path("/dev/shm/riven-cache"),
            configured_mb,
            free_bytes=free,
            tmpfs=True,
            tmpfs_hard_cap_bytes=hard_cap,
        )
        assert result.clamped is True
        assert result.effective_max_bytes == 1536 * 1024 * 1024
        assert result.reason is not None
        assert "cgroup memory headroom" in result.reason
        assert "cgroup limit=3072 MB - 1536 MB headroom = 1536 MB" in result.reason


def test_is_tmpfs_path_detects_dev_shm_prefix() -> None:
    from program.services.streaming.cache_sizing import is_tmpfs_path

    assert is_tmpfs_path(Path("/dev/shm/riven-cache")) is True
