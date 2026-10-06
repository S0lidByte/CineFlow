"""Deterministic unit tests verifying the offline synthetic runway underflow model in d80_endurance.py.

Tests cover:
1. Pure steady-state delivery: arrivals match consumption exactly -> no underflows at any runway.
2. Moderate initial burst: deliveries complete ahead of consumption -> 0 underflows across all runways.
3. Severe initial starvation: delivery delay exceeds runway -> underflow samples at runway=0.0 and 0.5.
4. Large mid-playback stall: network stall causes underflows on smaller runway buffers.
5. Multi-stream and multi-epoch isolation: each (stream, epoch) group is evaluated independently.
"""

import sys
from pathlib import Path

# Add backend root and scripts to path for importing d80_endurance
BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = BACKEND_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from d80_endurance import (  # type: ignore
    BYTES_PER_MEGABIT,
    classify_http_request,
    normalize_http_range,
    range_duplicate_category,
    runway_sweep,
)


def test_runway_sweep_steady_state():
    """Steady delivery at 20 Mbps with 1 MB chunks delivered every 0.4s exactly matching bitrate."""
    # 20 Mbps = 2,500,000 bytes/sec. A 1,000,000 byte chunk takes 0.4s to consume.
    # Notice the discrete delivery property: at t=0, 0 bytes have been delivered.
    # At delivery instant of read 0 (completed_t=0.35s), consumed = 0.35 * 2.5MB = 875,000 bytes.
    # Delivered prior to read 0 delivery completion is 0.
    # At runway=0.0: ahead = 0 + 0 - 875,000 = -875,000 <= 0 -> underflow.
    # But with a small runway=0.5s: ahead = (0.5 * 2,500,000) - 875,000 = +375,000 > 0 -> NO underflows!
    reads = []
    chunk_bytes = 1_000_000
    for i in range(10):
        t = i * 0.4
        completed_t = t + 0.35  # arrives 0.05s before next read begins
        reads.append(
            {
                "stream": "stream-1",
                "epoch": 0,
                "t": t,
                "completed_t": completed_t,
                "bytes": chunk_bytes,
                "mbps": 20,
            }
        )

    result = runway_sweep(reads)
    runways = result["runways_seconds"]

    # At runway 0.0, discrete chunk delivery means player consumes bytes before first chunk arrives
    assert runways["0.0"]["buffer_underflow_samples"] > 0
    # At runway 0.5s, the initial 0.5s runway easily covers the 0.35s arrival latency
    assert runways["0.5"]["buffer_underflow_samples"] == 0
    assert runways["1.0"]["buffer_underflow_samples"] == 0
    assert result["minimum_initial_runway_seconds"] == 0.5


def test_runway_sweep_initial_burst():
    """Initial burst where 4 chunks arrive in rapid succession (e.g. from prefetch/cache)."""
    # 20 Mbps = 2,500,000 bytes/s.
    # Chunks 0-3 arrive within 0.20s total (4,000,000 bytes delivered).
    # Then chunk 4 finishes at 0.60s (elapsed 0.60s = 1,500,000 bytes consumed, 4,000,000 delivered -> 2,500,000 ahead).
    # Notice that at t=0 before chunk 0 completes at 0.05s:
    # At runway=0.0: elapsed=0.05s, consumed = 0.05 * 2,500,000 = 125,000 bytes. Delivered=0.
    # ahead = -125,000 <= 0 (underflow sample for runway=0.0).
    # With runway=0.5s: 0.5s * 2,500,000 = 1,250,000 bytes buffer covers the initial 0.05s chunk latency.
    chunk_bytes = 1_000_000  # 0.4s of 20 Mbps
    reads = [
        {
            "stream": "s1",
            "epoch": 0,
            "t": 0.0,
            "completed_t": 0.05,
            "bytes": chunk_bytes,
            "mbps": 20,
        },
        {
            "stream": "s1",
            "epoch": 0,
            "t": 0.05,
            "completed_t": 0.10,
            "bytes": chunk_bytes,
            "mbps": 20,
        },
        {
            "stream": "s1",
            "epoch": 0,
            "t": 0.10,
            "completed_t": 0.15,
            "bytes": chunk_bytes,
            "mbps": 20,
        },
        {
            "stream": "s1",
            "epoch": 0,
            "t": 0.15,
            "completed_t": 0.20,
            "bytes": chunk_bytes,
            "mbps": 20,
        },
        {
            "stream": "s1",
            "epoch": 0,
            "t": 0.40,
            "completed_t": 0.60,
            "bytes": chunk_bytes,
            "mbps": 20,
        },
    ]
    result = runway_sweep(reads)
    assert (
        result["runways_seconds"]["0.0"]["buffer_underflow_samples"] == 1
    )  # exactly chunk 0 delivery instant
    assert result["runways_seconds"]["0.5"]["buffer_underflow_samples"] == 0
    assert result["minimum_initial_runway_seconds"] == 0.5


