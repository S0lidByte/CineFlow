"""Focused unit tests for Phase 18 D80 Runtime Profile, Cgroups, and Provenance models."""

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from program.services.streaming.runtime_profile import (
    CapacityTestRecord,
    CgroupCapabilities,
    CgroupVersion,
    FallbackMemoryHeadroomPolicyV1,
    FallbackStorageReservePolicyV1,
    LimitState,
    Policy,
    PressureResponsePolicyV1,
    ProviderCalibrationPolicyV1,
    ResourceValue,
    RuntimeDeploymentProfile,
    ValueOrigin,
    ValueSemantics,
    calibration_invalidation,
    compute_page_counter_max,
    detect_storage_capacity,
    fingerprint,
    get_supported_page_sizes,
    is_calibration_valid_for_profile,
    is_cgroup_v1_unbounded_limit,
)


def test_resource_value_invariants() -> None:
    # FINITE requires a non-None value
    with pytest.raises(ValueError, match="FINITE values require a value"):
        ResourceValue(
            None, LimitState.FINITE, ValueOrigin.KERNEL_FACT, ValueSemantics.OBSERVATION
        )

    # Non-FINITE must NOT carry sentinel values
    with pytest.raises(
        ValueError, match="Non-FINITE states must not carry sentinel values"
    ):
        ResourceValue(
            0,
            LimitState.UNBOUNDED,
            ValueOrigin.KERNEL_FACT,
            ValueSemantics.HARD_BOUNDARY,
        )

    with pytest.raises(
        ValueError, match="Non-FINITE states must not carry sentinel values"
    ):
        ResourceValue(
            -1, LimitState.UNKNOWN, ValueOrigin.KERNEL_FACT, ValueSemantics.OBSERVATION
        )

    # Valid values
    v1 = ResourceValue(
        1048576,
        LimitState.FINITE,
        ValueOrigin.KERNEL_FACT,
        ValueSemantics.HARD_BOUNDARY,
    )
    assert v1.value == 1048576
    assert v1.state is LimitState.FINITE

    v2 = ResourceValue(
        None,
        LimitState.UNBOUNDED,
        ValueOrigin.KERNEL_FACT,
        ValueSemantics.HARD_BOUNDARY,
    )
    assert v2.value is None
    assert v2.state is LimitState.UNBOUNDED


def test_cgroup_v2_finite_memory_max() -> None:
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "memory.current").write_text("104857600\n", encoding="ascii")
        (root / "memory.max").write_text("209715200\n", encoding="ascii")
        (root / "memory.high").write_text("157286400\n", encoding="ascii")
        (root / "memory.swap.current").write_text("0\n", encoding="ascii")
        (root / "memory.swap.high").write_text("max\n", encoding="ascii")
        (root / "memory.swap.max").write_text("max\n", encoding="ascii")

        caps = CgroupCapabilities.detect(root)
        assert caps.version is CgroupVersion.V2
        assert caps.memory_current.state is LimitState.FINITE
        assert caps.memory_current.value == 104857600
        assert caps.memory_current.semantics is ValueSemantics.CURRENT_USAGE

        assert caps.memory_hard_boundary.state is LimitState.FINITE
        assert caps.memory_hard_boundary.value == 209715200
        assert caps.memory_hard_boundary.semantics is ValueSemantics.HARD_BOUNDARY

        assert caps.memory_soft_boundary.state is LimitState.FINITE
        assert caps.memory_soft_boundary.value == 157286400
        assert caps.memory_soft_boundary.semantics is ValueSemantics.SOFT_BOUNDARY


def test_cgroup_v2_memory_max_and_high_max_token() -> None:
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "memory.current").write_text("52428800\n", encoding="ascii")
        (root / "memory.max").write_text("max\n", encoding="ascii")
        (root / "memory.high").write_text("max\n", encoding="ascii")
        (root / "memory.swap.current").write_text("1048576\n", encoding="ascii")
        (root / "memory.swap.high").write_text("max\n", encoding="ascii")
        (root / "memory.swap.max").write_text("max\n", encoding="ascii")

        caps = CgroupCapabilities.detect(root)
        assert caps.version is CgroupVersion.V2
        assert caps.memory_hard_boundary.state is LimitState.UNBOUNDED
        assert caps.memory_hard_boundary.value is None
        assert caps.memory_soft_boundary.state is LimitState.UNBOUNDED
        assert caps.memory_soft_boundary.value is None

        # Swap telemetry is separate and finite
        assert caps.swap_current.state is LimitState.FINITE
        assert caps.swap_current.value == 1048576


