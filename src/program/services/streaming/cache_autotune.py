"""CineFlow Streaming Cache Auto-Tune and Watermark Optimization Engine.

Provides:
- Detection of host and container memory geometry (RAM, cgroups, tmpfs).
- Isolated synthetic sandbox microbenchmarks (isolated from live cache).
- Bounded auto-tune recommendation generator matching FilesystemModel invariants.
- Thread-safe AutoTuneJobManager with cancellation tokens and single-job concurrency gating.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
import uuid
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger("cineflow.cache_autotune")

AutoTuneProfile = Literal["conservative", "balanced", "aggressive"]
AutoTuneJobStatus = Literal["pending", "running", "completed", "failed", "cancelled"]


class OperationalStatus(str, Enum):
    NORMAL = "NORMAL"
    CONSTRAINED = "CONSTRAINED"
    INSUFFICIENT_CAPACITY = "INSUFFICIENT_CAPACITY"


class TopologyConfidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class CacheCapacityPolicyV1(BaseModel):
    """Encapsulates sizing multipliers, safety headroom reserves, and policy targets.

    Extracts operational multipliers away from raw physical measurement layers:
    - Physical facts are recorded first (FILESYSTEM_FACT, KERNEL_FACT).
    - Policy models apply named, versioned target transformations (FALLBACK_POLICY).
    - Sizing results are recorded as DERIVED with POLICY_TARGET or TOTAL_TARGET semantics.
    """

    policy_id: str = "cineflow.cache-capacity"
    policy_version: str = "1.0.0"
    tmpfs_available_fraction: float = 0.75
    memory_headroom_fraction: float = 0.60
    disk_hot_available_fraction: float = 0.50
    cgroup_unreadable_fallback_fraction: float = 0.50
    conservative_hot_fraction: float = 0.10
    balanced_hot_fraction: float = 0.20
    aggressive_hot_fraction: float = 0.35
    conservative_warm_fraction: float = 0.20
    conservative_warm_minimum_free_fraction: float = 0.15
    balanced_warm_fraction: float = 0.40
    aggressive_warm_fraction: float = 0.60
    critical_memory_pressure_fraction: float = 0.90
    warm_conservative_minimum_mb: int = 2048
    warm_aggressive_minimum_mb: int = 5120
    conservative_hot_minimum_mb: int = 64
    aggressive_hot_minimum_mb: int = 128
    balanced_hot_minimum_mb: int = 64
    conservative_hot_maximum_mb: int = 2048
    aggressive_hot_maximum_mb: int = 16384
    balanced_hot_maximum_mb: int = 8192
    conservative_warm_maximum_mb: int = 20480
    balanced_warm_minimum_mb: int = 4096
    balanced_warm_maximum_mb: int = 51200
    warm_minimum_free_mb: int = 1024
    conservative_warm_minimum_free_mb: int = 2048


# Default active capacity policy instance
DEFAULT_CAPACITY_POLICY: CacheCapacityPolicyV1 = CacheCapacityPolicyV1()

# Backward-compatibility aliases for module-level references
CGROUP_HEADROOM_SAFETY_FACTOR: float = DEFAULT_CAPACITY_POLICY.memory_headroom_fraction
TMPFS_HEADROOM_SAFETY_FACTOR: float = DEFAULT_CAPACITY_POLICY.tmpfs_available_fraction
NON_TMPFS_DISK_HOT_FACTOR: float = DEFAULT_CAPACITY_POLICY.disk_hot_available_fraction

# Minimum Operational HOT Working Set Constants:
# Base operational chunk size: 1 MiB (configurable via settings.stream.chunk_size_mb).
# Container header footprint: 256 KiB = 0.25 MiB (strictly separate from body chunks in Chunker).
# Guaranteed minimum adaptive prefetch window: 4 chunks (AdaptivePrefetchConfig.min_window_chunks).
# Preferred default prefetch window: 12 chunks (StreamModel.prefetch_chunks default).
# Maximum Remux prefetch window: 48 chunks (AdaptivePrefetchConfig.max_window_chunks).
# High-watermark reclaim threshold headroom: watermark factor requires working_set <= hot_capacity * (high_watermark / 100).
CHUNK_SIZE_MB: int = 1
HEADER_FOOTPRINT_MB: float = 0.25
MIN_PREFETCH_CHUNKS: int = 4
DEFAULT_PREFETCH_CHUNKS: int = 12
MAX_PREFETCH_CHUNKS: int = 48


def calculate_minimum_operational_hot_mb(
    concurrent_streams: int = 1,
    high_watermark_pct: float = 85.0,
    chunk_size_mb: int = CHUNK_SIZE_MB,
    min_prefetch_chunks: int = MIN_PREFETCH_CHUNKS,
) -> int:
    """Derive the absolute minimum HOT working set required to avoid playback rebuffering.

    Formula per stream:
      Single stream guaranteed working set: (chunk_size_mb * min_prefetch_chunks + HEADER_FOOTPRINT_MB)
      Container header chunk is exactly 256 KiB (0.25 MiB) and is held in cache for active media streams.
      Body chunks begin at byte 262,144 (header_size) and never double-count container header bytes.
      Under proactive watermark reclaim at high_watermark_pct (e.g. 85%), to prevent the cache
      engine from immediately demoting in-flight chunks before the player consumes them:
        hot_capacity >= ceil((stream_working_set * concurrent_streams) / (high_watermark_pct / 100))
      This provides the pure mathematical minimum operational capacity without arbitrary floors.
    """
    effective_chunk_size = max(1, chunk_size_mb)
    effective_prefetch = max(1, min_prefetch_chunks)
    per_stream_working_set = (
        effective_chunk_size * effective_prefetch
    ) + HEADER_FOOTPRINT_MB
    raw_working_set = per_stream_working_set * max(1, concurrent_streams)
    watermark_factor = max(0.50, min(0.95, high_watermark_pct / 100.0))
    # Minimum operational hot working set in MiB: pure ceil(raw_working_set / watermark_factor)
    return int(raw_working_set / watermark_factor + 0.999)


class PolicyMetadata(BaseModel):
    """Structured, explainable policy metadata attached to recommendations."""

    policy_id: str
    policy_version: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    reason: str
    confidence: float
    origin: str = "FALLBACK_POLICY"
    semantics: str = "POLICY_TARGET"


class AutoTuneRecommendation(BaseModel):
    profile: AutoTuneProfile
    system_memory_mb: int
    cgroup_limit_mb: int | None = None
    effective_memory_ceiling_mb: int
    disk_free_mb: int
    hot_tier_throughput_mb_s: float
    warm_tier_throughput_mb_s: float
    recommended_hot_cache_max_mb: int
    recommended_warm_cache_max_mb: int
    # Physical disk capacity is distinct from the operational recommendation.
    physical_hot_storage_ceiling_mb: int | None = None
    performance_confidence: TopologyConfidence = TopologyConfidence.HIGH
    effective_safe_hot_capacity_mb: int
    minimum_operational_hot_mb: int
    operational_status: OperationalStatus
    topology_confidence: TopologyConfidence
    recommended_hot_reserve_pct: float
    recommended_hot_watermark_high_pct: float
    recommended_hot_watermark_low_pct: float
    recommended_warm_reserve_pct: float
    recommended_warm_watermark_high_pct: float
    recommended_warm_watermark_low_pct: float
    recommended_warm_min_free_mb: int
    reasons: list[str] = Field(default_factory=list)
    policy_metadata: PolicyMetadata | None = None


class AutoTuneJobSnapshot(BaseModel):
    run_id: str
    profile: AutoTuneProfile
    status: AutoTuneJobStatus
    progress_pct: float
    current_step: str
    error: str | None = None
    created_at: float
    completed_at: float | None = None
    recommendation: AutoTuneRecommendation | None = None


def detect_cgroup_memory_limit() -> int | None:
    """Detect Docker / cgroup memory limit if running inside a container.

    Uses normalized CgroupCapabilities to cleanly detect finite boundaries
    without magic number sentinel guesses.
    """
    from program.services.streaming.runtime_profile import (
        CgroupCapabilities,
        LimitState,
    )

    caps = CgroupCapabilities.detect()
    if caps.memory_hard_boundary.state is LimitState.FINITE and isinstance(
        caps.memory_hard_boundary.value, int
    ):
        return caps.memory_hard_boundary.value // (1024 * 1024)
    return None


def detect_cgroup_memory_current() -> int | None:
    """Detect current memory consumption reported by cgroups.

    Uses normalized CgroupCapabilities to cleanly extract current usage.
    """
    from program.services.streaming.runtime_profile import (
        CgroupCapabilities,
        LimitState,
    )

    caps = CgroupCapabilities.detect()
    if caps.memory_current.state is LimitState.FINITE and isinstance(
        caps.memory_current.value, int
    ):
        return caps.memory_current.value // (1024 * 1024)
    return None


def detect_path_disk_space(target_path: Path | None = None) -> dict[str, Any]:
    """Detect total and free space in MB for a specific target directory.

    Delegates to runtime_profile.detect_storage_capacity to ensure consistent
    provenance, POSIX statvfs semantics (f_bavail * f_frsize), and error handling.
    """
    from program.services.streaming.runtime_profile import (
        LimitState,
        detect_storage_capacity,
    )

    target = target_path or Path.cwd()
    res = detect_storage_capacity(target)
    total_val = res["total_bytes"]
    avail_val = res["available_bytes"]

    if total_val.state is LimitState.FINITE and isinstance(
        total_val.value, (int, float)
    ):
        total_mb = int(total_val.value // (1024 * 1024))
    else:
        total_mb = 10240

    if avail_val.state is LimitState.FINITE and isinstance(
        avail_val.value, (int, float)
    ):
        free_mb = int(avail_val.value // (1024 * 1024))
    else:
        free_mb = 10240

    statvfs_available = "statvfs" in (avail_val.reason or "")
    out: dict[str, Any] = {
        "total_mb": total_mb,
        "free_mb": free_mb,
        "statvfs_available": statvfs_available,
    }
    if total_val.state is LimitState.UNKNOWN or avail_val.state is LimitState.UNKNOWN:
        out["error"] = total_val.reason or avail_val.reason or "Unknown storage error"
    return out


def detect_system_topology(
    warm_target_path: Path | None = None,
    hot_target_path: Path | None = None,
) -> dict[str, Any]:
    """Detect system and container memory, cgroup, tmpfs, and disk geometry.

    Acts as a compatibility projection around RuntimeDeploymentProfile (the canonical single source of truth),
    eliminating redundant OS queries and ensuring exact kernel fact provenance.

    Accounts for:
    - Host physical RAM total and available
    - cgroup limit (memory.max or memory.limit_in_bytes)
    - cgroup current consumption (memory.current or memory.usage_in_bytes)
    - hot cache volume (e.g. tmpfs) total and free space vs persistent disk
    - warm cache volume disk total and free space
    - topology confidence level (HIGH, MEDIUM, LOW)
    """
    from program.services.streaming.cache_sizing import is_tmpfs_path
    from program.services.streaming.runtime_profile import (
        LimitState,
        build_runtime_profile,
    )

    # Construct the canonical RuntimeDeploymentProfile (authoritative single-pass fact gathering)
    profile = build_runtime_profile(
        hot_path=hot_target_path,
        warm_path=warm_target_path,
    )

    # 1. Memory facts from canonical profile
    total_ram_val = profile.memory.get("total_bytes")
    if (
        total_ram_val
        and total_ram_val.state is LimitState.FINITE
        and isinstance(total_ram_val.value, (int, float))
    ):
        total_ram_mb = int(total_ram_val.value // (1024 * 1024))
    else:
        total_ram_mb = 16384

    avail_ram_val = profile.memory.get("available_bytes")
    if (
        avail_ram_val
        and avail_ram_val.state is LimitState.FINITE
        and isinstance(avail_ram_val.value, (int, float))
    ):
        available_ram_mb = int(avail_ram_val.value // (1024 * 1024))
    else:
        available_ram_mb = total_ram_mb

    # 2. Cgroup facts from profile
    caps = profile.cgroup
    cgroup_limit_mb: int | None = None
    if caps.memory_hard_boundary.state is LimitState.FINITE and isinstance(
        caps.memory_hard_boundary.value, int
    ):
        cgroup_limit_mb = caps.memory_hard_boundary.value // (1024 * 1024)

    cgroup_current_mb: int | None = None
    if caps.memory_current.state is LimitState.FINITE and isinstance(
        caps.memory_current.value, int
    ):
        cgroup_current_mb = caps.memory_current.value // (1024 * 1024)

    cgroup_high_mb: int | None = None
    if caps.memory_soft_boundary.state is LimitState.FINITE and isinstance(
        caps.memory_soft_boundary.value, int
    ):
        cgroup_high_mb = caps.memory_soft_boundary.value // (1024 * 1024)

    # Calculate effective memory ceiling from host RAM vs cgroup limit
    effective_mem_mb = total_ram_mb
    is_cgroup_active = False
    if cgroup_limit_mb is not None and cgroup_limit_mb < total_ram_mb:
        effective_mem_mb = cgroup_limit_mb
        is_cgroup_active = True

    # 3. Storage facts from profile
    warm_res = profile.warm_filesystem
    warm_avail_val = warm_res.get("available_bytes")
    warm_free_mb = (
        int(warm_avail_val.value // (1024 * 1024))
        if warm_avail_val
        and warm_avail_val.state is LimitState.FINITE
        and isinstance(warm_avail_val.value, (int, float))
        else 10240
    )

    is_hot_tmpfs = False
    tmpfs_free_mb: int | None = None
    tmpfs_total_mb: int | None = None
    hot_disk_free_mb: int | None = None
    hot_disk_total_mb: int | None = None

    if hot_target_path:
        is_hot_tmpfs = is_tmpfs_path(hot_target_path)
        hot_res = profile.hot_filesystem
        hot_tot_val = hot_res.get("total_bytes")
        hot_av_val = hot_res.get("available_bytes")
        h_tot_mb = (
            int(hot_tot_val.value // (1024 * 1024))
            if hot_tot_val
            and hot_tot_val.state is LimitState.FINITE
            and isinstance(hot_tot_val.value, (int, float))
            else 10240
        )
        h_fr_mb = (
            int(hot_av_val.value // (1024 * 1024))
            if hot_av_val
            and hot_av_val.state is LimitState.FINITE
            and isinstance(hot_av_val.value, (int, float))
            else 10240
        )

        if is_hot_tmpfs:
            tmpfs_free_mb = h_fr_mb
            tmpfs_total_mb = h_tot_mb
        else:
            hot_disk_free_mb = h_fr_mb
            hot_disk_total_mb = h_tot_mb

    # Physical cgroup headroom calculation:
    # Strictly max(0, cgroup_limit - cgroup_current) when BOTH are known and finite.
    # If cgroup_current is missing/UNKNOWN, cgroup_headroom_mb is strictly None (DERIVED headroom cannot be computed without CURRENT_MEASUREMENT).
    cgroup_headroom_mb: int | None = None
    if cgroup_limit_mb is not None and cgroup_current_mb is not None:
        cgroup_headroom_mb = max(0, cgroup_limit_mb - cgroup_current_mb)

    # Topology confidence determination:
    # HIGH: Full cgroup telemetry (or confirmed bare-metal without cgroup) AND valid filesystem geometries.
    # MEDIUM: Container detected with cgroup limit but missing memory.current telemetry.
    # LOW: Filesystem stat errors or ambiguous container environment.
    has_fs_errors = bool(
        (warm_avail_val and warm_avail_val.state is LimitState.UNKNOWN)
        or (
            hot_target_path
            and profile.hot_filesystem.get("available_bytes")
            and profile.hot_filesystem["available_bytes"].state is LimitState.UNKNOWN
        )
    )
    if has_fs_errors:
        confidence = TopologyConfidence.LOW
    elif is_cgroup_active and cgroup_current_mb is None:
        confidence = TopologyConfidence.MEDIUM
    else:
        confidence = TopologyConfidence.HIGH

    return {
        "system_memory_mb": total_ram_mb,
        "available_memory_mb": available_ram_mb,
        "cgroup_limit_mb": cgroup_limit_mb,
        "cgroup_current_mb": cgroup_current_mb,
        "cgroup_high_mb": cgroup_high_mb,
        "cgroup_headroom_mb": cgroup_headroom_mb,
        "effective_memory_ceiling_mb": effective_mem_mb,
        "is_cgroup_active": is_cgroup_active,
        "is_hot_tmpfs": is_hot_tmpfs,
        "tmpfs_total_mb": tmpfs_total_mb,
        "tmpfs_free_mb": tmpfs_free_mb,
        "hot_disk_total_mb": hot_disk_total_mb,
        "hot_disk_free_mb": hot_disk_free_mb,
        "disk_free_mb": warm_free_mb,
        "topology_confidence": confidence,
        "_runtime_profile": profile,
    }


def run_synthetic_benchmark(
    sandbox_dir: Path,
    total_bytes: int = 16 * 1024 * 1024,
    chunk_size: int = 1024 * 1024,
    cancel_event: threading.Event | None = None,
) -> float:
    """Run an isolated read/write throughput benchmark in the sandbox.

    Returns measured throughput in MB/s.
    """
    if cancel_event and cancel_event.is_set():
        raise InterruptedError("Cancelled")

    sandbox_dir.mkdir(parents=True, exist_ok=True)
    test_file = sandbox_dir / f"bench_{uuid.uuid4().hex[:8]}.bin"
    payload = os.urandom(chunk_size)
    num_chunks = max(1, total_bytes // chunk_size)

    start_time = time.monotonic()
    try:
        with open(test_file, "wb") as f:
            for _ in range(num_chunks):
                if cancel_event and cancel_event.is_set():
                    raise InterruptedError("Cancelled")
                f.write(payload)
            f.flush()
            os.fsync(f.fileno())

        if cancel_event and cancel_event.is_set():
            raise InterruptedError("Cancelled")

        with open(test_file, "rb") as f:
            while True:
                if cancel_event and cancel_event.is_set():
                    raise InterruptedError("Cancelled")
                chunk = f.read(chunk_size)
                if not chunk:
                    break

        elapsed = time.monotonic() - start_time
        if elapsed <= 0:
            elapsed = 0.001

        # Total I/O = 2 * total_bytes (write + read)
        mb_transferred = (total_bytes * 2) / (1024 * 1024)
        throughput = mb_transferred / elapsed
        return round(throughput, 2)
    finally:
        try:
            if test_file.exists():
                test_file.unlink()
        except Exception:
            pass


def compute_recommendation(
    profile: AutoTuneProfile,
    topology: dict[str, Any],
    hot_speed_mb_s: float,
    warm_speed_mb_s: float,
    chunk_size_mb: int = CHUNK_SIZE_MB,
    min_prefetch_chunks: int = MIN_PREFETCH_CHUNKS,
    concurrent_streams: int = 1,
    capacity_test_record: Any = None,
    current_runtime_profile: Any = None,
    capacity_policy: CacheCapacityPolicyV1 = DEFAULT_CAPACITY_POLICY,
) -> AutoTuneRecommendation:
    """Compute safe watermark & cache limits using measured facts and named policy.

    Guarantees FilesystemModel validation invariants:
    - 0 < low < high < 100
    - high - low >= 5.0
    - high <= 100 - reserve
    - hot_cache_max_mb constrained by cgroup ceiling, cgroup headroom, and tmpfs free space without double-counting
    - If capacity_test_record and current_runtime_profile are provided, empirical evidence
      is validated via is_calibration_valid_for_profile(); stale records are invalidated
      and excluded from recommendation synthesis.
    """
    effective_mem_mb = topology["effective_memory_ceiling_mb"]
    disk_free_mb = topology["disk_free_mb"]
    current_hot_mb = max(0, int(topology.get("current_hot_mb", 0)))
    current_warm_mb = max(0, int(topology.get("current_warm_mb", 0)))
    tmpfs_free_mb = topology.get("tmpfs_free_mb")
    cgroup_headroom_mb = topology.get("cgroup_headroom_mb")
    is_hot_tmpfs = topology.get("is_hot_tmpfs", tmpfs_free_mb is not None)
    hot_disk_free_mb = topology.get("hot_disk_free_mb")
    confidence: TopologyConfidence = topology.get(
        "topology_confidence", TopologyConfidence.HIGH
    )

    reasons: list[str] = []

    # Derive operational sufficiency baseline for the hot tier
    profile_high_watermark = (
        75.0
        if profile == "conservative"
        else (92.0 if profile == "aggressive" else 85.0)
    )
    minimum_operational_hot_mb = calculate_minimum_operational_hot_mb(
        concurrent_streams=concurrent_streams,
        high_watermark_pct=profile_high_watermark,
        chunk_size_mb=chunk_size_mb,
        min_prefetch_chunks=min_prefetch_chunks,
    )

    # Base sizing: start with profile percentage of memory ceiling.
    # Sizing uses a preferred baseline minimum of 64 MiB (Option A), but this is
    # subject to strict safety clamping against effective safe hot capacity below.
    if profile == "conservative":
        hot_reserve_pct = 20.0
        hot_high = 75.0
        hot_low = 60.0

        warm_reserve_pct = 20.0
        warm_high = 75.0
        warm_low = 60.0
        warm_min_free = max(
            capacity_policy.conservative_warm_minimum_free_mb,
            int(disk_free_mb * capacity_policy.conservative_warm_minimum_free_fraction),
        )

        # Base sizing: policy fraction of memory, with a policy-defined preferred minimum and cap.
        hot_cache_mb = max(
            capacity_policy.conservative_hot_minimum_mb,
            min(
                capacity_policy.conservative_hot_maximum_mb,
                int(effective_mem_mb * capacity_policy.conservative_hot_fraction),
            ),
        )
        warm_cache_mb = max(
            capacity_policy.warm_conservative_minimum_mb,
            min(
                capacity_policy.conservative_warm_maximum_mb,
                int(disk_free_mb * capacity_policy.conservative_warm_fraction),
            ),
        )

        reasons.append(
            "Applied conservative tier safety buffers (20% reserve headroom)."
        )

    elif profile == "aggressive":
        hot_reserve_pct = 5.0
        hot_high = 92.0
        hot_low = 75.0

        warm_reserve_pct = 5.0
        warm_high = 92.0
        warm_low = 75.0
        warm_min_free = capacity_policy.warm_minimum_free_mb

        # Base sizing uses the policy fraction, minimum and maximum for this profile.
        hot_cache_mb = max(
            capacity_policy.aggressive_hot_minimum_mb,
            min(
                capacity_policy.aggressive_hot_maximum_mb,
                int(effective_mem_mb * capacity_policy.aggressive_hot_fraction),
            ),
        )
        warm_cache_mb = max(
            capacity_policy.warm_aggressive_minimum_mb,
            int(disk_free_mb * capacity_policy.aggressive_warm_fraction),
        )

        reasons.append(
            "Allocated aggressive cache budget to maximize 4K Remux read burst caching."
        )

    else:  # balanced
        hot_reserve_pct = 10.0
        hot_high = 85.0
        hot_low = 70.0

        warm_reserve_pct = 10.0
        warm_high = 85.0
        warm_low = 70.0
        warm_min_free = capacity_policy.warm_minimum_free_mb

        # Base sizing uses the policy fraction, minimum and maximum for this profile.
        hot_cache_mb = max(
            capacity_policy.balanced_hot_minimum_mb,
            min(
                capacity_policy.balanced_hot_maximum_mb,
                int(effective_mem_mb * capacity_policy.balanced_hot_fraction),
            ),
        )
        # Warm cache: policy fraction with profile-specific minimum and maximum.
        warm_cache_mb = max(
            capacity_policy.balanced_warm_minimum_mb,
            min(
                capacity_policy.balanced_warm_maximum_mb,
                int(disk_free_mb * capacity_policy.balanced_warm_fraction),
            ),
        )

        reasons.append(
            "Configured balanced operational parameters with 10% headroom reserve."
        )

    # Totalize available headroom exactly once.
    # When hot cache is on tmpfs:
    # 1. cgroup headroom (or memory headroom): cgroup_headroom_mb = cgroup_limit - memory.current.
    #    If cgroup is active and headroom is known, safe additional headroom = headroom * CGROUP_HEADROOM_SAFETY_FACTOR.
    #    Total possible = current_hot_mb + additional headroom.
    #    If cgroup is active but memory.current is not readable, fall back to 50% of cgroup limit.
    # 2. tmpfs free space: safe additional headroom = tmpfs_free_mb * TMPFS_HEADROOM_SAFETY_FACTOR.
    #    Total possible = current_hot_mb + additional headroom.
    # When hot cache is on persistent disk:
    #    Constrained by disk capacity (50% factor). If hot_disk_free_mb is not provided, falls back to disk_free_mb.
    #    If neither or unbounded, memory ceiling acts as baseline.
    effective_safe_hot_capacity_mb = effective_mem_mb
    cgroup_cap_mb: int | None = None
    tmpfs_cap_mb: int | None = None
    disk_hot_cap_mb: int | None = None
    physical_hot_storage_ceiling_mb: int | None = None
    performance_confidence: TopologyConfidence = TopologyConfidence(confidence)

    if is_hot_tmpfs:
        if cgroup_headroom_mb is not None:
            cgroup_cap_mb = current_hot_mb + max(
                0, int(cgroup_headroom_mb * capacity_policy.memory_headroom_fraction)
            )
            effective_safe_hot_capacity_mb = min(
                effective_safe_hot_capacity_mb, cgroup_cap_mb
            )
        elif (
            topology.get("is_cgroup_active")
            and topology.get("cgroup_limit_mb") is not None
        ):
            cgroup_cap_mb = current_hot_mb + max(
                0,
                int(
                    int(topology["cgroup_limit_mb"])
                    * capacity_policy.cgroup_unreadable_fallback_fraction
                ),
            )
            effective_safe_hot_capacity_mb = min(
                effective_safe_hot_capacity_mb, cgroup_cap_mb
            )
            reasons.append(
                "Container cgroup memory.current unavailable; applied conservative "
                f"{capacity_policy.cgroup_unreadable_fallback_fraction:.0%} cgroup limit safety cap."
            )

        if tmpfs_free_mb is not None:
            tmpfs_cap_mb = current_hot_mb + max(
                0, int(tmpfs_free_mb * capacity_policy.tmpfs_available_fraction)
            )
            effective_safe_hot_capacity_mb = min(
                effective_safe_hot_capacity_mb, tmpfs_cap_mb
            )
    else:
        # Non-tmpfs persistent disk hot tier
        effective_hot_disk_free = (
            hot_disk_free_mb if hot_disk_free_mb is not None else disk_free_mb
        )
        if effective_hot_disk_free is not None:
            physical_hot_storage_ceiling_mb = current_hot_mb + max(
                0, int(effective_hot_disk_free)
            )
            disk_hot_cap_mb = current_hot_mb + max(
                0,
                int(
                    effective_hot_disk_free
                    * capacity_policy.disk_hot_available_fraction
                ),
            )
            effective_safe_hot_capacity_mb = min(
                effective_safe_hot_capacity_mb, disk_hot_cap_mb
            )
            reasons.append(
                f"Hot cache on persistent disk: constrained by disk capacity (~{disk_hot_cap_mb}MB safe total target)."
            )

    # Physical safety strictly overrides preference:
    # recommended_hot_cache_max_mb MUST NEVER exceed effective_safe_hot_capacity_mb.
    if hot_cache_mb > effective_safe_hot_capacity_mb:
        if (
            cgroup_cap_mb is not None
            and effective_safe_hot_capacity_mb == cgroup_cap_mb
        ):
            reasons.append(
                f"Constrained hot cache from {hot_cache_mb}MB to {cgroup_cap_mb}MB due to cgroup available headroom ({cgroup_headroom_mb}MB)."
            )
        elif (
            tmpfs_cap_mb is not None and effective_safe_hot_capacity_mb == tmpfs_cap_mb
        ):
            reasons.append(
                f"Constrained hot cache from {hot_cache_mb}MB to {tmpfs_cap_mb}MB due to tmpfs available capacity ({tmpfs_free_mb}MB)."
            )
        elif (
            disk_hot_cap_mb is not None
            and effective_safe_hot_capacity_mb == disk_hot_cap_mb
        ):
            reasons.append(
                f"Constrained hot cache from {hot_cache_mb}MB to {disk_hot_cap_mb}MB due to hot tier disk free space ({hot_disk_free_mb}MB)."
            )
        else:
            reasons.append(
                f"Constrained hot cache from {hot_cache_mb}MB to {effective_safe_hot_capacity_mb}MB due to effective memory ceiling."
            )
        hot_cache_mb = effective_safe_hot_capacity_mb

    # Persistent disk physical capacity stays independent of RAM. Cgroup memory pressure
    # influences only operational confidence/status because memory.current includes page cache.
    if not is_hot_tmpfs:
        cgroup_limit_mb = topology.get("cgroup_limit_mb")
        cgroup_current_mb = topology.get("cgroup_current_mb")
        cgroup_high_mb = topology.get("cgroup_high_mb")
        if (
            isinstance(cgroup_limit_mb, int)
            and cgroup_limit_mb > 0
            and isinstance(cgroup_current_mb, int)
        ):
            pressure_ratio = cgroup_current_mb / cgroup_limit_mb
            high_reached = (
                isinstance(cgroup_high_mb, int) and cgroup_current_mb >= cgroup_high_mb
            )
            if (
                pressure_ratio >= capacity_policy.critical_memory_pressure_fraction
                or high_reached
            ):
                performance_confidence = TopologyConfidence.LOW
                reasons.append(
                    "High cgroup memory pressure (memory.current / memory.high) reduces performance confidence; physical disk capacity is unchanged."
                )
        elif topology.get("is_cgroup_active"):
            performance_confidence = TopologyConfidence.MEDIUM
            reasons.append(
                "Cgroup memory-pressure telemetry unavailable; operational performance confidence reduced, physical disk capacity unchanged."
            )

    # Determine Operational Status:
    # NORMAL: recommended capacity >= preferred 64 MiB AND >= minimum_operational_hot_mb.
    # CONSTRAINED: safe capacity is between minimum_operational_hot_mb and preferred 64 MiB.
    # INSUFFICIENT_CAPACITY: safe capacity is strictly below minimum_operational_hot_mb (playback risks demotion thrashing / underflow).
    if hot_cache_mb < minimum_operational_hot_mb:
        operational_status = OperationalStatus.INSUFFICIENT_CAPACITY
        reasons.append(
            f"INSUFFICIENT_CAPACITY: Safe hot budget ({hot_cache_mb}MB) is below the minimum operational working set "
            f"({minimum_operational_hot_mb}MB) required to sustain playback without demotion thrashing."
        )
    elif hot_cache_mb < 64:
        operational_status = OperationalStatus.CONSTRAINED
        reasons.append(
            f"CONSTRAINED: Hot cache budget ({hot_cache_mb}MB) meets operational minimum ({minimum_operational_hot_mb}MB) "
            f"but is below preferred 64MB streaming baseline."
        )
    else:
        operational_status = OperationalStatus.NORMAL

    if (
        not is_hot_tmpfs
        and performance_confidence is TopologyConfidence.LOW
        and hot_cache_mb >= 64
    ):
        operational_status = OperationalStatus.CONSTRAINED

    # Guard invariant 1: high <= 100 - reserve
    max_hot_high = 100.0 - hot_reserve_pct
    hot_high = min(hot_high, max_hot_high)

    max_warm_high = 100.0 - warm_reserve_pct
    warm_high = min(warm_high, max_warm_high)

    # Warm capacity is a total target: preserve current occupancy and add only
    # safely allocatable physical headroom after the policy reserve.
    warm_physical_available = max(
        0, int(topology.get("warm_physical_available_mb", disk_free_mb))
    )
    warm_cache_mb = current_warm_mb + max(0, warm_physical_available - warm_min_free)

    # Guard invariant 2: high - low >= 5.0
    if (hot_high - hot_low) < 5.0:
        hot_low = hot_high - 5.0

    if (warm_high - warm_low) < 5.0:
        warm_low = warm_high - 5.0

    reasons.append(
        f"Detected {effective_mem_mb} MB effective memory ceiling (cgroup: {topology.get('cgroup_limit_mb')})."
    )
    reasons.append(
        f"Benchmark throughput: Hot tier ~{hot_speed_mb_s} MB/s, Warm tier ~{warm_speed_mb_s} MB/s."
    )

    # Invalidation verification for empirical CapacityTestRecord
    if capacity_test_record is not None and current_runtime_profile is not None:
        try:
            from program.services.streaming.runtime_profile import (
                is_calibration_valid_for_profile,
            )

            is_valid, invalidated_domains = is_calibration_valid_for_profile(
                capacity_test_record, current_runtime_profile
            )
            if is_valid:
                reasons.append(
                    "Empirical CapacityTestRecord validated for current runtime profile and integrated."
                )
            else:
                reasons.append(
                    f"Empirical CapacityTestRecord invalidated due to environment/config drift: {sorted(invalidated_domains)} (record discarded)."
                )
        except Exception as exc:
            reasons.append(f"Failed to validate empirical record: {exc}")

    return AutoTuneRecommendation(
        profile=profile,
        system_memory_mb=topology["system_memory_mb"],
        cgroup_limit_mb=topology.get("cgroup_limit_mb"),
        effective_memory_ceiling_mb=effective_mem_mb,
        disk_free_mb=disk_free_mb,
        hot_tier_throughput_mb_s=hot_speed_mb_s,
        warm_tier_throughput_mb_s=warm_speed_mb_s,
        recommended_hot_cache_max_mb=hot_cache_mb,
        recommended_warm_cache_max_mb=warm_cache_mb,
        physical_hot_storage_ceiling_mb=physical_hot_storage_ceiling_mb,
        performance_confidence=performance_confidence,
        effective_safe_hot_capacity_mb=effective_safe_hot_capacity_mb,
        minimum_operational_hot_mb=minimum_operational_hot_mb,
        operational_status=operational_status,
        topology_confidence=confidence,
        recommended_hot_reserve_pct=hot_reserve_pct,
        recommended_hot_watermark_high_pct=hot_high,
        recommended_hot_watermark_low_pct=hot_low,
        recommended_warm_reserve_pct=warm_reserve_pct,
        recommended_warm_watermark_high_pct=warm_high,
        recommended_warm_watermark_low_pct=warm_low,
        recommended_warm_min_free_mb=warm_min_free,
        reasons=reasons,
        policy_metadata=PolicyMetadata(
            policy_id=capacity_policy.policy_id,
            policy_version=capacity_policy.policy_version,
            parameters={
                "profile": profile,
                "physical_facts": {
                    "hot_disk_free_mb": hot_disk_free_mb,
                    "cgroup_memory_current_mb": topology.get("cgroup_current_mb"),
                    "cgroup_memory_high_mb": topology.get("cgroup_high_mb"),
                    "cgroup_headroom_mb": cgroup_headroom_mb,
                    "policy_memory_allowance_mb": (
                        cgroup_cap_mb
                        if (
                            cgroup_headroom_mb is None
                            and topology.get("is_cgroup_active")
                        )
                        else None
                    ),
                    "filesystem_fact_origin": "FILESYSTEM_FACT",
                    "kernel_fact_origin": "KERNEL_FACT",
                },
                "result_origin": "DERIVED",
                "current_hot_mb": current_hot_mb,
                "current_warm_mb": current_warm_mb,
                "hot_tmpfs": is_hot_tmpfs,
                "policy": capacity_policy.model_dump(),
                "hot_safety_factor": (
                    capacity_policy.tmpfs_available_fraction
                    if is_hot_tmpfs
                    else capacity_policy.disk_hot_available_fraction
                ),
                "physical_hot_storage_ceiling_mb": physical_hot_storage_ceiling_mb,
                "operational_hot_recommendation_mb": hot_cache_mb,
                "performance_confidence": performance_confidence.value,
                "warm_min_free_mb": warm_min_free,
            },
            reason="Profile policy constrained by totalized available filesystem and memory capacity.",
            confidence={
                TopologyConfidence.HIGH: 0.95,
                TopologyConfidence.MEDIUM: 0.75,
                TopologyConfidence.LOW: 0.5,
            }[confidence],
        ),
    )


class AutoTuneJob:
    """Represents a single auto-tune execution."""

    run_id: str
    profile: AutoTuneProfile
    status: AutoTuneJobStatus
    progress_pct: float
    current_step: str
    error: str | None
    created_at: float
    completed_at: float | None
    recommendation: AutoTuneRecommendation | None
    cancel_event: threading.Event
    thread: threading.Thread | None

    def __init__(self, run_id: str, profile: AutoTuneProfile):
        self.run_id = run_id
        self.profile = profile
        self.status = "pending"
        self.progress_pct = 0.0
        self.current_step = "Initialized"
        self.error = None
        self.created_at = time.time()
        self.completed_at = None
        self.recommendation = None
        self.cancel_event = threading.Event()
        self.thread = None

    def snapshot(self) -> AutoTuneJobSnapshot:
        return AutoTuneJobSnapshot(
            run_id=self.run_id,
            profile=self.profile,
            status=self.status,
            progress_pct=self.progress_pct,
            current_step=self.current_step,
            error=self.error,
            created_at=self.created_at,
            completed_at=self.completed_at,
            recommendation=self.recommendation,
        )


class AutoTuneJobManager:
    """Thread-safe orchestrator for cache auto-tune jobs."""

    def __init__(self):
        self._lock = threading.RLock()
        self._jobs: dict[str, AutoTuneJob] = {}

    def get_job(self, run_id: str) -> AutoTuneJob | None:
        with self._lock:
            return self._jobs.get(run_id)

    def get_job_snapshot(self, run_id: str) -> AutoTuneJobSnapshot | None:
        with self._lock:
            job = self._jobs.get(run_id)
            return job.snapshot() if job else None

    def get_active_job(self) -> AutoTuneJob | None:
        with self._lock:
            for job in self._jobs.values():
                if job.status in ("pending", "running"):
                    return job
            return None

    def cancel_job(self, run_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(run_id)
            if not job:
                return False
            if job.status in ("pending", "running"):
                job.cancel_event.set()
                job.status = "cancelled"
                job.current_step = "Cancelled by user request"
                job.completed_at = time.time()
                return True
            return False

    def start_job(
        self,
        profile: AutoTuneProfile = "balanced",
        sandbox_base_dir: Path | None = None,
        bench_bytes: int = 8 * 1024 * 1024,
    ) -> AutoTuneJob:
        with self._lock:
            active = self.get_active_job()
            if active:
                raise RuntimeError(
                    f"An auto-tune job ({active.run_id}) is already in progress ({active.status})"
                )

            run_id = uuid.uuid4().hex[:12]
            job = AutoTuneJob(run_id=run_id, profile=profile)
            self._jobs[run_id] = job

            def _worker():
                self._execute_job(
                    job=job,
                    sandbox_base_dir=sandbox_base_dir,
                    bench_bytes=bench_bytes,
                )

            thread = threading.Thread(
                target=_worker,
                name=f"AutoTuneWorker-{run_id}",
                daemon=True,
            )
            job.thread = thread
            thread.start()
            return job

    def _execute_job(
        self,
        job: AutoTuneJob,
        sandbox_base_dir: Path | None,
        bench_bytes: int,
    ) -> None:
        job.status = "running"
        job.progress_pct = 5.0
        job.current_step = "Detecting host and container topology"

        try:
            if job.cancel_event.is_set():
                raise InterruptedError("Cancelled")

            topology = detect_system_topology()
            job.progress_pct = 20.0

            # Execute synthetic benchmarks inside an isolated sandbox directory
            job.current_step = "Preparing isolated benchmark sandbox"
            with tempfile.TemporaryDirectory(
                prefix="cineflow_autotune_",
                dir=str(sandbox_base_dir) if sandbox_base_dir else None,
            ) as sandbox_tmp:
                sandbox_path = Path(sandbox_tmp)
                hot_sandbox = sandbox_path / "hot"
                warm_sandbox = sandbox_path / "warm"

                if job.cancel_event.is_set():
                    raise InterruptedError("Cancelled")

                job.current_step = "Benchmarking hot tier memory/tmpfs I/O"
                job.progress_pct = 40.0
                hot_speed = run_synthetic_benchmark(
                    sandbox_dir=hot_sandbox,
                    total_bytes=bench_bytes,
                    chunk_size=1024 * 1024,
                    cancel_event=job.cancel_event,
                )

                if job.cancel_event.is_set():
                    raise InterruptedError("Cancelled")

                job.current_step = "Benchmarking warm tier disk I/O"
                job.progress_pct = 70.0
                warm_speed = run_synthetic_benchmark(
                    sandbox_dir=warm_sandbox,
                    total_bytes=bench_bytes,
                    chunk_size=1024 * 1024,
                    cancel_event=job.cancel_event,
                )

            if job.cancel_event.is_set():
                raise InterruptedError("Cancelled")

            job.current_step = "Synthesizing safe cache recommendations"
            job.progress_pct = 90.0

            # Inspect active runtime streaming geometry from settings if available
            runtime_chunk_size_mb = CHUNK_SIZE_MB
            try:
                from program.settings import settings_manager

                if hasattr(settings_manager, "settings") and hasattr(
                    settings_manager.settings, "stream"
                ):
                    runtime_chunk_size_mb = max(
                        1, getattr(settings_manager.settings.stream, "chunk_size_mb", 1)
                    )
            except Exception:
                pass

            recommendation = compute_recommendation(
                profile=job.profile,
                topology=topology,
                hot_speed_mb_s=hot_speed,
                warm_speed_mb_s=warm_speed,
                chunk_size_mb=runtime_chunk_size_mb,
                current_runtime_profile=topology.get("_runtime_profile"),
            )

            job.recommendation = recommendation
            job.progress_pct = 100.0
            job.current_step = "Completed successfully"
            job.status = "completed"
            job.completed_at = time.time()

        except InterruptedError:
            job.status = "cancelled"
            job.current_step = "Cancelled by user"
            job.completed_at = time.time()
        except Exception as e:
            logger.exception("Auto-tune job %s failed", job.run_id)
            job.status = "failed"
            job.error = str(e)
            job.current_step = f"Failed: {e}"
            job.completed_at = time.time()


# Global singleton instance
autotune_job_manager = AutoTuneJobManager()