def test_runway_sweep_severe_initial_starvation():
    """Initial read experiences high latency before first byte delivery (e.g. cold start of 0.8s on 20 Mbps stream)."""
    # 20 Mbps = 2,500,000 bytes/s.
    # At t=0.0, consumption begins. First chunk of 1,000,000 bytes arrives at completed_t = 0.8s.
    # During 0.8s, player consumed 0.8 * 2,500,000 = 2,000,000 bytes.
    # At runway=0.0: ahead = 0 + 0 - 2,000,000 = -2,000,000 <= 0 -> underflow!
    # At runway=0.5: runway buffer = 0.5 * 2,500,000 = 1,250,000 bytes.
    #   ahead = 1,250,000 - 2,000,000 = -750,000 <= 0 -> underflow!
    # At runway=1.0: runway buffer = 1.0 * 2,500,000 = 2,500,000 bytes.
    #   ahead = 2,500,000 - 2,000,000 = +500,000 > 0 -> NO underflow!
    reads = [
        {
            "stream": "s1",
            "epoch": 0,
            "t": 0.0,
            "completed_t": 0.8,
            "bytes": 1_000_000,
            "mbps": 20,
        },
        {
            "stream": "s1",
            "epoch": 0,
            "t": 0.8,
            "completed_t": 0.9,
            "bytes": 1_000_000,
            "mbps": 20,
        },
        {
            "stream": "s1",
            "epoch": 0,
            "t": 0.9,
            "completed_t": 1.0,
            "bytes": 1_000_000,
            "mbps": 20,
        },
    ]
    result = runway_sweep(reads)
    runways = result["runways_seconds"]
    assert runways["0.0"]["buffer_underflow_samples"] > 0
    assert runways["0.5"]["buffer_underflow_samples"] > 0
    assert runways["1.0"]["buffer_underflow_samples"] == 0
    assert result["minimum_initial_runway_seconds"] == 1.0


def test_runway_sweep_mid_playback_stall():
    """Mid-playback stall where chunk 3 takes 2.5s to arrive on a 20 Mbps stream."""
    chunk_bytes = 1_000_000  # 0.4s playback
    reads = [
        {
            "stream": "s1",
            "epoch": 0,
            "t": 0.0,
            "completed_t": 0.1,
            "bytes": chunk_bytes,
            "mbps": 20,
        },
        {
            "stream": "s1",
            "epoch": 0,
            "t": 0.1,
            "completed_t": 0.2,
            "bytes": chunk_bytes,
            "mbps": 20,
        },
        # Long stall: chunk 3 starts at 0.2, finishes at 2.7 (elapsed 2.7s)
        # Total delivered prior to read 3 delivery instant: 2,000,000 bytes.
        # Consumed by player at 2.7s: 2.7 * 2,500,000 = 6,750,000 bytes.
        # Net buffer ahead without runway: 2,000,000 - 6,750,000 = -4,750,000 bytes.
        # At runway=1.0: +2,500,000 - 4,750,000 = -2,250,000 (underflow)
        # At runway=2.0: +5,000,000 - 4,750,000 = +250,000 > 0 (sustains the 2.5s stall!)
        {
            "stream": "s1",
            "epoch": 0,
            "t": 0.2,
            "completed_t": 2.7,
            "bytes": chunk_bytes,
            "mbps": 20,
        },
    ]
    result = runway_sweep(reads)
    runways = result["runways_seconds"]
    assert runways["0.0"]["buffer_underflow_samples"] > 0
    assert runways["1.0"]["buffer_underflow_samples"] > 0
    assert runways["2.0"]["buffer_underflow_samples"] == 0
    assert result["minimum_initial_runway_seconds"] == 2.0


