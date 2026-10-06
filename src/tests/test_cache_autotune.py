"""Unit tests for CineFlow Cache Auto-Tune and Watermark Optimization Engine."""

import tempfile
import threading
import time
from pathlib import Path

import pytest

from program.services.streaming.cache_autotune import (
    AutoTuneJobManager,
    calculate_minimum_operational_hot_mb,
    compute_recommendation,
    detect_path_disk_space,
    detect_system_topology,
    run_synthetic_benchmark,
)
from program.settings.models import FilesystemModel


def test_detect_system_topology():
    topology = detect_system_topology()
    assert topology["system_memory_mb"] > 0
    assert topology["effective_memory_ceiling_mb"] > 0
    assert topology["disk_free_mb"] >= 0


def test_run_synthetic_benchmark_and_cleanup():
    with tempfile.TemporaryDirectory() as tmpdir:
        sandbox = Path(tmpdir)
        speed = run_synthetic_benchmark(
            sandbox_dir=sandbox,
            total_bytes=2 * 1024 * 1024,
            chunk_size=512 * 1024,
        )
        assert speed > 0.0
        # Check no benchmark files were left behind
        assert len(list(sandbox.glob("*.bin"))) == 0


def test_run_synthetic_benchmark_cancellation():
    cancel_event = threading.Event()
    cancel_event.set()
    with tempfile.TemporaryDirectory() as tmpdir:
        with pytest.raises(InterruptedError):
            run_synthetic_benchmark(
                sandbox_dir=Path(tmpdir),
                total_bytes=2 * 1024 * 1024,
                chunk_size=512 * 1024,
                cancel_event=cancel_event,
            )


@pytest.mark.parametrize("profile", ["conservative", "balanced", "aggressive"])
def test_compute_recommendation_satisfies_filesystem_invariants(profile):
    topology = {
        "system_memory_mb": 16384,
        "available_memory_mb": 12000,
        "cgroup_limit_mb": 8192,
        "effective_memory_ceiling_mb": 8192,
        "disk_free_mb": 100000,
    }
    rec = compute_recommendation(
        profile=profile,
        topology=topology,
        hot_speed_mb_s=1500.0,
        warm_speed_mb_s=350.0,
    )

    # Invariants
    assert (
        rec.recommended_hot_watermark_low_pct < rec.recommended_hot_watermark_high_pct
    )
    assert (
        rec.recommended_hot_watermark_high_pct - rec.recommended_hot_watermark_low_pct
    ) >= 5.0
    assert rec.recommended_hot_watermark_high_pct <= (
        100.0 - rec.recommended_hot_reserve_pct
    )

    assert (
        rec.recommended_warm_watermark_low_pct < rec.recommended_warm_watermark_high_pct
    )
    assert (
        rec.recommended_warm_watermark_high_pct - rec.recommended_warm_watermark_low_pct
    ) >= 5.0
    assert rec.recommended_warm_watermark_high_pct <= (
        100.0 - rec.recommended_warm_reserve_pct
    )

    # Validate directly against FilesystemModel
    fs = FilesystemModel(
        autotune_mode=profile,
        hot_cache_reserve_pct=rec.recommended_hot_reserve_pct,
        hot_cache_watermark_high_pct=rec.recommended_hot_watermark_high_pct,
        hot_cache_watermark_low_pct=rec.recommended_hot_watermark_low_pct,
        warm_cache_reserve_pct=rec.recommended_warm_reserve_pct,
        warm_cache_watermark_high_pct=rec.recommended_warm_watermark_high_pct,
        warm_cache_watermark_low_pct=rec.recommended_warm_watermark_low_pct,
        warm_cache_min_free_mb=rec.recommended_warm_min_free_mb,
    )
    assert fs.autotune_mode == profile


def test_autotune_job_manager_lifecycle():
    manager = AutoTuneJobManager()
    job = manager.start_job(profile="balanced", bench_bytes=1024 * 1024)

    assert job.status in ("pending", "running")
    # Verify concurrency lock prevents starting another job simultaneously
    with pytest.raises(RuntimeError):
        manager.start_job(profile="conservative")

    # Wait for completion
    timeout = 10.0
    start = time.time()
    while job.status in ("pending", "running") and (time.time() - start) < timeout:
        time.sleep(0.05)

    assert job.status == "completed"
    assert job.recommendation is not None
    assert job.progress_pct == 100.0

    snap = manager.get_job_snapshot(job.run_id)
    assert snap is not None
    assert snap.status == "completed"
    assert snap.recommendation is not None


def test_autotune_job_manager_cancellation():
    manager = AutoTuneJobManager()
    # Start a larger benchmark so we have time to cancel it
    job = manager.start_job(profile="aggressive", bench_bytes=16 * 1024 * 1024)
    # Immediately cancel
    cancelled = manager.cancel_job(job.run_id)
    assert cancelled is True

    # Wait for worker thread to exit
    if job.thread:
        job.thread.join(timeout=3.0)

    assert job.status == "cancelled"
    snap = manager.get_job_snapshot(job.run_id)
    assert snap is not None
    assert snap.status == "cancelled"