def test_cgroup_v1_finite_memory_limit() -> None:
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        v1_dir = root / "memory"
        v1_dir.mkdir(parents=True)
        (v1_dir / "memory.usage_in_bytes").write_text("52428800\n", encoding="ascii")
        (v1_dir / "memory.limit_in_bytes").write_text("104857600\n", encoding="ascii")

        caps = CgroupCapabilities.detect(root)
        assert caps.version is CgroupVersion.V1
        assert caps.memory_current.state is LimitState.FINITE
        assert caps.memory_current.value == 52428800
        assert caps.memory_hard_boundary.state is LimitState.FINITE
        assert caps.memory_hard_boundary.value == 104857600

        # Critical v1 correction: soft limit is deprecated/ineffective -> UNSUPPORTED
        assert caps.memory_soft_boundary.state is LimitState.UNSUPPORTED
        assert caps.memory_soft_boundary.value is None


def test_cgroup_v1_unlimited_page_counter_max() -> None:
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        v1_dir = root / "memory"
        v1_dir.mkdir(parents=True)
        (v1_dir / "memory.usage_in_bytes").write_text("52428800\n", encoding="ascii")
        # PAGE_COUNTER_MAX on 64-bit Linux: LONG_MAX rounded down to page size (4096)
        page_counter_max = ((1 << 63) - 1) & ~4095
        (v1_dir / "memory.limit_in_bytes").write_text(
            f"{page_counter_max}\n", encoding="ascii"
        )

        caps = CgroupCapabilities.detect(root)
        assert caps.version is CgroupVersion.V1
        assert caps.memory_hard_boundary.state is LimitState.UNBOUNDED
        assert caps.memory_hard_boundary.value is None
        assert caps.memory_hard_boundary.reason == "cgroup-v1 PAGE_COUNTER_MAX"


def test_cgroup_v1_memsw_available_and_unavailable() -> None:
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        v1_dir = root / "memory"
        v1_dir.mkdir(parents=True)
        (v1_dir / "memory.usage_in_bytes").write_text("1000000\n", encoding="ascii")
        (v1_dir / "memory.limit_in_bytes").write_text("5000000\n", encoding="ascii")

        # Without memsw counters
        caps = CgroupCapabilities.detect(root)
        assert caps.swap_current.state is LimitState.UNSUPPORTED

        # With memsw counters
        (v1_dir / "memory.memsw.usage_in_bytes").write_text(
            "1500000\n", encoding="ascii"
        )
        (v1_dir / "memory.memsw.limit_in_bytes").write_text(
            "10000000\n", encoding="ascii"
        )
        caps_with_memsw = CgroupCapabilities.detect(root)
        assert caps_with_memsw.swap_current.state is LimitState.FINITE
        # swap = memsw_usage - memory_usage = 1500000 - 1000000 = 500000
        assert caps_with_memsw.swap_current.value == 500000
        # Telemetry provenance: CURRENT_MEASUREMENT and OBSERVATION
        assert caps_with_memsw.swap_current.origin is ValueOrigin.CURRENT_MEASUREMENT
        assert caps_with_memsw.swap_current.semantics is ValueSemantics.OBSERVATION


def test_no_cgroup_detected() -> None:
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        caps = CgroupCapabilities.detect(root)
        assert caps.version is CgroupVersion.NONE
        assert caps.memory_current.state is LimitState.UNSUPPORTED
        assert caps.memory_hard_boundary.state is LimitState.UNSUPPORTED
        assert caps.memory_soft_boundary.state is LimitState.UNSUPPORTED


def test_unreadable_controller_and_unknown_vs_unbounded() -> None:
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        # Create unparseable controller file
        (root / "memory.current").write_text("invalid_garbage\n", encoding="ascii")
        (root / "memory.max").write_text("corrupted\n", encoding="ascii")

        caps = CgroupCapabilities.detect(root)
        assert caps.version is CgroupVersion.V2
        assert caps.memory_current.state is LimitState.UNKNOWN
        assert caps.memory_hard_boundary.state is LimitState.UNKNOWN
        assert caps.memory_hard_boundary.state is not LimitState.UNBOUNDED


