import pytest

from program.services.streaming.adaptive_prefetch import (
    AdaptivePrefetchConfig,
    AdaptivePrefetchManager,
)


def test_window_calculation_known_bitrates():
    mgr = AdaptivePrefetchManager()

    # Low bitrate: 2 Mbps -> buffer = 2_000_000 * 15 / 8 = 3,750,000 bytes -> ceil(3.57) = 4
    mgr.set_metadata_bitrate(2_000_000)
    assert mgr.calculate_window() == 4

    # Medium bitrate: 10 Mbps -> buffer = 10_000_000 * 15 / 8 = 18,750,000 bytes -> ceil(17.88) = 18
    mgr.set_metadata_bitrate(10_000_000)
    assert mgr.calculate_window() == 18

    # High bitrate: 80 Mbps -> buffer = 80_000_000 * 15 / 8 = 150_000_000 bytes -> ceil(143) -> capped at 48
    mgr.set_metadata_bitrate(80_000_000)
    assert mgr.calculate_window() == 48


def test_window_calculation_unknown_bitrate_fallback():
    mgr = AdaptivePrefetchManager()
    assert mgr.get_effective_bitrate() == 0
    # Falls back to default 12
    assert mgr.calculate_window() == 12


def test_pool_pressure_throttling():
    mgr = AdaptivePrefetchManager()
    mgr.set_metadata_bitrate(80_000_000)  # normally 48 chunks

    # Normal load: 10 active leases
    assert mgr.calculate_window(active_leases=10, max_leases=64) == 48

    # High load: 48 active leases (halfway between 32 and 64)
    # remaining = 16 / 32 = 0.5 -> 48 * 0.5 = 24
    window_high_load = mgr.calculate_window(active_leases=48, max_leases=64)
    assert window_high_load == 24

    # Extreme load: 60 active leases
    # remaining = 4 / 32 = 0.125 -> 48 * 0.125 = 6
    window_extreme = mgr.calculate_window(active_leases=60, max_leases=64)
    assert window_extreme == 6


def test_cache_pressure_throttling():
    mgr = AdaptivePrefetchManager()
    mgr.set_metadata_bitrate(10_000_000)  # normally 18 chunks

    # Cache at 70% -> no throttling
    assert mgr.calculate_window(cache_usage_pct=70.0) == 18

    # Cache at 90% -> throttled to 50%
    assert mgr.calculate_window(cache_usage_pct=90.0) == 9

    # Cache at 96% -> throttled to minimum (4)
    assert mgr.calculate_window(cache_usage_pct=96.0) == 4


def test_read_tracking_and_bitrate_estimation():
    mgr = AdaptivePrefetchManager()

    # Read 1: 0 to 1MB at t=0.0
    is_seq1 = mgr.record_read(0, 1048575, current_time=100.0)
    assert not is_seq1  # first read, not sequential yet
    assert mgr.current_generation == 0

    # Read 2: 1MB to 2MB at t=0.5s -> 1MB in 0.5s = 16 Mbps
    is_seq2 = mgr.record_read(1048576, 2097151, current_time=100.5)
    assert is_seq2
    assert mgr.current_generation == 0
    assert 15_000_000 <= mgr.estimated_bitrate <= 17_000_000

    # Effective bitrate should now reflect estimated
    assert mgr.get_effective_bitrate() > 0


def test_seek_detection_and_generation_advancement():
    mgr = AdaptivePrefetchManager()

    mgr.record_read(0, 1048575, current_time=10.0)
    mgr.record_read(1048576, 2097151, current_time=10.5)
    assert mgr.current_generation == 0

    # Player seeks to 50 MB
    seek_start = 50 * 1024 * 1024
    seek_end = seek_start + 1048575
    is_seq = mgr.record_read(seek_start, seek_end, current_time=11.0)
    assert not is_seq
    assert mgr.current_generation == 1
    assert mgr.seek_count == 1

    # Prefetch started under gen 0 should now be aborted
    assert mgr.should_abort_prefetch(0) is True
    # Prefetch started under gen 1 should NOT be aborted
    assert mgr.should_abort_prefetch(1) is False