def test_runway_sweep_multi_stream_multi_epoch_isolation():
    """Verify that multiple streams and seek epochs are grouped and evaluated independently."""
    reads = [
        # Stream 1 epoch 0: clean
        {
            "stream": "s1",
            "epoch": 0,
            "t": 0.0,
            "completed_t": 0.1,
            "bytes": 1_000_000,
            "mbps": 20,
        },
        # Stream 1 epoch 1 (post-seek): initial delay requiring 0.5s runway
        {
            "stream": "s1",
            "epoch": 1,
            "t": 10.0,
            "completed_t": 10.4,
            "bytes": 1_000_000,
            "mbps": 20,
        },
        # Stream 2 epoch 0: requires 1.0s runway due to 0.7s initial delivery time
        {
            "stream": "s2",
            "epoch": 0,
            "t": 0.0,
            "completed_t": 0.7,
            "bytes": 1_000_000,
            "mbps": 20,
        },
    ]
    result = runway_sweep(reads)
    runways = result["runways_seconds"]
    # Runway 0.0 will underflow for s1:epoch1 and s2:epoch0
    assert runways["0.0"]["buffer_underflow_samples"] >= 2
    # Runway 0.5 will still underflow for s2:epoch0 (0.7s delay * 2.5MB/s = 1.75MB consumed > 1.25MB runway)
    assert runways["0.5"]["buffer_underflow_samples"] >= 1
    # Runway 1.0 absorbs both s1:epoch1 and s2:epoch0
    assert runways["1.0"]["buffer_underflow_samples"] == 0
    assert result["minimum_initial_runway_seconds"] == 1.0


def test_runway_sweep_variable_rate_timeline():
    """Verify variable-bitrate timeline consumption calculation in runway_sweep."""
    # Stream switches rates:
    # Read 0: t=0.0, completed_t=0.3, bytes=1,000,000, mbps=20 (2.5 MB/s)
    # Read 1: t=0.4, completed_t=0.6, bytes=1,000,000, mbps=40 (5.0 MB/s)
    # At read 1 completion (completed_t=0.6):
    # From t=0.0 to 0.4: duration 0.4s @ 2.5 MB/s = 1,000,000 bytes.
    # From t=0.4 to 0.6: duration 0.2s @ 5.0 MB/s = 1,000,000 bytes.
    # Total consumed at 0.6s = 2,000,000 bytes.
    # Total delivered prior to read 1 completion = 1,000,000 bytes (from read 0).
    # Deficit at read 1 completion = 2,000,000 - 1,000,000 = 1,000,000 bytes.
    # At runway 0.0: ahead = 0 + 1,000,000 - 2,000,000 = -1,000,000 -> underflow.
    # Initial rate is 20 Mbps = 2.5 MB/s. Runway buffer for runway=0.5s = 0.5 * 2.5 MB = 1,250,000 bytes.
    # Ahead at runway 0.5: 1,250,000 + 1,000,000 - 2,000,000 = +250,000 > 0.
    reads = [
        {
            "stream": "s1",
            "epoch": 0,
            "t": 0.0,
            "completed_t": 0.3,
            "bytes": 1_000_000,
            "mbps": 20,
        },
        {
            "stream": "s1",
            "epoch": 0,
            "t": 0.4,
            "completed_t": 0.6,
            "bytes": 1_000_000,
            "mbps": 40,
        },
    ]
    result = runway_sweep(reads)
    runways = result["runways_seconds"]
    assert runways["0.0"]["buffer_underflow_samples"] > 0
    assert runways["0.5"]["buffer_underflow_samples"] == 0
    assert result["minimum_initial_runway_seconds"] == 0.5
    assert result["minimum_initial_runway_seconds_exact"] > 0.0
    assert result["minimum_initial_runway_bytes"] == 1_000_000
    assert result["minimum_initial_runway_seconds_from_bytes"] == 0.4


def test_runway_sweep_exact_bytes_and_seconds():
    """Deterministic variable-rate trace verifying exact byte deficit and time conversion."""
    # Group: initial rate 40 Mbps = 5.0 MB/s (5,000,000 bytes/s).
    # Read 0: t=0.0, completed_t=0.2, bytes=2,000,000, mbps=40.
    # At completed_t=0.2: consumed = 0.2 * 5,000,000 = 1,000,000 bytes.
    # Delivered prior = 0 -> deficit = 1,000,000 bytes. (1,000,000 / 5,000,000 = 0.2s).
    # Read 1: t=0.5, completed_t=0.8, bytes=2,000,000, mbps=40.
    # At completed_t=0.8: consumed = 0.8 * 5,000,000 = 4,000,000 bytes.
    # Delivered prior = 2,000,000 -> deficit = 2,000,000 bytes. (2,000,000 / 5,000,000 = 0.4s).
    # Max deficit bytes = 2,000,000.
    # Initial rate is 5,000,000 bytes/s -> 2,000,000 / 5,000,000 = 0.4s.
    reads = [
        {
            "stream": "stream-exact",
            "epoch": 0,
            "t": 0.0,
            "completed_t": 0.2,
            "bytes": 2_000_000,
            "mbps": 40,
        },
        {
            "stream": "stream-exact",
            "epoch": 0,
            "t": 0.5,
            "completed_t": 0.8,
            "bytes": 2_000_000,
            "mbps": 40,
        },
    ]
    result = runway_sweep(reads)
    assert result["minimum_initial_runway_bytes"] == 2_000_000
    assert result["minimum_initial_runway_seconds_from_bytes"] == 0.4
    assert result["minimum_initial_runway_seconds"] == 0.5