def test_fallback_policies_provenance_and_semantics() -> None:
    mem_policy = FallbackMemoryHeadroomPolicyV1(fraction=0.6)
    assert mem_policy.policy_id == "memory-headroom"
    assert mem_policy.origin is ValueOrigin.FALLBACK_POLICY
    assert mem_policy.parameters["fraction"] == 0.6

    storage_policy = FallbackStorageReservePolicyV1(reserve_fraction=0.15)
    assert storage_policy.policy_id == "storage-reserve"
    assert storage_policy.origin is ValueOrigin.FALLBACK_POLICY

    calib_policy = ProviderCalibrationPolicyV1(minimum_samples=5)
    assert calib_policy.policy_id == "provider-calibration"

    pressure_policy = PressureResponsePolicyV1(pressure_threshold=0.85)
    assert pressure_policy.policy_id == "pressure-response"


def test_calibration_invalidation_granularity() -> None:
    # Provider change only invalidates provider domain
    stale_provider = calibration_invalidation({"provider"})
    assert stale_provider == {"provider"}

    # Network change invalidates provider and network
    stale_net = calibration_invalidation({"network"})
    assert stale_net == {"provider", "network"}

    # Cache geometry change invalidates cache
    stale_geo = calibration_invalidation({"hot_geometry", "warm_geometry"})
    assert stale_geo == {"cache"}

    # UI or unrelated changes invalidate nothing
    assert calibration_invalidation({"ui_theme"}) == set()


def test_runtime_deployment_profile_and_fingerprinting() -> None:
    env_fp = fingerprint({"kernel": "6.6.0", "arch": "x86_64"})
    cfg_fp = fingerprint({"chunk_size_mb": 1, "block_size": 131072})

    profile = RuntimeDeploymentProfile(
        profile_id="prof_001",
        kernel={
            "release": ResourceValue(
                "6.6.0",
                LimitState.FINITE,
                ValueOrigin.KERNEL_FACT,
                ValueSemantics.OBSERVATION,
            )
        },
        cgroup=CgroupCapabilities.detect(Path("/nonexistent")),
        memory={
            "total_ram": ResourceValue(
                16384,
                LimitState.FINITE,
                ValueOrigin.KERNEL_FACT,
                ValueSemantics.OBSERVATION,
            )
        },
        hot_filesystem={
            "free_mb": ResourceValue(
                4096,
                LimitState.FINITE,
                ValueOrigin.FILESYSTEM_FACT,
                ValueSemantics.CURRENT_USAGE,
            )
        },
        warm_filesystem={
            "free_mb": ResourceValue(
                51200,
                LimitState.FINITE,
                ValueOrigin.FILESYSTEM_FACT,
                ValueSemantics.CURRENT_USAGE,
            )
        },
        pressure_psi={},
        fuse_vfs={},
        active_policies=(
            FallbackMemoryHeadroomPolicyV1(),
            FallbackStorageReservePolicyV1(),
        ),
        configuration_fingerprint=cfg_fp,
        environment_fingerprint=env_fp,
    )
    assert profile.profile_id == "prof_001"
    assert profile.configuration_fingerprint == cfg_fp


def test_capacity_test_record_integrity() -> None:
    record = CapacityTestRecord(
        runtime_profile_id="prof_001",
        environment_fingerprint="env_hash",
        configuration_fingerprint="cfg_hash",
        provider_context="realdebrid",
        network_context="lan",
        stream_count=2,
        bitrate_profile_bps=(60000000, 80000000),
        aggregate_demand_bps=140000000,
        duration_seconds=300.0,
        rate_attainment=1.0,
        underflow_count=0,
        minimum_runway_seconds=12.5,
        latency_p50_ms=4.2,
        latency_p95_ms=18.5,
        latency_p99_ms=45.1,
        latency_max_ms=88.0,
        pressure_observations={"cpu": 0.12, "io": 0.05},
        integrity_passed=True,
        lost_reads=0,
        duplicate_fetches=0,
        cleanup_passed=True,
        passed=True,
        safety_confidence=0.95,
        performance_confidence=0.92,
        test_id="test_001",
        target_tier="hot",
        target_path="/dev/shm",
        observed_throughput_bps=150000000.0,
        pressure_spikes=0,
    )
    assert record.passed is True
    assert record.integrity_passed is True
    assert record.lost_reads == 0
    assert record.test_id == "test_001"
    assert record.target_tier == "hot"