def test_autotune_topology_cgroup_and_tmpfs_independent_constraints():
    """Verify AutoTune independently evaluates cgroup headroom and tmpfs space without double-counting."""
    # Topology with low cgroup headroom (e.g. 512MB container, 200MB available headroom)
    synth_topology = {
        "system_memory_mb": 16384,
        "available_memory_mb": 12000,
        "cgroup_limit_mb": 512,
        "cgroup_current_mb": 312,
        "cgroup_headroom_mb": 200,
        "effective_memory_ceiling_mb": 512,
        "tmpfs_total_mb": 256,
        "tmpfs_free_mb": 180,
        "disk_free_mb": 50000,
    }

    # Balanced profile: 20% of 512MB = ~102MB.
    # Cgroup headroom cap: 60% of 200MB = 120MB.
    # Tmpfs free cap: 75% of 180MB = 135MB.
    # Hot cache should remain safe under 120MB and 135MB.
    rec = compute_recommendation("balanced", synth_topology, 300.0, 150.0)
    assert rec.recommended_hot_cache_max_mb <= 120
    assert rec.recommended_hot_cache_max_mb <= 135
    assert rec.recommended_hot_cache_max_mb >= 64

    # Now simulate constrained tmpfs (e.g. tmpfs free is only 80MB)
    synth_topology_low_tmpfs = dict(synth_topology, tmpfs_free_mb=80)
    rec_low_tmpfs = compute_recommendation(
        "aggressive", synth_topology_low_tmpfs, 300.0, 150.0
    )
    # Aggressive base wants up to 35% of 512MB = ~179MB.
    # Tmpfs cap is 75% of 80MB = 60MB. Under Option A, 60MB strictly overrides preference.
    assert rec_low_tmpfs.recommended_hot_cache_max_mb == 60
    assert any("tmpfs available capacity" in r for r in rec_low_tmpfs.reasons)
    assert rec_low_tmpfs.operational_status.value == "CONSTRAINED"
    assert any("CONSTRAINED" in r for r in rec_low_tmpfs.reasons)

    # Invariant checks for FilesystemModel validation
    assert (
        rec_low_tmpfs.recommended_hot_watermark_low_pct
        < rec_low_tmpfs.recommended_hot_watermark_high_pct
    )
    assert (
        rec_low_tmpfs.recommended_hot_watermark_high_pct
        - rec_low_tmpfs.recommended_hot_watermark_low_pct
        >= 5.0
    )
    assert (
        rec_low_tmpfs.recommended_hot_watermark_high_pct
        <= 100.0 - rec_low_tmpfs.recommended_hot_reserve_pct
    )