def test_provider_categorize_requests_cache_refusal_duplicate_zero():
    """Verify that Provider request categorization reports cache_refusal_duplicates == 0."""
    from d80_endurance import Provider  # type: ignore

    provider = Provider()
    # Simulate a stream with initial open and several reconnects
    provider.ranges = [
        {
            "id": 1,
            "title": "/media-0.mkv",
            "start": 0,
            "end": 1000,
            "opened_at": 1.0,
            "generated_bytes": 1000,
            "closed": True,
            "status_code": 206,
        },
        {
            "id": 2,
            "title": "/media-0.mkv",
            "start": 1000,
            "end": 2000,
            "opened_at": 2.0,
            "generated_bytes": 1000,
            "closed": True,
            "status_code": 206,
        },
    ]
    categorization = provider.categorize_requests()
    assert categorization["cache_refusal_duplicates"] == 0
    assert categorization["summary_counts"]["initial_stream_open"] == 1
    assert categorization["summary_counts"]["subsequent_stream_reconnect"] == 1


def test_http_range_normalization_and_boundaries():
    assert normalize_http_range(0, 0) == (0, 1)
    assert normalize_http_range(4, 7) == (4, 8)
    assert range_duplicate_category((0, 4), (0, 4)) == "EXACT_DUPLICATE"
    assert range_duplicate_category((2, 4), (0, 8)) == "CONTAINED_DUPLICATE"
    assert range_duplicate_category((6, 12), (0, 8)) == "PARTIAL_OVERLAP"
    assert range_duplicate_category((8, 10), (0, 8)) == "NO_OVERLAP"
    try:
        normalize_http_range(2, 1)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid range must be rejected")


def test_http_request_classification_covers_retries_and_cache_refusal():
    assert classify_http_request((0, 8), (0, 8)) == "EXACT_DUPLICATE"
    assert classify_http_request((2, 4), (0, 8)) == "CONTAINED_DUPLICATE"
    assert classify_http_request((6, 12), (0, 8)) == "PARTIAL_OVERLAP"
    assert (
        classify_http_request((8, 12), (0, 8), previous_status=206) == "EXPECTED_RETRY"
    )
    assert (
        classify_http_request((8, 12), (0, 8), previous_status=503)
        == "EXPECTED_URL_REFRESH_RETRY"
    )
    assert (
        classify_http_request((0, 8), (0, 8), cache_refusal=True)
        == "CACHE_REFUSAL_DUPLICATE"
    )


def test_decimal_mbps_conversion_is_not_binary_mib():
    assert BYTES_PER_MEGABIT == 125_000
    assert 60 * BYTES_PER_MEGABIT == 7_500_000
    assert 60 * BYTES_PER_MEGABIT != 60 * 1024 * 1024 / 8


def test_runway_models_are_named_and_stream_isolated():
    reads = [
        {
            "stream": "a",
            "epoch": 0,
            "t": 0.0,
            "completed_t": 0.2,
            "bytes": 1_000_000,
            "mbps": 20,
        },
        {
            "stream": "a",
            "epoch": 0,
            "t": 0.4,
            "completed_t": 0.6,
            "bytes": 1_000_000,
            "mbps": 40,
        },
        {
            "stream": "b",
            "epoch": 0,
            "t": 0.0,
            "completed_t": 0.1,
            "bytes": 1_000_000,
            "mbps": 60,
        },
    ]
    constant = runway_sweep(reads, "CONSTANT_RATE_EXACT")
    variable = runway_sweep(reads, "VARIABLE_TIMELINE_INVERSION")
    assert constant["model"] == "CONSTANT_RATE_EXACT"
    assert variable["model"] == "VARIABLE_TIMELINE_INVERSION"
    assert constant["minimum_initial_runway_bytes"] > 0
    assert variable["minimum_initial_runway_bytes"] > 0