def test_is_cgroup_v1_unbounded_limit_portable_page_sizes_and_architectures() -> None:
    from program.services.streaming.runtime_profile import (
        compute_cgroup_v1_unlimited_limit_bytes,
    )

    # 64-bit architectures
    assert is_cgroup_v1_unbounded_limit((1 << 63) - 1) is True  # LONG_MAX 64-bit
    for ps in get_supported_page_sizes():
        val = compute_page_counter_max(64, ps)
        assert is_cgroup_v1_unbounded_limit(val) is True
        assert val == compute_cgroup_v1_unlimited_limit_bytes(64, ps)

    # 32-bit architectures:
    # Kernel PAGE_COUNTER_MAX * PAGE_SIZE = ((1 << 31) - 1) * page_size (~8 TiB for 4K)
    for ps in get_supported_page_sizes():
        val = compute_page_counter_max(32, ps)
        assert is_cgroup_v1_unbounded_limit(val) is True
        assert val == compute_cgroup_v1_unlimited_limit_bytes(32, ps)
        assert val == ((1 << 31) - 1) * ps

    # Raw 32-bit LONG_MAX (2147483647 = ~2 GiB) is a normal finite container memory limit
    assert is_cgroup_v1_unbounded_limit((1 << 31) - 1) is False

    # Finite numbers must NOT match
    assert is_cgroup_v1_unbounded_limit(104857600) is False
    assert is_cgroup_v1_unbounded_limit(1073741824) is False
    assert is_cgroup_v1_unbounded_limit(0) is False


def test_detect_storage_capacity_provenance_and_semantics() -> None:
    with TemporaryDirectory() as tmpdir:
        res = detect_storage_capacity(Path(tmpdir))
        assert "total_bytes" in res
        assert "available_bytes" in res
        assert "free_bytes" in res

        tot = res["total_bytes"]
        avail = res["available_bytes"]
        free = res["free_bytes"]

        assert tot.origin is ValueOrigin.FILESYSTEM_FACT
        assert tot.semantics is ValueSemantics.TOTAL_TARGET
        assert tot.state is LimitState.FINITE

        assert avail.origin is ValueOrigin.FILESYSTEM_FACT
        assert avail.semantics is ValueSemantics.ADDITIONAL_HEADROOM
        assert avail.state is LimitState.FINITE

        assert free.origin is ValueOrigin.FILESYSTEM_FACT
        assert free.semantics is ValueSemantics.OBSERVATION
        assert free.state is LimitState.FINITE


def test_calibration_validation_and_invalidation() -> None:
    env_fp = fingerprint({"kernel": "6.6.0", "arch": "x86_64"})
    cfg_fp = fingerprint({"chunk_size_mb": 1, "block_size": 131072})

    profile = RuntimeDeploymentProfile(
        profile_id="prof_001",
        kernel={},
        cgroup=CgroupCapabilities.detect(Path("/nonexistent")),
        memory={},
        hot_filesystem={},
        warm_filesystem={},
        pressure_psi={},
        fuse_vfs={},
        active_policies=(),
        configuration_fingerprint=cfg_fp,
        environment_fingerprint=env_fp,
    )

    valid_record = CapacityTestRecord(
        runtime_profile_id="prof_001",
        environment_fingerprint=env_fp,
        configuration_fingerprint=cfg_fp,
        provider_context="rd",
        network_context="lan",
        stream_count=1,
        bitrate_profile_bps=(1000,),
        aggregate_demand_bps=1000,
        duration_seconds=10.0,
        rate_attainment=1.0,
        underflow_count=0,
        minimum_runway_seconds=5.0,
        latency_p50_ms=1.0,
        latency_p95_ms=2.0,
        latency_p99_ms=3.0,
        latency_max_ms=4.0,
        pressure_observations={},
        integrity_passed=True,
        lost_reads=0,
        duplicate_fetches=0,
        cleanup_passed=True,
        passed=True,
        safety_confidence=1.0,
        performance_confidence=1.0,
    )
    is_valid, inv = is_calibration_valid_for_profile(valid_record, profile)
    assert is_valid is True
    assert inv == set()

    # Mismatched environment
    stale_env_record = CapacityTestRecord(
        runtime_profile_id="prof_001",
        environment_fingerprint="other_env",
        configuration_fingerprint=cfg_fp,
        provider_context="rd",
        network_context="lan",
        stream_count=1,
        bitrate_profile_bps=(1000,),
        aggregate_demand_bps=1000,
        duration_seconds=10.0,
        rate_attainment=1.0,
        underflow_count=0,
        minimum_runway_seconds=5.0,
        latency_p50_ms=1.0,
        latency_p95_ms=2.0,
        latency_p99_ms=3.0,
        latency_max_ms=4.0,
        pressure_observations={},
        integrity_passed=True,
        lost_reads=0,
        duplicate_fetches=0,
        cleanup_passed=True,
        passed=True,
        safety_confidence=1.0,
        performance_confidence=1.0,
    )
    is_valid, inv = is_calibration_valid_for_profile(stale_env_record, profile)
    assert is_valid is False
    assert "runtime" in inv


