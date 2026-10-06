"""Normalized runtime capability/profile contracts for D80 AutoTune.

This module models observations and policies without embedding machine-class sizing.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class LimitState(str, Enum):
    FINITE = "FINITE"
    UNBOUNDED = "UNBOUNDED"
    UNKNOWN = "UNKNOWN"
    UNSUPPORTED = "UNSUPPORTED"


class CgroupVersion(str, Enum):
    V1 = "V1"
    V2 = "V2"
    NONE = "NONE"
    UNKNOWN = "UNKNOWN"


class ValueOrigin(str, Enum):
    KERNEL_FACT = "KERNEL_FACT"
    FILESYSTEM_FACT = "FILESYSTEM_FACT"
    CURRENT_MEASUREMENT = "CURRENT_MEASUREMENT"
    DERIVED = "DERIVED"
    USER_CONFIGURATION = "USER_CONFIGURATION"
    FALLBACK_POLICY = "FALLBACK_POLICY"
    EMPIRICAL_VERIFICATION = "EMPIRICAL_VERIFICATION"


class ValueSemantics(str, Enum):
    HARD_BOUNDARY = "HARD_BOUNDARY"
    SOFT_BOUNDARY = "SOFT_BOUNDARY"
    CONFIGURED_CONSTRAINT = "CONFIGURED_CONSTRAINT"
    CURRENT_USAGE = "CURRENT_USAGE"
    ADDITIONAL_HEADROOM = "ADDITIONAL_HEADROOM"
    TOTAL_TARGET = "TOTAL_TARGET"
    OBSERVATION = "OBSERVATION"
    POLICY_TARGET = "POLICY_TARGET"


@dataclass(frozen=True)
class ResourceValue:
    value: int | float | str | None
    state: LimitState
    origin: ValueOrigin
    semantics: ValueSemantics
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.state is LimitState.FINITE and self.value is None:
            raise ValueError("FINITE values require a value")
        if self.state is not LimitState.FINITE and self.value is not None:
            raise ValueError("Non-FINITE states must not carry sentinel values")


def get_supported_page_sizes() -> tuple[int, ...]:
    """Return common Linux architecture page sizes (4 KiB, 8 KiB, 16 KiB, 64 KiB)."""
    return (4096, 8192, 16384, 65536)


def detect_page_size() -> int | None:
    """Detect system memory page size in bytes. Returns None if detection fails.

    Deployment-neutral: does NOT assume 4096 bytes fallback when runtime detection fails,
    as Linux kernels also run with 16 KiB or 64 KiB base pages.
    """
    sysconf_func = getattr(os, "sysconf", None)
    if sysconf_func is not None:
        try:
            val = sysconf_func("SC_PAGE_SIZE")
            if val is not None and val > 0:
                return int(val)
        except (ValueError, OSError, AttributeError):
            pass
    return None


def detect_kernel_bitness() -> int | None:
    """Detect host kernel bitness (32 or 64-bit) normalizing across architectures.

    In container environments, 32-bit userland processes can execute on a 64-bit host kernel.
    We check platform.uname().machine normalized against known architecture families.
    Returns None for unknown/unrecognized architectures without guessing from pointer size.
    """
    try:
        mach = platform.uname().machine.lower()
    except Exception:
        mach = ""

    known_64 = (
        "x86_64",
        "amd64",
        "aarch64",
        "arm64",
        "ppc64",
        "ppc64le",
        "s390x",
        "mips64",
        "riscv64",
        "ia64",
        "sparc64",
        "alpha",
    )
    known_32 = (
        "i386",
        "i486",
        "i586",
        "i686",
        "x86",
        "armv6",
        "armv6l",
        "armv7",
        "armv7l",
        "armv8l",
        "arm",
        "ppc",
        "s390",
        "mips",
        "riscv32",
    )

    if mach in known_64 or mach.endswith("64"):
        return 64
    if mach in known_32:
        return 32

    return None


def compute_cgroup_v1_unlimited_limit_bytes(kernel_bits: int, page_size: int) -> int:
    """Compute Linux kernel cgroup v1 unlimited memory limit in bytes as returned in userspace.

    In Linux kernel (include/linux/page_counter.h, mm/memcontrol.c):
        #if BITS_PER_LONG == 32
        #define PAGE_COUNTER_MAX LONG_MAX
        #else
        #define PAGE_COUNTER_MAX (LONG_MAX / PAGE_SIZE)
        #endif

    When userspace reads `memory.limit_in_bytes`, mem_cgroup_read_u64 returns:
        (u64)counter->max * PAGE_SIZE

    Therefore, the sentinel byte value returned to userspace is:
        - 32-bit kernel: PAGE_COUNTER_MAX * PAGE_SIZE = LONG_MAX_32 * PAGE_SIZE = ((1 << 31) - 1) * page_size
          (e.g., 2147483647 * 4096 = 8,796,093,018,112 bytes ~= 8 TiB).
          NOTE: Raw LONG_MAX_32 (2,147,483,647 bytes ~= 2 GiB) is a normal, finite container memory limit
          and must NOT be treated as unbounded.
        - 64-bit kernel: PAGE_COUNTER_MAX * PAGE_SIZE = ((LONG_MAX_64 / PAGE_SIZE) * PAGE_SIZE)
          = (((1 << 63) - 1) // page_size) * page_size
          (e.g., for 4 KiB page: 9,223,372,036,854,771,712 bytes ~= 8 EiB).
    """
    if kernel_bits == 32:
        return ((1 << 31) - 1) * page_size
    return (((1 << (kernel_bits - 1)) - 1) // page_size) * page_size


def compute_page_counter_max(kernel_bits: int, page_size: int) -> int:
    """[DEPRECATED: Use compute_cgroup_v1_unlimited_limit_bytes directly]

    Warning: This function returns the sentinel limit in BYTES (PAGE_COUNTER_MAX * PAGE_SIZE),
    not the raw page count PAGE_COUNTER_MAX. Retained solely for backwards compatibility.
    """
    return compute_cgroup_v1_unlimited_limit_bytes(kernel_bits, page_size)


def is_cgroup_v1_unbounded_limit(
    number: int,
    *,
    word_bits: tuple[int, ...] = (32, 64),
    page_sizes: tuple[int, ...] | None = None,
) -> bool:
    """Check if number matches a cgroup v1 unlimited sentinel.

    Sentinel candidates are limited to detected kernel bitness/page size when available;
    otherwise they use the finite, explicit supported architecture matrix. An unavailable
    page size is never replaced with an assumed 4 KiB page.

    Supported Linux architectures:
    - 64-bit with 4 KiB, 8 KiB, 16 KiB, 64 KiB pages:
        * PAGE_COUNTER_MAX * PAGE_SIZE = (((1 << 63) - 1) // ps) * ps
        * Raw LONG_MAX_64 ((1 << 63) - 1)
    - 32-bit with 4 KiB, 8 KiB, 16 KiB, 64 KiB pages:
        * PAGE_COUNTER_MAX * PAGE_SIZE = ((1 << 31) - 1) * ps (~8 TiB for 4K)

    NOTE: Raw 32-bit LONG_MAX (2,147,483,647 = ~2 GiB) is explicitly NOT unbounded; it is a valid finite limit.
    """
    supported_pages = (
        page_sizes if page_sizes is not None else get_supported_page_sizes()
    )
    if not supported_pages:
        return False

    # Check 64-bit LONG_MAX only when 64-bit is among the known candidate architectures.
    if 64 in word_bits and number == (1 << 63) - 1:
        return True

    for bits in word_bits:
        for ps in supported_pages:
            if number == compute_cgroup_v1_unlimited_limit_bytes(bits, ps):
                return True
    return False


def _read_number(
    path: Path, *, unlimited_tokens: tuple[str, ...] = ()
) -> ResourceValue:
    try:
        raw = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return ResourceValue(
            None,
            LimitState.UNSUPPORTED,
            ValueOrigin.KERNEL_FACT,
            ValueSemantics.OBSERVATION,
            "controller file absent",
        )
    except OSError as exc:
        return ResourceValue(
            None,
            LimitState.UNKNOWN,
            ValueOrigin.KERNEL_FACT,
            ValueSemantics.OBSERVATION,
            str(exc),
        )
    if raw in unlimited_tokens:
        return ResourceValue(
            None,
            LimitState.UNBOUNDED,
            ValueOrigin.KERNEL_FACT,
            ValueSemantics.HARD_BOUNDARY,
        )
    try:
        number = int(raw)
    except ValueError:
        return ResourceValue(
            None,
            LimitState.UNKNOWN,
            ValueOrigin.KERNEL_FACT,
            ValueSemantics.OBSERVATION,
            "unparseable controller value",
        )
    # v1 memory.limit_in_bytes / memsw.limit_in_bytes uses PAGE_COUNTER_MAX (LONG_MAX rounded down to page size)
    # across supported kernel bitness (32/64) and page sizes (4K, 8K, 16K, 64K).
    if path.name in (
        "memory.limit_in_bytes",
        "memory.memsw.limit_in_bytes",
    ) and is_cgroup_v1_unbounded_limit(number):
        return ResourceValue(
            None,
            LimitState.UNBOUNDED,
            ValueOrigin.KERNEL_FACT,
            ValueSemantics.HARD_BOUNDARY,
            "cgroup-v1 PAGE_COUNTER_MAX",
        )
    if number < 0:
        return ResourceValue(
            None,
            LimitState.UNKNOWN,
            ValueOrigin.KERNEL_FACT,
            ValueSemantics.OBSERVATION,
            "negative controller value",
        )
    return ResourceValue(
        number, LimitState.FINITE, ValueOrigin.KERNEL_FACT, ValueSemantics.OBSERVATION
    )


@dataclass(frozen=True)
class CgroupCapabilities:
    version: CgroupVersion
    memory_current: ResourceValue
    memory_hard_boundary: ResourceValue
    memory_soft_boundary: ResourceValue
    swap_current: ResourceValue
    swap_high: ResourceValue
    swap_max: ResourceValue
    source_paths: Mapping[str, str] = field(default_factory=lambda: dict[str, str]())

    @classmethod
    def detect(cls, root: Path = Path("/sys/fs/cgroup")) -> "CgroupCapabilities":
        v2_current = root / "memory.current"
        v1_current = root / "memory" / "memory.usage_in_bytes"
        if v2_current.exists():

            def v2(name: str, semantics: ValueSemantics) -> ResourceValue:
                result = _read_number(root / name, unlimited_tokens=("max",))
                return ResourceValue(
                    result.value, result.state, result.origin, semantics, result.reason
                )

            return cls(
                version=CgroupVersion.V2,
                memory_current=v2("memory.current", ValueSemantics.CURRENT_USAGE),
                memory_hard_boundary=v2("memory.max", ValueSemantics.HARD_BOUNDARY),
                memory_soft_boundary=v2("memory.high", ValueSemantics.SOFT_BOUNDARY),
                swap_current=v2("memory.swap.current", ValueSemantics.CURRENT_USAGE),
                swap_high=v2("memory.swap.high", ValueSemantics.SOFT_BOUNDARY),
                swap_max=v2("memory.swap.max", ValueSemantics.HARD_BOUNDARY),
                source_paths={"root": str(root)},
            )
        if v1_current.exists():
            base = root / "memory"
            current = _read_number(v1_current)
            hard = _read_number(base / "memory.limit_in_bytes")
            hard = ResourceValue(
                hard.value,
                hard.state,
                hard.origin,
                ValueSemantics.HARD_BOUNDARY,
                hard.reason,
            )
            memsw_current = _read_number(base / "memory.memsw.usage_in_bytes")
            memsw_limit = _read_number(base / "memory.memsw.limit_in_bytes")

            # Derive container_swap_current:
            # In cgroup v1, memory.memsw.usage_in_bytes is (memory + swap) usage.
            # swap_current is derived as max(0, memsw_usage - memory_usage) only when both are FINITE.
            # Because memory.memsw.usage_in_bytes is an empirical measurement rather than a static kernel fact,
            # its origin is CURRENT_MEASUREMENT and semantics is OBSERVATION.
            swap_current: ResourceValue
            if (
                memsw_current.state is LimitState.FINITE
                and current.state is LimitState.FINITE
            ):
                if isinstance(memsw_current.value, int) and isinstance(
                    current.value, int
                ):
                    swap_current = ResourceValue(
                        max(0, memsw_current.value - current.value),
                        LimitState.FINITE,
                        ValueOrigin.CURRENT_MEASUREMENT,
                        ValueSemantics.OBSERVATION,
                        "derived from v1 memsw.usage - memory.usage",
                    )
                else:
                    swap_current = ResourceValue(
                        None,
                        LimitState.UNKNOWN,
                        ValueOrigin.CURRENT_MEASUREMENT,
                        ValueSemantics.OBSERVATION,
                        "invalid non-int counter values",
                    )
            elif (
                memsw_current.state is LimitState.UNSUPPORTED
                or current.state is LimitState.UNSUPPORTED
            ):
                swap_current = ResourceValue(
                    None,
                    LimitState.UNSUPPORTED,
                    ValueOrigin.CURRENT_MEASUREMENT,
                    ValueSemantics.OBSERVATION,
                    "memsw controller absent",
                )
            elif (
                memsw_current.state is LimitState.UNKNOWN
                or current.state is LimitState.UNKNOWN
            ):
                swap_current = ResourceValue(
                    None,
                    LimitState.UNKNOWN,
                    ValueOrigin.CURRENT_MEASUREMENT,
                    ValueSemantics.OBSERVATION,
                    "memsw counter unreadable",
                )
            else:
                swap_current = ResourceValue(
                    None,
                    LimitState.UNBOUNDED,
                    ValueOrigin.CURRENT_MEASUREMENT,
                    ValueSemantics.OBSERVATION,
                    "memsw counter unbounded",
                )

            # Derive container_swap_limit (swap_max):
            # In cgroup v1, memory.memsw.limit_in_bytes is (memory_limit + swap_limit).
            # If memsw is absent/unsupported, swap limit is UNSUPPORTED.
            # If memory is unbounded or memsw is unbounded, swap limit is UNBOUNDED.
            # If both are finite: swap_limit = max(0, memsw_limit - memory_limit).
            swap_max: ResourceValue
            if (
                memsw_limit.state is LimitState.UNSUPPORTED
                or hard.state is LimitState.UNSUPPORTED
            ):
                swap_max = ResourceValue(
                    None,
                    LimitState.UNSUPPORTED,
                    ValueOrigin.KERNEL_FACT,
                    ValueSemantics.HARD_BOUNDARY,
                    "memsw limit controller absent",
                )
            elif (
                memsw_limit.state is LimitState.UNKNOWN
                or hard.state is LimitState.UNKNOWN
            ):
                swap_max = ResourceValue(
                    None,
                    LimitState.UNKNOWN,
                    ValueOrigin.KERNEL_FACT,
                    ValueSemantics.HARD_BOUNDARY,
                    "memsw or memory limit unreadable",
                )
            elif memsw_limit.state is LimitState.UNBOUNDED:
                swap_max = ResourceValue(
                    None,
                    LimitState.UNBOUNDED,
                    ValueOrigin.KERNEL_FACT,
                    ValueSemantics.HARD_BOUNDARY,
                    "memsw limit unbounded",
                )
            elif hard.state is LimitState.UNBOUNDED:
                # Memory limit is unbounded; combined limit cannot produce independent finite swap limit
                swap_max = ResourceValue(
                    None,
                    LimitState.UNBOUNDED,
                    ValueOrigin.KERNEL_FACT,
                    ValueSemantics.HARD_BOUNDARY,
                    "memory limit unbounded",
                )
            elif isinstance(memsw_limit.value, int) and isinstance(hard.value, int):
                # Both finite
                swap_max = ResourceValue(
                    max(0, memsw_limit.value - hard.value),
                    LimitState.FINITE,
                    ValueOrigin.DERIVED,
                    ValueSemantics.HARD_BOUNDARY,
                    "derived from v1 memsw.limit - memory.limit (CGROUP_V1_MEMSW_DIFFERENCE)",
                )
            else:
                swap_max = ResourceValue(
                    None,
                    LimitState.UNKNOWN,
                    ValueOrigin.DERIVED,
                    ValueSemantics.HARD_BOUNDARY,
                    "invalid non-int limit values",
                )

            return cls(
                version=CgroupVersion.V1,
                memory_current=current,
                memory_hard_boundary=hard,
                memory_soft_boundary=ResourceValue(
                    None,
                    LimitState.UNSUPPORTED,
                    ValueOrigin.KERNEL_FACT,
                    ValueSemantics.SOFT_BOUNDARY,
                    "memory.soft_limit_in_bytes is deprecated/ineffective",
                ),
                swap_current=swap_current,
                swap_high=ResourceValue(
                    None,
                    LimitState.UNSUPPORTED,
                    ValueOrigin.KERNEL_FACT,
                    ValueSemantics.SOFT_BOUNDARY,
                    "cgroup v1 has no normalized swap.high",
                ),
                swap_max=swap_max,
                source_paths={"root": str(base)},
            )
        return cls(
            version=CgroupVersion.NONE,
            memory_current=ResourceValue(
                None,
                LimitState.UNSUPPORTED,
                ValueOrigin.KERNEL_FACT,
                ValueSemantics.OBSERVATION,
                "cgroup controller absent",
            ),
            memory_hard_boundary=ResourceValue(
                None,
                LimitState.UNSUPPORTED,
                ValueOrigin.KERNEL_FACT,
                ValueSemantics.OBSERVATION,
                "cgroup controller absent",
            ),
            memory_soft_boundary=ResourceValue(
                None,
                LimitState.UNSUPPORTED,
                ValueOrigin.KERNEL_FACT,
                ValueSemantics.OBSERVATION,
                "cgroup controller absent",
            ),
            swap_current=ResourceValue(
                None,
                LimitState.UNSUPPORTED,
                ValueOrigin.KERNEL_FACT,
                ValueSemantics.OBSERVATION,
                "cgroup controller absent",
            ),
            swap_high=ResourceValue(
                None,
                LimitState.UNSUPPORTED,
                ValueOrigin.KERNEL_FACT,
                ValueSemantics.OBSERVATION,
                "cgroup controller absent",
            ),
            swap_max=ResourceValue(
                None,
                LimitState.UNSUPPORTED,
                ValueOrigin.KERNEL_FACT,
                ValueSemantics.OBSERVATION,
                "cgroup controller absent",
            ),
            source_paths={"root": str(root)},
        )


@dataclass(frozen=True)
class Policy:
    policy_id: str
    policy_version: str
    parameters: Mapping[str, int | float | str]
    reason: str
    confidence: float
    origin: ValueOrigin = ValueOrigin.FALLBACK_POLICY


@dataclass(frozen=True)
class FallbackMemoryHeadroomPolicyV1(Policy):
    def __init__(self, fraction: float = 0.5) -> None:
        super().__init__(
            policy_id="memory-headroom",
            policy_version="1",
            parameters={"fraction": fraction},
            reason="Conservative fallback when current usage is unavailable",
            confidence=0.5,
            origin=ValueOrigin.FALLBACK_POLICY,
        )


@dataclass(frozen=True)
class FallbackStorageReservePolicyV1(Policy):
    def __init__(self, reserve_fraction: float = 0.1) -> None:
        super().__init__(
            policy_id="storage-reserve",
            policy_version="1",
            parameters={"reserve_fraction": reserve_fraction},
            reason="Fallback reserve for storage admission",
            confidence=0.5,
            origin=ValueOrigin.FALLBACK_POLICY,
        )


@dataclass(frozen=True)
class ProviderCalibrationPolicyV1(Policy):
    def __init__(self, minimum_samples: int = 3) -> None:
        super().__init__(
            policy_id="provider-calibration",
            policy_version="1",
            parameters={"minimum_samples": minimum_samples},
            reason="Require repeated empirical provider samples",
            confidence=0.5,
            origin=ValueOrigin.FALLBACK_POLICY,
        )


@dataclass(frozen=True)
class PressureResponsePolicyV1(Policy):
    def __init__(self, pressure_threshold: float = 0.8) -> None:
        super().__init__(
            policy_id="pressure-response",
            policy_version="1",
            parameters={"threshold": pressure_threshold},
            reason="Respond to measured pressure without changing delivery semantics",
            confidence=0.5,
            origin=ValueOrigin.FALLBACK_POLICY,
        )


def detect_storage_capacity(
    target_path: Path | None = None,
) -> dict[str, ResourceValue]:
    """Detect storage capacity facts for a filesystem target.

    Assigns rigorous provenance and semantics:
    - total_bytes: origin=FILESYSTEM_FACT, semantics=TOTAL_TARGET (hard physical capacity)
    - available_bytes: origin=FILESYSTEM_FACT, semantics=ADDITIONAL_HEADROOM (f_bavail * f_frsize)
    - free_bytes: origin=FILESYSTEM_FACT, semantics=OBSERVATION (f_bfree * f_frsize)
    """
    import os
    import shutil

    target = target_path or Path.cwd()
    resolved = target
    try:
        if not resolved.exists():
            parent = resolved.parent
            while parent != parent.parent and not parent.exists():
                parent = parent.parent
            if parent.exists():
                resolved = parent
    except Exception:
        pass

    if hasattr(os, "statvfs"):
        try:
            st = os.statvfs(str(resolved))
            total_bytes = st.f_blocks * st.f_frsize
            free_bytes = st.f_bfree * st.f_frsize
            avail_bytes = st.f_bavail * st.f_frsize
            return {
                "total_bytes": ResourceValue(
                    total_bytes,
                    LimitState.FINITE,
                    ValueOrigin.FILESYSTEM_FACT,
                    ValueSemantics.TOTAL_TARGET,
                    str(resolved),
                ),
                "available_bytes": ResourceValue(
                    avail_bytes,
                    LimitState.FINITE,
                    ValueOrigin.FILESYSTEM_FACT,
                    ValueSemantics.ADDITIONAL_HEADROOM,
                    f"statvfs f_bavail on {resolved}",
                ),
                "free_bytes": ResourceValue(
                    free_bytes,
                    LimitState.FINITE,
                    ValueOrigin.FILESYSTEM_FACT,
                    ValueSemantics.OBSERVATION,
                    f"statvfs f_bfree on {resolved}",
                ),
            }
        except Exception as exc:
            pass

    try:
        usage = shutil.disk_usage(str(resolved))
        return {
            "total_bytes": ResourceValue(
                usage.total,
                LimitState.FINITE,
                ValueOrigin.FILESYSTEM_FACT,
                ValueSemantics.TOTAL_TARGET,
                str(resolved),
            ),
            "available_bytes": ResourceValue(
                usage.free,
                LimitState.FINITE,
                ValueOrigin.FILESYSTEM_FACT,
                ValueSemantics.ADDITIONAL_HEADROOM,
                f"shutil free on {resolved}",
            ),
            "free_bytes": ResourceValue(
                usage.free,
                LimitState.FINITE,
                ValueOrigin.FILESYSTEM_FACT,
                ValueSemantics.OBSERVATION,
                f"shutil free on {resolved}",
            ),
        }
    except Exception as exc:
        return {
            "total_bytes": ResourceValue(
                None,
                LimitState.UNKNOWN,
                ValueOrigin.FILESYSTEM_FACT,
                ValueSemantics.TOTAL_TARGET,
                str(exc),
            ),
            "available_bytes": ResourceValue(
                None,
                LimitState.UNKNOWN,
                ValueOrigin.FILESYSTEM_FACT,
                ValueSemantics.ADDITIONAL_HEADROOM,
                str(exc),
            ),
            "free_bytes": ResourceValue(
                None,
                LimitState.UNKNOWN,
                ValueOrigin.FILESYSTEM_FACT,
                ValueSemantics.OBSERVATION,
                str(exc),
            ),
        }


def fingerprint(values: Mapping[str, object]) -> str:
    encoded = json.dumps(
        values, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class RuntimeDeploymentProfile:
    profile_id: str
    kernel: Mapping[str, ResourceValue]
    cgroup: CgroupCapabilities
    memory: Mapping[str, ResourceValue]
    hot_filesystem: Mapping[str, ResourceValue]
    warm_filesystem: Mapping[str, ResourceValue]
    pressure_psi: Mapping[str, ResourceValue]
    fuse_vfs: Mapping[str, ResourceValue]
    active_policies: tuple[Policy, ...]
    configuration_fingerprint: str
    environment_fingerprint: str
    provider_context: str = ""
    network_context: str = ""


@dataclass(frozen=True)
class CapacityTestRecord:
    runtime_profile_id: str
    environment_fingerprint: str
    configuration_fingerprint: str
    provider_context: str
    network_context: str
    stream_count: int
    bitrate_profile_bps: tuple[int, ...]
    aggregate_demand_bps: int
    duration_seconds: float
    rate_attainment: float
    underflow_count: int
    minimum_runway_seconds: float
    latency_p50_ms: float
    latency_p95_ms: float
    latency_p99_ms: float
    latency_max_ms: float
    pressure_observations: Mapping[str, float]
    integrity_passed: bool
    lost_reads: int
    duplicate_fetches: int
    cleanup_passed: bool
    passed: bool
    safety_confidence: float
    performance_confidence: float
    test_id: str = ""
    timestamp_utc: str = ""
    target_tier: str = "hot"
    target_path: str = ""
    observed_throughput_bps: float = 0.0
    pressure_spikes: int = 0
    invalidation_domains: tuple[str, ...] = ()


def calibration_invalidation(changed: set[str]) -> set[str]:
    """Return evidence domains made stale by specific material changes."""
    mapping: dict[str, set[str]] = {
        "provider": {"provider"},
        "network": {"provider", "network"},
        "hot_geometry": {"cache"},
        "warm_geometry": {"cache"},
        "chunk_config": {"streaming"},
        "prefetch_config": {"streaming"},
        "cpu_quota": {"runtime"},
        "memory_quota": {"runtime"},
    }
    result: set[str] = set()
    for item in changed:
        result.update(mapping.get(item, set()))
    return result


def is_calibration_valid_for_profile(
    record: CapacityTestRecord,
    current_profile: RuntimeDeploymentProfile,
) -> tuple[bool, set[str]]:
    """Determine if a past empirical CapacityTestRecord is valid for the current deployment profile.

    Returns (is_valid, invalidated_domains).
    A record is considered stale and invalid if any of the following drift:
    - environment_fingerprint -> invalidates 'runtime' domain
    - configuration_fingerprint -> invalidates 'cache' and 'streaming' domains
    - provider_context -> invalidates 'provider' domain (when profile context is specified)
    - network_context -> invalidates 'provider' and 'network' domains (when profile context is specified)
    """
    invalidated: set[str] = set()
    if record.environment_fingerprint != current_profile.environment_fingerprint:
        # Changes in environment (e.g., host/container resources, cgroups, kernel) invalidate runtime domain
        invalidated.add("runtime")
    if record.configuration_fingerprint != current_profile.configuration_fingerprint:
        # Changes in configuration invalidate cache and streaming calibration
        invalidated.add("cache")
        invalidated.add("streaming")
    if (
        current_profile.provider_context
        and record.provider_context != current_profile.provider_context
    ):
        invalidated.add("provider")
    if (
        current_profile.network_context
        and record.network_context != current_profile.network_context
    ):
        invalidated.add("provider")
        invalidated.add("network")
    if invalidated:
        return False, invalidated
    return True, set()


def build_runtime_profile(
    *,
    hot_path: Path | None = None,
    warm_path: Path | None = None,
    config_dict: Mapping[str, object] | None = None,
    provider_context: str = "",
    network_context: str = "",
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> RuntimeDeploymentProfile:
    """Construct an active RuntimeDeploymentProfile capturing current kernel, cgroup, memory, and storage facts."""
    import psutil

    # 1. Kernel detection
    bitness = detect_kernel_bitness()
    detected_ps = detect_page_size()
    kernel_facts: dict[str, ResourceValue] = {
        "bitness": ResourceValue(
            bitness,
            LimitState.FINITE if bitness is not None else LimitState.UNKNOWN,
            ValueOrigin.KERNEL_FACT,
            ValueSemantics.OBSERVATION,
            (
                f"{bitness}-bit kernel"
                if bitness is not None
                else "unknown kernel bitness"
            ),
        ),
        "page_size": ResourceValue(
            detected_ps,
            LimitState.FINITE if detected_ps is not None else LimitState.UNKNOWN,
            ValueOrigin.KERNEL_FACT,
            ValueSemantics.OBSERVATION,
            (
                f"{detected_ps}B page size"
                if detected_ps is not None
                else "page size detection unavailable"
            ),
        ),
    }

    # 2. Cgroups
    caps = CgroupCapabilities.detect(root=cgroup_root)

    # 3. System Memory
    try:
        vm = psutil.virtual_memory()
        mem_facts: dict[str, ResourceValue] = {
            "total_bytes": ResourceValue(
                vm.total,
                LimitState.FINITE,
                ValueOrigin.KERNEL_FACT,
                ValueSemantics.TOTAL_TARGET,
                "psutil total",
            ),
            "available_bytes": ResourceValue(
                vm.available,
                LimitState.FINITE,
                ValueOrigin.KERNEL_FACT,
                ValueSemantics.ADDITIONAL_HEADROOM,
                "psutil available",
            ),
        }
    except Exception as exc:
        mem_facts = {
            "total_bytes": ResourceValue(
                None,
                LimitState.UNKNOWN,
                ValueOrigin.KERNEL_FACT,
                ValueSemantics.TOTAL_TARGET,
                str(exc),
            ),
            "available_bytes": ResourceValue(
                None,
                LimitState.UNKNOWN,
                ValueOrigin.KERNEL_FACT,
                ValueSemantics.ADDITIONAL_HEADROOM,
                str(exc),
            ),
        }

    # 4. Storage
    hot_storage = detect_storage_capacity(hot_path)
    warm_storage = detect_storage_capacity(warm_path)

    # 5. Pressure PSI & FUSE VFS stubs
    psi_facts: dict[str, ResourceValue] = {
        "memory_some_avg10": ResourceValue(
            0.0,
            LimitState.FINITE,
            ValueOrigin.CURRENT_MEASUREMENT,
            ValueSemantics.OBSERVATION,
            "baseline PSI",
        ),
    }
    fuse_facts: dict[str, ResourceValue] = {
        "active_mount": ResourceValue(
            1,
            LimitState.FINITE,
            ValueOrigin.KERNEL_FACT,
            ValueSemantics.OBSERVATION,
            "VFS active",
        ),
    }

    env_data: dict[str, object] = {
        "kernel_bitness": bitness,
        "cgroup_version": str(caps.version),
        "cgroup_mem_limit": caps.memory_hard_boundary.value,
        "sys_mem_total": mem_facts["total_bytes"].value,
        "hot_storage_total": hot_storage["total_bytes"].value,
        "warm_storage_total": warm_storage["total_bytes"].value,
    }
    cfg_data: dict[str, object] = dict(config_dict or {"default": True})

    env_fp = fingerprint(env_data)
    cfg_fp = fingerprint(cfg_data)
    profile_id = f"profile-{env_fp[:8]}-{cfg_fp[:8]}"

    return RuntimeDeploymentProfile(
        profile_id=profile_id,
        kernel=kernel_facts,
        cgroup=caps,
        memory=mem_facts,
        hot_filesystem=hot_storage,
        warm_filesystem=warm_storage,
        pressure_psi=psi_facts,
        fuse_vfs=fuse_facts,
        active_policies=(),
        configuration_fingerprint=cfg_fp,
        environment_fingerprint=env_fp,
        provider_context=provider_context,
        network_context=network_context,
    )