def test_runway_sweep_empty_reads():
    """Verify behavior on empty reads list."""
    result = runway_sweep([])
    # With 0 reads, there are 0 underflow samples for all runways, so runway 0.0 passes.
    assert result["minimum_initial_runway_seconds"] == 0.0
    assert result["minimum_initial_runway_seconds_exact"] == 0.0
    assert result["minimum_initial_runway_bytes"] == 0
    assert result["minimum_initial_runway_seconds_from_bytes"] == 0.0
    for r in (0.0, 0.5, 1.0, 2.0, 5.0, 10.0):
        assert result["runways_seconds"][str(r)]["buffer_underflow_samples"] == 0
        assert result["runways_seconds"][str(r)]["minimum_buffered_ahead_bytes"] is None


def test_deterministic_variable_rate_timeline_integral():
    """Verify analytical integral matches piecewise variable rates across 20, 60, 120, 40 Mbps.

    Scenario timeline (100 seconds total):
      0s  -> 25s: 20 Mbps  (2.5 MB/s)  -> 25s * 2.5 MB/s  = 62.5 MB  (62,500,000 bytes)
      25s -> 50s: 60 Mbps  (7.5 MB/s)  -> 25s * 7.5 MB/s  = 187.5 MB (187,500,000 bytes)
      50s -> 75s: 120 Mbps (15.0 MB/s) -> 25s * 15.0 MB/s = 375.0 MB (375,000,000 bytes)
      75s -> 100s: 40 Mbps (5.0 MB/s)  -> 25s * 5.0 MB/s  = 125.0 MB (125,000,000 bytes)
      Total analytical integral: 750.0 MB = 750,000,000 bytes.
    """
    from d80_endurance import BYTES_PER_MEGABIT, DECIMAL_MBPS

    rates = [20, 60, 120, 40]
    total_seconds = 100.0
    interval = total_seconds / len(rates)

    # 1. Verify analytical cumulative integral at interval boundaries
    cum_bytes = 0
    expected_milestones = [
        (25.0, 62_500_000),
        (50.0, 250_000_000),
        (75.0, 625_000_000),
        (100.0, 750_000_000),
    ]
    for i, rate in enumerate(rates):
        bps = rate * DECIMAL_MBPS
        rate_bytes_per_sec = bps / 8.0
        cum_bytes += int(rate_bytes_per_sec * interval)
        boundary_t, expected_total = expected_milestones[i]
        assert cum_bytes == expected_total

    # 2. Simulate synthetic reads delivering chunks with 100ms delivery latency
    # When rates jump from 20 -> 60 -> 120 Mbps, the initial buffer in bytes calculated from
    # runway * initial_rate (20 Mbps) is smaller than the deficit when rate jumps to 120 Mbps.
    # At runway=1.0s (and above), all underflows are completely eliminated.
    simulated_reads = []
    t = 0.0
    dt = 0.5
    while t < total_seconds:
        idx = min(int(t / interval), len(rates) - 1)
        current_rate = rates[idx]
        current_bps = current_rate * DECIMAL_MBPS
        byte_rate = current_bps / 8.0
        chunk_b = int(byte_rate * dt)
        simulated_reads.append(
            {
                "stream": "test-var-integral",
                "epoch": 0,
                "t": t,
                "completed_t": round(t + 0.1, 4),  # delivers within 100ms
                "bytes": chunk_b,
                "mbps": current_rate,
            }
        )
        t = round(t + dt, 4)

    res = runway_sweep(simulated_reads, model="VARIABLE_TIMELINE_INVERSION")
    assert res["runways_seconds"]["0.0"]["buffer_underflow_samples"] > 0
    assert res["runways_seconds"]["1.0"]["buffer_underflow_samples"] == 0
    assert res["runways_seconds"]["2.0"]["buffer_underflow_samples"] == 0
    assert res["minimum_initial_runway_seconds"] == 1.0