def test_cgroup_v1_memsw_unbounded_and_swap_limit_derivation() -> None:
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        v1_dir = root / "memory"
        v1_dir.mkdir(parents=True)
        (v1_dir / "memory.usage_in_bytes").write_text("1000000\n", encoding="ascii")
        (v1_dir / "memory.limit_in_bytes").write_text("5000000\n", encoding="ascii")

        # When memsw.limit_in_bytes is PAGE_COUNTER_MAX (unbounded)
        page_counter_max = ((1 << 63) - 1) & ~4095
        (v1_dir / "memory.memsw.usage_in_bytes").write_text(
            "1500000\n", encoding="ascii"
        )
        (v1_dir / "memory.memsw.limit_in_bytes").write_text(
            f"{page_counter_max}\n", encoding="ascii"
        )

        caps = CgroupCapabilities.detect(root)
        assert caps.swap_max.state is LimitState.UNBOUNDED
        assert caps.swap_max.value is None

        # When memory.limit_in_bytes is also unbounded
        (v1_dir / "memory.limit_in_bytes").write_text(
            f"{page_counter_max}\n", encoding="ascii"
        )
        (v1_dir / "memory.memsw.limit_in_bytes").write_text(
            "10000000\n", encoding="ascii"
        )
        caps2 = CgroupCapabilities.detect(root)
        assert caps2.swap_max.state is LimitState.UNBOUNDED

        # When both memsw and memory limit are finite
        (v1_dir / "memory.limit_in_bytes").write_text("5000000\n", encoding="ascii")
        (v1_dir / "memory.memsw.limit_in_bytes").write_text(
            "8000000\n", encoding="ascii"
        )
        caps3 = CgroupCapabilities.detect(root)
        assert caps3.swap_max.state is LimitState.FINITE
        assert caps3.swap_max.value == 3000000
        assert caps3.swap_max.origin is ValueOrigin.DERIVED
        assert "CGROUP_V1_MEMSW_DIFFERENCE" in (caps3.swap_max.reason or "")
        assert caps3.swap_current.origin is ValueOrigin.CURRENT_MEASUREMENT
        assert caps3.swap_current.semantics is ValueSemantics.OBSERVATION


def test_kernel_architecture_and_page_counter_max_matrix() -> None:
    from program.services.streaming.runtime_profile import (
        compute_cgroup_v1_unlimited_limit_bytes,
        compute_page_counter_max,
        detect_kernel_bitness,
        detect_page_size,
        is_cgroup_v1_unbounded_limit,
    )

    # 1. Bitness and page size detection normalized without unsafe fallbacks
    bitness = detect_kernel_bitness()
    assert bitness in (32, 64, None)
    page_size = detect_page_size()
    # On environments where sysconf is present (e.g. Linux), page_size is in (4096, 8192, 16384, 65536)
    # On systems lacking sysconf(SC_PAGE_SIZE) (e.g. standard Windows), detect_page_size returns None
    assert page_size in (4096, 8192, 16384, 65536, None)

    # 2. Kernel PAGE_COUNTER_MAX evaluation across architectures and page sizes
    # 64-bit kernel: PAGE_COUNTER_MAX = LONG_MAX / PAGE_SIZE, in bytes = (((1 << 63) - 1) // PAGE_SIZE) * PAGE_SIZE
    for ps in (4096, 8192, 16384, 65536):
        val_64 = compute_cgroup_v1_unlimited_limit_bytes(64, ps)
        assert is_cgroup_v1_unbounded_limit(val_64) is True
        assert compute_page_counter_max(64, ps) == val_64
        # Plain LONG_MAX 64
        assert is_cgroup_v1_unbounded_limit((1 << 63) - 1) is True

    # 32-bit kernel: PAGE_COUNTER_MAX = LONG_MAX, in bytes = LONG_MAX * PAGE_SIZE = ((1 << 31) - 1) * PAGE_SIZE
    for ps in (4096, 8192, 16384, 65536):
        val_32 = compute_cgroup_v1_unlimited_limit_bytes(32, ps)
        assert val_32 == ((1 << 31) - 1) * ps
        assert is_cgroup_v1_unbounded_limit(val_32) is True
        assert compute_page_counter_max(32, ps) == val_32

    # Raw 32-bit LONG_MAX is a valid finite limit and must NOT be classified as unbounded
    assert is_cgroup_v1_unbounded_limit((1 << 31) - 1) is False

    # Finite values should never match unbounded
    assert is_cgroup_v1_unbounded_limit(1024 * 1024 * 1024) is False
    assert is_cgroup_v1_unbounded_limit(4 * 1024 * 1024 * 1024) is False