def test_autotune_constrained_topologies_cases_a_through_e():
    """Deterministic verification for Section 5 Cases A, B, C, D, and E."""
    # Case A: 512 MiB total container, 256 MiB cgroup limit, 60 MiB cgroup headroom, 128 MiB tmpfs free
    # Cgroup cap: 60% of 60 MiB = 36 MiB. Tmpfs cap: 75% of 128 MiB = 96 MiB.
    # Effective safe capacity = min(256, 36, 96) = 36 MiB.
    topology_a = {
        "system_memory_mb": 512,
        "available_memory_mb": 100,
        "cgroup_limit_mb": 256,
        "cgroup_current_mb": 196,
        "cgroup_headroom_mb": 60,
        "effective_memory_ceiling_mb": 256,
        "tmpfs_total_mb": 128,
        "tmpfs_free_mb": 128,
        "disk_free_mb": 10000,
    }
    rec_a = compute_recommendation("balanced", topology_a, 200.0, 100.0)
    assert rec_a.recommended_hot_cache_max_mb == 36
    assert any("cgroup available headroom" in r for r in rec_a.reasons)
    assert rec_a.operational_status.value == "CONSTRAINED"
    assert rec_a.recommended_hot_cache_max_mb >= rec_a.minimum_operational_hot_mb

    # Case B: 512 MiB total container, 512 MiB cgroup limit, 200 MiB cgroup headroom, 32 MiB tmpfs free
    # Cgroup cap: 60% of 200 MiB = 120 MiB. Tmpfs cap: 75% of 32 MiB = 24 MiB.
    # Effective safe capacity = min(512, 120, 24) = 24 MiB.
    topology_b = {
        "system_memory_mb": 512,
        "available_memory_mb": 200,
        "cgroup_limit_mb": 512,
        "cgroup_current_mb": 312,
        "cgroup_headroom_mb": 200,
        "effective_memory_ceiling_mb": 512,
        "tmpfs_total_mb": 64,
        "tmpfs_free_mb": 32,
        "disk_free_mb": 10000,
    }
    rec_b = compute_recommendation("conservative", topology_b, 200.0, 100.0)
    assert rec_b.recommended_hot_cache_max_mb == 24
    assert any("tmpfs available capacity" in r for r in rec_b.reasons)
    assert rec_b.operational_status.value == "CONSTRAINED"

    # Case C: 512 MiB total container, 256 MiB cgroup limit, 30 MiB cgroup headroom, 20 MiB tmpfs free
    # Cgroup cap: 60% of 30 MiB = 18 MiB. Tmpfs cap: 75% of 20 MiB = 15 MiB.
    # Effective safe capacity = min(256, 18, 15) = 15 MiB.
    # With aggressive high watermark (92%), minimum operational working set is ceil(12.25 / 0.92) = 15 MiB.
    # When safe capacity is < minimum_operational_hot_mb (or e.g. 10 MiB), status is INSUFFICIENT_CAPACITY.
    topology_c = {
        "system_memory_mb": 512,
        "available_memory_mb": 30,
        "cgroup_limit_mb": 256,
        "cgroup_current_mb": 226,
        "cgroup_headroom_mb": 30,
        "effective_memory_ceiling_mb": 256,
        "tmpfs_total_mb": 32,
        "tmpfs_free_mb": 20,
        "disk_free_mb": 10000,
    }
    rec_c = compute_recommendation("aggressive", topology_c, 200.0, 100.0)
    assert rec_c.recommended_hot_cache_max_mb == 15
    assert any("tmpfs available capacity" in r for r in rec_c.reasons)

    # Case D: Bare metal host, 8 GiB RAM, no cgroup limit, hot_cache_dir is on disk (not tmpfs, tmpfs_free_mb is None)
    # Effective ceiling = 8192 MiB.
    # Balanced base sizing: 20% of 8192 MiB = 1638 MiB. Preferred minimum 64 MiB.
    topology_d = {
        "system_memory_mb": 8192,
        "available_memory_mb": 6000,
        "cgroup_limit_mb": None,
        "cgroup_current_mb": None,
        "cgroup_headroom_mb": None,
        "effective_memory_ceiling_mb": 8192,
        "tmpfs_total_mb": None,
        "tmpfs_free_mb": None,
        "disk_free_mb": 100000,
    }
    rec_d = compute_recommendation("balanced", topology_d, 500.0, 300.0)
    assert rec_d.recommended_hot_cache_max_mb == 1638
    assert rec_d.recommended_hot_cache_max_mb >= 64
    assert rec_d.operational_status.value == "NORMAL"

    # Case E: High-memory host, 64 GiB RAM, 32 GiB cgroup limit, 24 GiB headroom, 16 GiB tmpfs free
    # Cgroup cap: 60% of 24576 MiB = 14745 MiB. Tmpfs cap: 75% of 16384 MiB = 12288 MiB.
    # Aggressive base: 35% of 32768 MiB = 11468 MiB (capped by 16384 MiB max base).
    # All caps (14745, 12288) > 11468, so hot cache is 11468 MiB.
    topology_e = {
        "system_memory_mb": 65536,
        "available_memory_mb": 50000,
        "cgroup_limit_mb": 32768,
        "cgroup_current_mb": 8192,
        "cgroup_headroom_mb": 24576,
        "effective_memory_ceiling_mb": 32768,
        "tmpfs_total_mb": 20480,
        "tmpfs_free_mb": 16384,
        "disk_free_mb": 500000,
    }
    rec_e = compute_recommendation("aggressive", topology_e, 2500.0, 800.0)
    assert rec_e.recommended_hot_cache_max_mb == 11468
    assert rec_e.recommended_hot_cache_max_mb <= int(16384 * 0.75)
    assert rec_e.recommended_hot_cache_max_mb <= int(24576 * 0.60)
    assert rec_e.operational_status.value == "NORMAL"


def test_autotune_no_double_subtraction_of_existing_hot_memory():
    """Verify that existing hot memory is not double-subtracted when cgroup accounting already reflects it."""
    # cgroup limit = 1024 MiB. memory.current = 500 MiB (which already includes 150 MiB of tmpfs cache files).
    # Available cgroup headroom is 524 MiB.
    # Tmpfs has 350 MiB free space.
    topology = {
        "system_memory_mb": 8192,
        "available_memory_mb": 4000,
        "cgroup_limit_mb": 1024,
        "cgroup_current_mb": 500,
        "cgroup_headroom_mb": 524,
        "effective_memory_ceiling_mb": 1024,
        "tmpfs_total_mb": 500,
        "tmpfs_free_mb": 350,
        "disk_free_mb": 20000,
    }
    # Balanced base: 20% of 1024 = 204 MiB.
    # Cgroup safe headroom cap: 60% of 524 = 314 MiB.
    # Tmpfs safe cap: 75% of 350 = 262 MiB.
    # Safe hot capacity is min(1024, 314, 262) = 262 MiB.
    # Since 204 MiB <= 262 MiB, recommended hot cache is 204 MiB.
    rec = compute_recommendation("balanced", topology, 500.0, 200.0)
    assert rec.recommended_hot_cache_max_mb == 204
    assert rec.recommended_hot_cache_max_mb <= 262