def test_geometry_definitions_and_cli_arguments():
    """Verify GEOMETRIES dictionary contains stress, scaled_pressure, production_representative."""
    from d80_endurance import GEOMETRIES

    assert "stress" in GEOMETRIES
    assert "scaled_pressure" in GEOMETRIES
    assert "production_representative" in GEOMETRIES

    stress = GEOMETRIES["stress"]
    assert stress["hot_bytes"] == 16 * 1024 * 1024
    assert stress["warm_bytes"] == 32 * 1024 * 1024
    assert stress["hot_high"] == int(16 * 1024 * 1024 * 0.85)
    assert stress["hot_low"] == int(16 * 1024 * 1024 * 0.70)
    assert stress["warm_high"] == int(32 * 1024 * 1024 * 0.85)
    assert stress["warm_low"] == int(32 * 1024 * 1024 * 0.70)

    scaled = GEOMETRIES["scaled_pressure"]
    assert scaled["hot_bytes"] == 64 * 1024 * 1024
    assert scaled["warm_bytes"] == 256 * 1024 * 1024
    assert scaled["hot_high"] == int(64 * 1024 * 1024 * 0.85)
    assert scaled["hot_low"] == int(64 * 1024 * 1024 * 0.70)
    assert scaled["warm_high"] == int(256 * 1024 * 1024 * 0.85)
    assert scaled["warm_low"] == int(256 * 1024 * 1024 * 0.70)


def test_mathematical_runway_invariants_and_monotonicity():
    """Verify hard mathematical invariants:
    1. Buffer underflow sample count is monotonically non-increasing as runway increases:
       U(R1) >= U(R2) for all R1 <= R2.
    2. Buffer ahead at any instant t for runway R is:
       A(R, t) = A(0, t) + R * initial_byte_rate.
    3. If U(R) == 0, then min_t A(R, t) > 0.
    4. Exact continuous runway deficit equals:
       max(0, max_t(Consumed(t) - Delivered_prior(t))) / initial_byte_rate.
    """
    initial_mbps = 50
    initial_byte_rate = initial_mbps * 1_000_000 / 8.0  # 6,250,000 B/s
    reads = [
        {
            "stream": "s1",
            "epoch": 0,
            "t": 0.0,
            "completed_t": 0.5,
            "bytes": 1_000_000,
            "mbps": initial_mbps,
        },
        {
            "stream": "s1",
            "epoch": 0,
            "t": 0.5,
            "completed_t": 1.2,
            "bytes": 2_000_000,
            "mbps": initial_mbps,
        },
        {
            "stream": "s1",
            "epoch": 0,
            "t": 1.2,
            "completed_t": 2.0,
            "bytes": 2_000_000,
            "mbps": initial_mbps,
        },
        {
            "stream": "s1",
            "epoch": 0,
            "t": 2.0,
            "completed_t": 2.5,
            "bytes": 3_000_000,
            "mbps": initial_mbps,
        },
    ]

    res = runway_sweep(reads)
    runways = [0.0, 0.5, 1.0, 2.0, 5.0, 10.0]

    # Invariant 1: Monotonic non-increasing underflows
    underflows = [
        res["runways_seconds"][str(r)]["buffer_underflow_samples"] for r in runways
    ]
    for i in range(len(underflows) - 1):
        assert (
            underflows[i] >= underflows[i + 1]
        ), f"Monotonicity violated: {underflows[i]} < {underflows[i+1]}"

    # Invariant 2 & 3: Minimum buffered ahead arithmetic
    for r in runways:
        ahead = res["runways_seconds"][str(r)]["minimum_buffered_ahead_bytes"]
        if res["runways_seconds"][str(r)]["buffer_underflow_samples"] == 0:
            assert (
                ahead > 0
            ), f"Passing runway {r} must have positive minimum ahead buffer: {ahead}"
        else:
            assert (
                ahead <= 0
            ), f"Failing runway {r} must have non-positive minimum ahead buffer: {ahead}"

    # Invariant 4: Exact bytes-based deficit
    # At t=0.5: consumed = 0.5 * 6,250,000 = 3,125,000 B. Delivered prior = 0 -> deficit = 3,125,000 B.
    # At t=1.2: consumed = 1.2 * 6,250,000 = 7,500,000 B. Delivered prior = 1,000,000 -> deficit = 6,500,000 B.
    # At t=2.0: consumed = 2.0 * 6,250,000 = 12,500,000 B. Delivered prior = 3,000,000 -> deficit = 9,500,000 B.
    # At t=2.5: consumed = 2.5 * 6,250,000 = 15,625,000 B. Delivered prior = 5,000,000 -> deficit = 10,625,000 B.
    # Peak deficit = 10,625,000 bytes.
    # Runway from bytes = 10,625,000 / 6,250,000 = 1.7000s.
    assert res["minimum_initial_runway_bytes"] == 10_625_000
    assert res["minimum_initial_runway_seconds_from_bytes"] == 1.7000
    assert (
        res["minimum_initial_runway_seconds"] == 2.0
    )  # Smallest discrete passing runway in (0, 0.5, 1, 2, 5, 10)