def test_detect_page_size_and_bitness_failure_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    from program.services.streaming import runtime_profile

    # When sysconf is absent
    if hasattr(os, "sysconf"):
        monkeypatch.delattr(os, "sysconf")
    assert runtime_profile.detect_page_size() is None

    # When sysconf raises ValueError, OSError, or returns -1
    monkeypatch.setattr(os, "sysconf", lambda name: -1, raising=False)
    assert runtime_profile.detect_page_size() is None

    def raise_err(name: str) -> int:
        raise OSError("Permission denied")

    monkeypatch.setattr(os, "sysconf", raise_err, raising=False)
    assert runtime_profile.detect_page_size() is None

    # When sysconf returns non-4K valid page sizes (e.g. 16 KiB or 64 KiB)
    monkeypatch.setattr(os, "sysconf", lambda name: 16384, raising=False)
    assert runtime_profile.detect_page_size() == 16384
    monkeypatch.setattr(os, "sysconf", lambda name: 65536, raising=False)
    assert runtime_profile.detect_page_size() == 65536

    # Test bitness failure mode: unrecognized machine returns None, not 64
    class DummyUname:
        machine = "unknown_weird_arch"

    monkeypatch.setattr(runtime_profile.platform, "uname", lambda: DummyUname())
    assert runtime_profile.detect_kernel_bitness() is None

    # Known 32-bit arch
    class ArmUname:
        machine = "armv7l"

    monkeypatch.setattr(runtime_profile.platform, "uname", lambda: ArmUname())
    assert runtime_profile.detect_kernel_bitness() == 32

    # Known 64-bit arch
    class X86Uname:
        machine = "x86_64"

    monkeypatch.setattr(runtime_profile.platform, "uname", lambda: X86Uname())
    assert runtime_profile.detect_kernel_bitness() == 64


def test_build_runtime_profile_end_to_end_and_invalidation() -> None:
    from program.services.streaming.runtime_profile import (
        build_runtime_profile,
        is_calibration_valid_for_profile,
    )

    profile = build_runtime_profile(config_dict={"chunk_size_mb": 4})
    assert profile.profile_id.startswith("profile-")
    assert "bitness" in profile.kernel
    assert profile.cgroup is not None
    assert "total_bytes" in profile.memory

    # Matching record
    rec = CapacityTestRecord(
        runtime_profile_id=profile.profile_id,
        environment_fingerprint=profile.environment_fingerprint,
        configuration_fingerprint=profile.configuration_fingerprint,
        provider_context="rd",
        network_context="wan",
        stream_count=1,
        bitrate_profile_bps=(10000000,),
        aggregate_demand_bps=10000000,
        duration_seconds=30.0,
        rate_attainment=1.0,
        underflow_count=0,
        minimum_runway_seconds=10.0,
        latency_p50_ms=2.0,
        latency_p95_ms=5.0,
        latency_p99_ms=10.0,
        latency_max_ms=15.0,
        pressure_observations={},
        integrity_passed=True,
        lost_reads=0,
        duplicate_fetches=0,
        cleanup_passed=True,
        passed=True,
        safety_confidence=0.95,
        performance_confidence=0.90,
    )
    is_valid, inv = is_calibration_valid_for_profile(rec, profile)
    assert is_valid is True
    assert inv == set()

    # Invalidate by building a profile with different configuration
    profile_diff_cfg = build_runtime_profile(config_dict={"chunk_size_mb": 8})
    is_valid_cfg, inv_cfg = is_calibration_valid_for_profile(rec, profile_diff_cfg)
    assert is_valid_cfg is False
    assert "cache" in inv_cfg
    assert "streaming" in inv_cfg
