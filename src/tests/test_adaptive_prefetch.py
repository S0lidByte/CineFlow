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
