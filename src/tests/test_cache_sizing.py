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


# ---------------------------------------------------------------------------
# FIX-2: resolve_cache_max_bytes – account for existing cache + partition cap
# ---------------------------------------------------------------------------


def test_disk_cache_accounts_for_existing_cache_bytes() -> None:
    """FIX-2: Existing cache occupancy plus free headroom prevents eviction storms
    while still respecting remaining free space on shared partitions.
    """
    total = 59 * 1024 * 1024 * 1024
    free = 5 * 1024 * 1024 * 1024
    existing = 6 * 1024 * 1024 * 1024
    configured_mb = 20000  # 20 GiB configured (> headroom, < total*0.9)
    result = resolve_cache_max_bytes(
        Path("/mnt/cache"),
        configured_mb,
        free_bytes=free,
        total_bytes=total,
        existing_bytes=existing,
        tmpfs=False,
    )
    expected = existing + int(
        free * 0.9
    )  # 6 GiB + 4.5 GiB = 10.5 GiB (leaves 10% free)
    assert result.clamped is True
    assert result.effective_max_bytes == expected
    assert result.effective_max_bytes > existing
    assert result.reason is not None and "existing=" in result.reason


def test_disk_cache_not_clamped_when_existing_plus_headroom_covers_config() -> None:
    """FIX-2: When configured <= min(total*0.9, existing + free*0.9), keep budget."""
    total = 59 * 1024 * 1024 * 1024
    free = 11 * 1024 * 1024 * 1024
    existing = 6 * 1024 * 1024 * 1024
    configured_mb = 10240  # 10 GiB <= min(53.1 GiB, 6 + 9.9 GiB)
    result = resolve_cache_max_bytes(
        Path("/mnt/cache"),
        configured_mb,
        free_bytes=free,
        total_bytes=total,
        existing_bytes=existing,
        tmpfs=False,
    )
    assert result.clamped is False
    assert result.effective_max_bytes == configured_mb * 1024 * 1024


def test_disk_cache_total_fraction_caps_when_tightest() -> None:
    """FIX-2: Dedicated partition still honors total * DISK_FREE_FRACTION ceiling."""
    total = 10 * 1024 * 1024 * 1024
    free = 2 * 1024 * 1024 * 1024
    existing = (
        8 * 1024 * 1024 * 1024
    )  # existing + 0.9*free = 9.8 GiB > 9.0 GiB total cap
    configured_mb = 10240
    result = resolve_cache_max_bytes(
        Path("/mnt/cache"),
        configured_mb,
        free_bytes=free,
        total_bytes=total,
        existing_bytes=existing,
        tmpfs=False,
    )
    expected = int(total * 0.9)
    assert result.clamped is True
    assert result.effective_max_bytes == expected


def test_disk_cache_backward_compat_without_existing_or_total() -> None:
    """FIX-2: Callers omitting existing_bytes/total_bytes keep free*fraction behavior."""
    free = 10 * 1024 * 1024 * 1024
    configured_mb = 10240
    result = resolve_cache_max_bytes(
        Path("/riven/data/cache"),
        configured_mb,
        free_bytes=free,
        tmpfs=False,
    )
    assert result.clamped is True
    assert result.effective_max_bytes == int(free * 0.9)
    assert result.reason is not None
    assert "available space" in result.reason


def test_measure_directory_bytes_counts_fanout_files(tmp_path: Path) -> None:
    """FIX-2: measure_directory_bytes sums top-level and 2-level fanout files."""
    from program.services.streaming.cache_sizing import measure_directory_bytes

    sub_a = tmp_path / "aa"
    sub_a.mkdir()
    (sub_a / "chunk1").write_bytes(b"1" * 1234)
    (sub_a / "chunk1.meta").write_bytes(b"m" * 66)
    (tmp_path / "root_file").write_bytes(b"r" * 100)
    assert measure_directory_bytes(tmp_path) == 1400
    assert measure_directory_bytes(tmp_path / "missing") == 0
