"""Resolve effective streaming-cache size with tmpfs/OOM-safe clamps.

Default ``filesystem.cache_dir`` is ``/dev/shm/riven-cache`` (RAM-backed).
Without a hard cap, ``cache_max_size_mb`` (default 10 GiB) plus disk-free
clamping at 90% can authorize multi-GiB tmpfs usage and get the process
SIGKILL'd by the Linux OOM killer (bare ``Killed``, no Python traceback).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Hard ceiling when cache lives on tmpfs/ramfs (/dev/shm). Leaves headroom
# for Python heap, FUSE, and concurrent chunk buffers under Plex multi-open.
TMPFS_CACHE_HARD_CAP_BYTES = 1024 * 1024 * 1024  # 1 GiB

# On tmpfs, never take more than half of reported free space.
TMPFS_FREE_FRACTION = 0.5

# On regular disk, keep the historical 90% free-space clamp.
DISK_FREE_FRACTION = 0.9


@dataclass(frozen=True)
class CacheSizeResolution:
    """Result of resolving configured cache size against filesystem reality."""

    effective_max_bytes: int
    configured_bytes: int
    free_bytes: int
    is_tmpfs: bool
    clamped: bool
    reason: str | None = None


def is_tmpfs_path(path: Path) -> bool:
    """
    Return True if ``path`` resolves onto a tmpfs/ramfs mount.

    Uses ``/proc/mounts`` when available; falls back to a ``/dev/shm`` prefix
    check (the historical default cache location).
    """

    # Check the configured path string before resolve() — on Windows,
    # Path("/dev/shm/...").resolve() becomes a drive-local path and would
    # miss the documented Linux default.
    configured = path.as_posix()
    if configured == "/dev/shm" or configured.startswith("/dev/shm/"):
        return True

    try:
        resolved = path.resolve()
    except OSError:
        resolved = path

    resolved_str = resolved.as_posix()

    if resolved_str == "/dev/shm" or resolved_str.startswith("/dev/shm/"):
        return True

    try:
        with open("/proc/mounts", encoding="utf-8") as mounts:
            best_mount: str | None = None
            best_fstype: str | None = None

            for line in mounts:
                parts = line.split()
                if len(parts) < 3:
                    continue

                mount_point = parts[1].replace("\\040", " ")
                fstype = parts[2]

                if resolved_str == mount_point or resolved_str.startswith(
                    mount_point.rstrip("/") + "/"
                ):
                    if best_mount is None or len(mount_point) > len(best_mount):
                        best_mount = mount_point
                        best_fstype = fstype

            return best_fstype in ("tmpfs", "ramfs")
    except OSError:
        return False


def get_cgroup_memory_limit() -> int | None:
    """Read container memory limit from cgroup v2 (memory.max) or cgroup v1 (memory.limit_in_bytes)
    via normalized CgroupCapabilities detection.
    """
    from program.services.streaming.runtime_profile import (
        CgroupCapabilities,
        LimitState,
    )

    caps = CgroupCapabilities.detect()
    if caps.memory_hard_boundary.state is LimitState.FINITE and isinstance(
        caps.memory_hard_boundary.value, int
    ):
        return caps.memory_hard_boundary.value
    return None


def resolve_cache_max_bytes(
    cache_dir: Path,
    configured_mb: int,
    *,
    free_bytes: int | None = None,
    tmpfs: bool | None = None,
    tmpfs_hard_cap_bytes: int = TMPFS_CACHE_HARD_CAP_BYTES,
) -> CacheSizeResolution:
    """
    Compute the effective cache budget.

    Parameters:
        cache_dir: Cache directory path (used for tmpfs detection when
            ``tmpfs`` is not provided).
        configured_mb: ``filesystem.cache_max_size_mb`` setting.
        free_bytes: Optional free-space override (tests); otherwise callers
            should pass ``shutil.disk_usage(...).free``.
        tmpfs: Optional override for tmpfs detection (tests).
        tmpfs_hard_cap_bytes: Absolute max when on tmpfs (default 1 GiB).

    Returns:
        CacheSizeResolution with effective bytes and clamp metadata.
    """

    configured_bytes = max(0, int(configured_mb)) * 1024 * 1024
    has_free_space_measurement = free_bytes is not None
    free = int(free_bytes) if has_free_space_measurement else 0
    on_tmpfs = is_tmpfs_path(cache_dir) if tmpfs is None else bool(tmpfs)

    effective = configured_bytes
    reason: str | None = None

    if on_tmpfs:
        # Evaluate candidate caps across:
        # 1. Configured tmpfs hard cap (filesystem.tmpfs_cache_max_mb)
        # 2. Cgroup memory limit minus reserved application headroom (1536 MiB)
        # 3. Available tmpfs space safety fraction (TMPFS_FREE_FRACTION = 0.5)
        limiting_factor = "configured tmpfs ceiling"
        tmpfs_cap = tmpfs_hard_cap_bytes

        cgroup_limit = get_cgroup_memory_limit()
        cgroup_cap: int | None = None
        if cgroup_limit is not None:
            # Leave at least 1.5 GiB for Python application working set, pyfuse3, PostgreSQL, network buffers
            reserved_app_headroom = 1536 * 1024 * 1024
            cgroup_cap = max(0, cgroup_limit - reserved_app_headroom)
            if cgroup_cap < tmpfs_cap:
                tmpfs_cap = cgroup_cap
                limiting_factor = "cgroup memory headroom"

        free_fraction_cap: int | None = None
        if has_free_space_measurement:
            free_fraction_cap = int(free * TMPFS_FREE_FRACTION)
            if free_fraction_cap < tmpfs_cap:
                tmpfs_cap = free_fraction_cap
                limiting_factor = "50% available tmpfs space"

        if effective > tmpfs_cap:
            effective = tmpfs_cap
            details: list[str] = []
            if limiting_factor == "configured tmpfs ceiling":
                details.append(
                    f"filesystem.tmpfs_cache_max_mb ceiling={tmpfs_hard_cap_bytes // (1024 * 1024)} MB"
                )
            elif (
                limiting_factor == "cgroup memory headroom" and cgroup_limit is not None
            ):
                details.append(
                    f"cgroup limit={cgroup_limit // (1024 * 1024)} MB - 1536 MB headroom = {cgroup_cap // (1024 * 1024) if cgroup_cap is not None else 0} MB"
                )
            elif limiting_factor == "50% available tmpfs space":
                details.append(
                    f"free tmpfs space={free // (1024 * 1024)} MB (50% safety fraction = {free_fraction_cap // (1024 * 1024) if free_fraction_cap is not None else 0} MB)"
                )

            detail_str = f" ({details[0]})" if details else ""
            reason = (
                f"tmpfs/ramfs cache_dir hard-capped to "
                f"{effective // (1024 * 1024)} MB "
                f"(configured {configured_mb} MB) due to {limiting_factor}{detail_str}. "
                "Raise filesystem.tmpfs_cache_max_mb (and container shm/mem limits) "
                "for a larger RAM cache, or move cache_dir off tmpfs onto disk."
            )
    elif has_free_space_measurement and configured_bytes > int(
        free * DISK_FREE_FRACTION
    ):
        effective = int(free * DISK_FREE_FRACTION)
        reason = (
            f"cache_max_size_mb clamped to available space: "
            f"{effective // (1024 * 1024)} MB"
        )

    return CacheSizeResolution(
        effective_max_bytes=effective,
        configured_bytes=configured_bytes,
        free_bytes=free,
        is_tmpfs=on_tmpfs,
        clamped=effective != configured_bytes,
        reason=reason,
    )