def test_autotune_operational_status_insufficient_capacity():
    """Verify that when safe capacity is strictly below minimum operational working set, status is INSUFFICIENT_CAPACITY."""
    # With 1 stream, min_prefetch=4, chunk_size=1, header=0.25 at 85% high watermark:
    # working_set = 4.25 MiB -> ceil(4.25 / 0.85) = 5 MiB.
    # If cgroup/tmpfs constrain safe capacity to 3 MiB (< 5 MiB), status must be INSUFFICIENT_CAPACITY.
    topology = {
        "system_memory_mb": 512,
        "available_memory_mb": 10,
        "cgroup_limit_mb": 256,
        "cgroup_current_mb": 251,
        "cgroup_headroom_mb": 5,  # 60% of 5 = 3 MiB
        "effective_memory_ceiling_mb": 256,
        "tmpfs_total_mb": 16,
        "tmpfs_free_mb": 4,  # 75% of 4 = 3 MiB
        "disk_free_mb": 10000,
    }
    rec = compute_recommendation("balanced", topology, 100.0, 50.0)
    assert rec.recommended_hot_cache_max_mb == 3
    assert rec.minimum_operational_hot_mb == 5
    assert rec.operational_status.value == "INSUFFICIENT_CAPACITY"
    assert any("INSUFFICIENT_CAPACITY" in r for r in rec.reasons)


def test_autotune_missing_cgroup_telemetry_resilience():
    """Verify that container with cgroup limit but unreadable memory.current falls back safely with MEDIUM confidence."""
    topology = {
        "system_memory_mb": 8192,
        "available_memory_mb": 4000,
        "cgroup_limit_mb": 1024,
        "cgroup_current_mb": None,
        "cgroup_headroom_mb": None,
        "is_cgroup_active": True,
        "effective_memory_ceiling_mb": 1024,
        "tmpfs_total_mb": 512,
        "tmpfs_free_mb": 256,
        "disk_free_mb": 50000,
        "topology_confidence": "MEDIUM",
    }
    rec = compute_recommendation("balanced", topology, 500.0, 200.0)
    # 50% conservative fallback of 1024 = 512 MiB
    # Tmpfs 75% of 256 = 192 MiB
    # Balanced base = 20% of 1024 = 204 MiB -> clamped to 192 MiB
    assert rec.recommended_hot_cache_max_mb == 192
    assert rec.topology_confidence.value == "MEDIUM"
    assert any("Container cgroup memory.current unavailable" in r for r in rec.reasons)


def test_autotune_non_tmpfs_hot_directory_support():
    """Verify that when hot cache is located on persistent disk, non-tmpfs disk factor applies."""
    topology = {
        "system_memory_mb": 16384,
        "available_memory_mb": 12000,
        "cgroup_limit_mb": None,
        "cgroup_current_mb": None,
        "cgroup_headroom_mb": None,
        "effective_memory_ceiling_mb": 16384,
        "is_hot_tmpfs": False,
        "tmpfs_total_mb": None,
        "tmpfs_free_mb": None,
        "hot_disk_total_mb": 10000,
        "hot_disk_free_mb": 500,  # 50% = 250 MiB disk cap
        "disk_free_mb": 50000,
        "topology_confidence": "HIGH",
    }
    rec = compute_recommendation("balanced", topology, 600.0, 300.0)
    # Balanced base: 20% of 16384 = 3276 MiB -> clamped to disk cap (50% of 500 = 250 MiB)
    assert rec.recommended_hot_cache_max_mb == 250
    assert any("Hot cache on persistent disk" in r for r in rec.reasons)