def test_post_seek_ramp_up():
    # 80 Mbps stream -> normal window is 48 chunks
    mgr = AdaptivePrefetchManager(initial_bitrate=80_000_000)
    assert mgr.calculate_window() == 48

    # Initial playback starts at 0
    mgr.record_read(0, 1048575, current_time=0.5)
    assert mgr.seek_count == 0

    # Player seeks to 100 MB
    seek_start = 100 * 1024 * 1024
    seek_end = seek_start + 1048575
    mgr.record_read(seek_start, seek_end, current_time=1.0)
    assert mgr.seek_count == 1
    assert mgr.consecutive_sequential_reads == 0

    # 1st sequential read window is ramp-capped at 8 chunks
    assert mgr.calculate_window() == 8

    # 2nd sequential read
    next_start = seek_end + 1
    next_end = next_start + 1048575
    mgr.record_read(next_start, next_end, current_time=1.1)
    assert mgr.consecutive_sequential_reads == 1
    # 2nd sequential read window is ramp-capped at 16 chunks
    assert mgr.calculate_window() == 16

    # 3rd sequential read
    next_start = next_end + 1
    next_end = next_start + 1048575
    mgr.record_read(next_start, next_end, current_time=1.2)
    assert mgr.consecutive_sequential_reads == 2
    # 3rd sequential read window is ramp-capped at 24 chunks
    assert mgr.calculate_window() == 24

    # 4th sequential read
    next_start = next_end + 1
    next_end = next_start + 1048575
    mgr.record_read(next_start, next_end, current_time=1.3)
    assert mgr.consecutive_sequential_reads == 3
    # Fully ramped up back to full 48 chunks!
    assert mgr.calculate_window() == 48


def test_time_aware_runway_under_pressure_detection():
    # 20 Mbps bitrate = 2,500,000 bytes/sec
    mgr = AdaptivePrefetchManager(initial_bitrate=20_000_000)

    # Simulated fetch latency p95 = 2.0s -> required safety = 2.0 * 1.5 = 3.0s
    # min_safe_runway_seconds is 4.0s -> target is max(4.0, 3.0) = 4.0s
    # 4.0s * 2,500,000 bytes/sec = 10,000,000 bytes (~10 MB)

    # 1. Healthy runway: 15 MB cached -> ~6.0s runway -> not under pressure
    assert not mgr.is_runway_under_pressure(cached_runway_bytes=15_000_000)

    # 2. Dangerous runway: 5 MB cached -> 2.0s runway -> under pressure
    assert mgr.is_runway_under_pressure(cached_runway_bytes=5_000_000)

    # 3. Dangerous cached runway but in-flight request pending (5 MB + 6 MB in-flight = 11 MB -> 4.4s) -> not under pressure
    assert not mgr.is_runway_under_pressure(
        cached_runway_bytes=5_000_000, in_flight_bytes=6_000_000
    )

    # 4. Record a high-latency distribution (p95 is 4.0s -> required safety = 6.0s = 15 MB)
    for _ in range(19):
        mgr.record_provider_fetch(4.0)
    mgr.record_provider_fetch(1.5)
    # Now 12 MB total runway (4.8s) is under pressure because fetch tail requires 6.0s
    assert mgr.is_runway_under_pressure(cached_runway_bytes=12_000_000)


def test_runway_observation_uses_inflight_contiguous_bytes():
    mgr = AdaptivePrefetchManager(initial_bitrate=20_000_000)
    mgr.record_runway(cached_bytes=5_000_000, in_flight_bytes=6_000_000)
    assert mgr.effective_runway_seconds() == pytest.approx(4.4, abs=0.01)
    assert not mgr.should_prefetch_now()


def test_provider_fetch_and_starvation_observations():
    mgr = AdaptivePrefetchManager(initial_bitrate=20_000_000)
    mgr.record_provider_fetch(1.25)
    mgr.record_starvation()
    assert mgr.get_fetch_p95() == pytest.approx(1.25)
    assert mgr.starvation_count == 1