def test_autotune_cgroup_v1_soft_limit_invariance():
    """Verify that cgroup v1 memory.soft_limit_in_bytes is strictly treated as UNSUPPORTED and invariant.

    It must not leak into cgroup_high_mb, nor affect performance_confidence, operational_status,
    or recommended_hot_cache_max_mb.
    """
    from unittest.mock import patch

    from program.services.streaming.runtime_profile import (
        CgroupCapabilities,
        CgroupVersion,
        LimitState,
        ResourceValue,
        ValueOrigin,
        ValueSemantics,
    )

    # Mock cgroup v1 capabilities: soft_limit_in_bytes exists on disk, but runtime_profile returns UNSUPPORTED
    with patch(
        "program.services.streaming.runtime_profile.CgroupCapabilities.detect"
    ) as mock_detect:
        # Construct realistic cgroup v1 capabilities object
        caps_v1 = CgroupCapabilities(
            version=CgroupVersion.V1,
            memory_current=ResourceValue(
                1024 * 1024 * 1024,
                LimitState.FINITE,
                ValueOrigin.CURRENT_MEASUREMENT,
                ValueSemantics.CURRENT_USAGE,
                "v1 memory.usage_in_bytes",
            ),
            memory_hard_boundary=ResourceValue(
                4 * 1024 * 1024 * 1024,
                LimitState.FINITE,
                ValueOrigin.KERNEL_FACT,
                ValueSemantics.HARD_BOUNDARY,
                "v1 memory.limit_in_bytes",
            ),
            memory_soft_boundary=ResourceValue(
                None,
                LimitState.UNSUPPORTED,
                ValueOrigin.KERNEL_FACT,
                ValueSemantics.SOFT_BOUNDARY,
                "memory.soft_limit_in_bytes is deprecated/ineffective",
            ),
            swap_current=ResourceValue(
                0,
                LimitState.FINITE,
                ValueOrigin.DERIVED,
                ValueSemantics.CURRENT_USAGE,
                "derived",
            ),
            swap_high=ResourceValue(
                None,
                LimitState.UNSUPPORTED,
                ValueOrigin.KERNEL_FACT,
                ValueSemantics.SOFT_BOUNDARY,
                "cgroup v1 has no normalized swap.high",
            ),
            swap_max=ResourceValue(
                None,
                LimitState.UNBOUNDED,
                ValueOrigin.KERNEL_FACT,
                ValueSemantics.HARD_BOUNDARY,
                "unbounded",
            ),
            source_paths={"root": "/sys/fs/cgroup/memory"},
        )
        assert caps_v1.memory_soft_boundary.state.value == "UNSUPPORTED"
        assert caps_v1.memory_soft_boundary.semantics.value == "SOFT_BOUNDARY"
        mock_detect.return_value = caps_v1

        with patch("psutil.virtual_memory") as mock_vm:
            mock_vm.return_value.total = 16 * 1024 * 1024 * 1024
            mock_vm.return_value.available = 12 * 1024 * 1024 * 1024

            topo = detect_system_topology(
                warm_target_path=Path("/tmp"),
                hot_target_path=Path("/tmp"),
            )
            # Must be None - not picked up from soft limit
            assert topo.get("cgroup_high_mb") is None

            rec = compute_recommendation("balanced", topo, 500.0, 300.0)
            assert rec.operational_status.value == "NORMAL"
            assert rec.performance_confidence.value in ("HIGH", "MEDIUM")
            # Recommendation must be computed without throttling
            assert rec.recommended_hot_cache_max_mb > 0


def test_autotune_non_tmpfs_hot_memory_pressure_is_not_physical_disk_capacity():
    topology = {
        "system_memory_mb": 16384,
        "available_memory_mb": 12000,
        "cgroup_limit_mb": 4096,
        "cgroup_current_mb": 3800,
        "cgroup_high_mb": 3500,
        "cgroup_headroom_mb": 296,
        "effective_memory_ceiling_mb": 4096,
        "is_cgroup_active": True,
        "is_hot_tmpfs": False,
        "hot_disk_total_mb": 10000,
        "hot_disk_free_mb": 500,
        "disk_free_mb": 50000,
        "topology_confidence": "HIGH",
    }
    rec = compute_recommendation("balanced", topology, 600.0, 300.0)
    assert rec.physical_hot_storage_ceiling_mb == 500
    assert rec.recommended_hot_cache_max_mb == 250
    assert rec.operational_status.value == "CONSTRAINED"
    assert rec.performance_confidence.value == "LOW"
    assert any(
        "physical disk capacity is unchanged" in reason for reason in rec.reasons
    )


def test_detect_path_disk_space_statvfs_and_missing_path():
    """Verify detect_path_disk_space handles missing paths gracefully without throwing."""
    non_existent = Path("/non/existent/path/that/does/not/exist/anywhere")
    result = detect_path_disk_space(non_existent)
    assert result["total_mb"] > 0
    assert result["free_mb"] > 0


def test_autotune_chunk_size_and_multi_stream_scaling():
    """Verify operational minimum hot capacity derivation across chunk sizes and concurrent streams."""
    # 1. Base 1 MB chunk, 4 prefetch, 0.25 header, 85% high watermark:
    # working_set = (1*4 + 0.25) * 1 = 4.25 MiB -> ceil(4.25 / 0.85) = 5 MiB
    assert (
        calculate_minimum_operational_hot_mb(
            concurrent_streams=1, high_watermark_pct=85.0, chunk_size_mb=1
        )
        == 5
    )

    # 2. 8 MB chunks (4K remux recommendation in settings description):
    # working_set = (8*4 + 0.25) * 1 = 32.25 MiB -> ceil(32.25 / 0.85) = 38 MiB
    assert (
        calculate_minimum_operational_hot_mb(
            concurrent_streams=1, high_watermark_pct=85.0, chunk_size_mb=8
        )
        == 38
    )

    # 3. Multi-stream scaling: 2 streams and 4 streams with 1 MB chunks at 85%
    # 2 streams: (1*4 + 0.25) * 2 = 8.5 MiB -> ceil(8.5 / 0.85) = 10 MiB
    assert (
        calculate_minimum_operational_hot_mb(
            concurrent_streams=2, high_watermark_pct=85.0, chunk_size_mb=1
        )
        == 10
    )
    # 4 streams: (1*4 + 0.25) * 4 = 17.0 MiB -> ceil(17.0 / 0.85) = 20 MiB
    assert (
        calculate_minimum_operational_hot_mb(
            concurrent_streams=4, high_watermark_pct=85.0, chunk_size_mb=1
        )
        == 20
    )

    # 4. Multi-stream scaling with 8 MB chunks:
    # 2 streams: 32.25 * 2 = 64.5 MiB -> ceil(64.5 / 0.85) = 76 MiB
    assert (
        calculate_minimum_operational_hot_mb(
            concurrent_streams=2, high_watermark_pct=85.0, chunk_size_mb=8
        )
        == 76
    )
    # 4 streams: 32.25 * 4 = 129.0 MiB -> ceil(129.0 / 0.85) = 152 MiB
    assert (
        calculate_minimum_operational_hot_mb(
            concurrent_streams=4, high_watermark_pct=85.0, chunk_size_mb=8
        )
        == 152
    )


def test_autotune_status_precedence_insufficient_vs_constrained_vs_normal():
    """Verify strict status precedence: INSUFFICIENT_CAPACITY > CONSTRAINED > NORMAL."""
    # Test topology with varying safe cgroup headroom
    base_topology = {
        "system_memory_mb": 4096,
        "available_memory_mb": 2048,
        "cgroup_limit_mb": 1024,
        "cgroup_current_mb": 1000,
        "effective_memory_ceiling_mb": 1024,
        "tmpfs_total_mb": 1024,
        "disk_free_mb": 50000,
        "topology_confidence": "HIGH",
    }

    # Case 1: Safe hot capacity (30 MiB) is above operational minimum (10 MiB for 2 streams)
    # but below preferred baseline (64 MiB) -> CONSTRAINED
    topo_constrained = dict(
        base_topology, cgroup_headroom_mb=50, tmpfs_free_mb=50
    )  # 60% of 50 = 30 MiB
    rec_constrained = compute_recommendation(
        "balanced", topo_constrained, 100.0, 50.0, chunk_size_mb=1, concurrent_streams=2
    )
    assert rec_constrained.recommended_hot_cache_max_mb == 30
    assert rec_constrained.minimum_operational_hot_mb == 10
    assert rec_constrained.operational_status.value == "CONSTRAINED"

    # Case 2: Safe hot capacity (80 MiB) is above preferred baseline (64 MiB), but with 8 MB chunks and 4 streams,
    # the operational minimum is 152 MiB. Even though 80 > 64, safe capacity is below operational minimum!
    # Status MUST be INSUFFICIENT_CAPACITY, not NORMAL or CONSTRAINED!
    topo_insufficient = dict(
        base_topology, cgroup_headroom_mb=133, tmpfs_free_mb=200
    )  # 60% of 133 = 79-80 MiB
    rec_insufficient = compute_recommendation(
        "balanced",
        topo_insufficient,
        100.0,
        50.0,
        chunk_size_mb=8,
        concurrent_streams=4,
    )
    assert rec_insufficient.recommended_hot_cache_max_mb <= 80
    assert rec_insufficient.minimum_operational_hot_mb == 152
    assert rec_insufficient.operational_status.value == "INSUFFICIENT_CAPACITY"

    # Case 3: Safe hot capacity (204 MiB) >= preferred 64 MiB AND >= operational minimum (10 MiB) -> NORMAL
    topo_normal = dict(base_topology, cgroup_headroom_mb=500, tmpfs_free_mb=500)
    rec_normal = compute_recommendation(
        "balanced", topo_normal, 100.0, 50.0, chunk_size_mb=1, concurrent_streams=2
    )
    assert rec_normal.recommended_hot_cache_max_mb == 204  # 20% of 1024
    assert rec_normal.operational_status.value == "NORMAL"


def test_autotune_recommendation_empirical_record_validation_and_invalidation():
    """Verify that compute_recommendation validates empirical capacity records and rejects stale ones."""
    from program.services.streaming.runtime_profile import (
        CapacityTestRecord,
        build_runtime_profile,
    )

    profile = build_runtime_profile(config_dict={"chunk_size_mb": 4})
    topology = {
        "system_memory_mb": 8192,
        "available_memory_mb": 4000,
        "cgroup_limit_mb": 2048,
        "cgroup_current_mb": 500,
        "cgroup_headroom_mb": 1548,
        "effective_memory_ceiling_mb": 2048,
        "tmpfs_total_mb": 1024,
        "tmpfs_free_mb": 800,
        "disk_free_mb": 50000,
    }

    # 1. Valid matching empirical record
    valid_record = CapacityTestRecord(
        runtime_profile_id=profile.profile_id,
        environment_fingerprint=profile.environment_fingerprint,
        configuration_fingerprint=profile.configuration_fingerprint,
        provider_context="rd",
        network_context="lan",
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
    rec_valid = compute_recommendation(
        "balanced",
        topology,
        500.0,
        200.0,
        capacity_test_record=valid_record,
        current_runtime_profile=profile,
    )
    assert any("Empirical CapacityTestRecord validated" in r for r in rec_valid.reasons)

    # 2. Stale empirical record with drifted environment
    stale_record = CapacityTestRecord(
        runtime_profile_id=profile.profile_id,
        environment_fingerprint="drifted_environment_hash",
        configuration_fingerprint=profile.configuration_fingerprint,
        provider_context="rd",
        network_context="lan",
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
    rec_stale = compute_recommendation(
        "balanced",
        topology,
        500.0,
        200.0,
        capacity_test_record=stale_record,
        current_runtime_profile=profile,
    )
    assert any(
        "Empirical CapacityTestRecord invalidated" in r for r in rec_stale.reasons
    )
    assert any("record discarded" in r for r in rec_stale.reasons)


def test_autotune_recommendation_policy_metadata_and_watermark_semantics():
    """Verify PolicyMetadata attachment, ValueSemantics.POLICY_TARGET for watermarks, and totalization semantics."""
    from program.services.streaming.cache_autotune import PolicyMetadata

    topology = {
        "system_memory_mb": 8192,
        "available_memory_mb": 4000,
        "cgroup_limit_mb": 2048,
        "cgroup_current_mb": 1000,
        "cgroup_headroom_mb": 1048,
        "effective_memory_ceiling_mb": 2048,
        "tmpfs_total_mb": 1024,
        "tmpfs_free_mb": 512,
        "disk_free_mb": 50000,
        "current_hot_mb": 100,
        "current_warm_mb": 2000,
        "topology_confidence": "HIGH",
    }
    rec = compute_recommendation("balanced", topology, 500.0, 200.0)

    # 1. Policy metadata verification
    assert rec.policy_metadata is not None
    assert isinstance(rec.policy_metadata, PolicyMetadata)
    assert rec.policy_metadata.policy_id == "cineflow.cache-capacity"
    assert rec.policy_metadata.policy_version == "1.0.0"
    assert rec.policy_metadata.origin == "FALLBACK_POLICY"
    assert rec.policy_metadata.semantics == "POLICY_TARGET"
    assert rec.policy_metadata.confidence >= 0.90
    assert rec.policy_metadata.parameters["current_hot_mb"] == 100
    assert rec.policy_metadata.parameters["current_warm_mb"] == 2000

    # 2. Totalization verification (H and W preserved without double-subtraction)
    # Tmpfs headroom: 512 * 0.75 = 384 MiB.
    # Total tmpfs possible = H (100) + 384 = 484 MiB.
    # Cgroup headroom: 1048 * 0.60 = 628 MiB.
    # Total cgroup possible = H (100) + 628 = 728 MiB.
    # Base sizing: 20% of 2048 = 409 MiB <= 484 MiB safe capacity.
    assert rec.recommended_hot_cache_max_mb == 409
    assert rec.effective_safe_hot_capacity_mb == 484

    # Warm totalization: W (2000) + max(0, 50000 - 1024) = 50976 MiB
    assert rec.recommended_warm_cache_max_mb == 2000 + (50000 - 1024)


def test_autotune_unknown_cgroup_current_yields_none_headroom_and_separate_policy_allowance():
    """Verify that when cgroup limit is 4 GiB but memory.current is UNKNOWN:
    1. cgroup_headroom_mb is strictly None (DERIVED headroom cannot be computed without physical measurement).
    2. Physical facts metadata reports cgroup_headroom_mb as None.
    3. Policy memory allowance is calculated separately under FALLBACK_POLICY / POLICY_TARGET (50% of 4 GiB = 2048 MiB).
    4. Physical fact origin KERNEL_FACT is preserved and kept distinct from FALLBACK_POLICY.
    """
    from unittest.mock import MagicMock, patch

    from program.services.streaming.runtime_profile import (
        CgroupCapabilities,
        LimitState,
        ResourceValue,
        ValueOrigin,
        ValueSemantics,
    )

    caps_v2 = CgroupCapabilities(
        version=2,
        source_paths={"root": "/sys/fs/cgroup"},
        memory_hard_boundary=ResourceValue(
            4 * 1024 * 1024 * 1024,
            LimitState.FINITE,
            ValueOrigin.KERNEL_FACT,
            ValueSemantics.HARD_BOUNDARY,
            "memory.max",
        ),
        memory_soft_boundary=ResourceValue(
            None,
            LimitState.UNBOUNDED,
            ValueOrigin.KERNEL_FACT,
            ValueSemantics.SOFT_BOUNDARY,
            "memory.high",
        ),
        memory_current=ResourceValue(
            None,
            LimitState.UNKNOWN,
            ValueOrigin.CURRENT_MEASUREMENT,
            ValueSemantics.CURRENT_USAGE,
            "permission denied reading memory.current",
        ),
        swap_current=ResourceValue(
            None,
            LimitState.UNBOUNDED,
            ValueOrigin.KERNEL_FACT,
            ValueSemantics.CURRENT_USAGE,
        ),
        swap_high=ResourceValue(
            None,
            LimitState.UNBOUNDED,
            ValueOrigin.KERNEL_FACT,
            ValueSemantics.SOFT_BOUNDARY,
        ),
        swap_max=ResourceValue(
            None,
            LimitState.UNBOUNDED,
            ValueOrigin.KERNEL_FACT,
            ValueSemantics.HARD_BOUNDARY,
        ),
    )

    with patch(
        "program.services.streaming.runtime_profile.CgroupCapabilities.detect",
        return_value=caps_v2,
    ):
        with patch("psutil.virtual_memory") as mock_vm:
            mock_vm.return_value.total = 16 * 1024 * 1024 * 1024
            mock_vm.return_value.available = 12 * 1024 * 1024 * 1024

            topo = detect_system_topology(
                warm_target_path=Path("/tmp"),
                hot_target_path=Path("/tmp"),
            )
            # Ensure topology indicates hot directory is on tmpfs for tmpfs policy allowance check
            topo["is_hot_tmpfs"] = True
            topo["tmpfs_total_mb"] = 2048
            topo["tmpfs_free_mb"] = 2048

            # Assert cgroup limit is recognized
            assert topo["cgroup_limit_mb"] == 4096
            assert topo["cgroup_current_mb"] is None
            # Physical headroom must be strictly None when current usage is UNKNOWN
            assert topo["cgroup_headroom_mb"] is None
            assert topo["is_cgroup_active"] is True
            # Topology confidence is MEDIUM due to missing memory.current
            assert topo["topology_confidence"].value == "MEDIUM"

            rec = compute_recommendation("balanced", topo, 500.0, 200.0)

            # Policy metadata checks
            assert rec.policy_metadata is not None
            phys_facts = rec.policy_metadata.parameters["physical_facts"]
            assert phys_facts["cgroup_headroom_mb"] is None
            assert phys_facts["policy_memory_allowance_mb"] is not None
            # 50% fallback of 4096 MiB = 2048 MiB
            assert phys_facts["policy_memory_allowance_mb"] == 2048
            assert phys_facts["kernel_fact_origin"] == "KERNEL_FACT"
            assert rec.policy_metadata.origin == "FALLBACK_POLICY"
            assert rec.policy_metadata.semantics == "POLICY_TARGET"
            assert any(
                "memory.current unavailable; applied conservative 50% cgroup limit safety cap"
                in r
                for r in rec.reasons
            )


def test_autotune_stale_empirical_record_rejected_upon_config_or_context_drift():
    """Verify that empirical capacity calibration is rejected when config, provider, or network context drifts."""
    from program.services.streaming.runtime_profile import (
        CapacityTestRecord,
        build_runtime_profile,
        is_calibration_valid_for_profile,
    )

    base_profile = build_runtime_profile(
        config_dict={"chunk_size_mb": 4, "prefetch_window": 8},
        provider_context="real_debrid_direct",
        network_context="10g_lan",
    )

    base_record = CapacityTestRecord(
        runtime_profile_id=base_profile.profile_id,
        environment_fingerprint=base_profile.environment_fingerprint,
        configuration_fingerprint=base_profile.configuration_fingerprint,
        provider_context="real_debrid_direct",
        network_context="10g_lan",
        stream_count=1,
        bitrate_profile_bps=(15000000,),
        aggregate_demand_bps=15000000,
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

    # 1. Matching contexts -> valid
    valid, inv = is_calibration_valid_for_profile(base_record, base_profile)
    assert valid is True
    assert len(inv) == 0

    # 2. Configuration drift (e.g. chunk size changed to 8 MB)
    drifted_config_profile = build_runtime_profile(
        config_dict={"chunk_size_mb": 8, "prefetch_window": 8},
        provider_context="real_debrid_direct",
        network_context="10g_lan",
    )
    valid, inv = is_calibration_valid_for_profile(base_record, drifted_config_profile)
    assert valid is False
    assert "cache" in inv
    assert "streaming" in inv

    # 3. Provider context drift
    drifted_provider_profile = build_runtime_profile(
        config_dict={"chunk_size_mb": 4, "prefetch_window": 8},
        provider_context="torbox",
        network_context="10g_lan",
    )
    valid, inv = is_calibration_valid_for_profile(base_record, drifted_provider_profile)
    assert valid is False
    assert "provider" in inv

    # 4. Network context drift
    drifted_network_profile = build_runtime_profile(
        config_dict={"chunk_size_mb": 4, "prefetch_window": 8},
        provider_context="real_debrid_direct",
        network_context="4g_lte_mobile",
    )
    valid, inv = is_calibration_valid_for_profile(base_record, drifted_network_profile)
    assert valid is False
    assert "network" in inv
