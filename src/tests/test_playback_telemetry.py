"""Unit tests for CineFlow Playback Telemetry & Observability HUD."""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

from program.services.streaming.telemetry import (
    PlaybackTelemetryCollector,
    StreamSessionTracker,
)
from schemas.playback_telemetry import (
    AggregatePlaybackMetrics,
    PlaybackTelemetryEvent,
    PlaybackTelemetrySnapshot,
    StreamSessionMetric,
)


def test_stream_session_tracker_reads_and_throughput():
    tracker = StreamSessionTracker(
        stream_id="test_stream_1",
        title="Dune 2 (2024)",
        media_item_id=42,
        client_ip="192.168.1.100",
        client_user_agent="PlexMediaPlayer/1.0",
        provider="realdebrid",
    )

    assert tracker.is_active is True
    assert tracker.bytes_transferred == 0
    assert tracker.bytes_from_cache == 0

    # Record 10 MB read from network
    ten_mb = 10 * 1024 * 1024
    tracker.record_read(ten_mb, from_cache=False)

    assert tracker.bytes_transferred == ten_mb
    assert tracker.bytes_from_cache == 0

    # Record 5 MB read from cache
    five_mb = 5 * 1024 * 1024
    tracker.record_read(five_mb, from_cache=True)

    assert tracker.bytes_transferred == 15 * 1024 * 1024
    assert tracker.bytes_from_cache == five_mb

    metric = tracker.to_metric()
    assert isinstance(metric, StreamSessionMetric)
    assert metric.title == "Dune 2 (2024)"
    assert metric.media_item_id == 42
    assert metric.cache_hit_rate_pct == 33.3  # (5 / 15) * 100
    assert metric.current_throughput_mbps > 0.0
    assert metric.is_active is True

    tracker.mark_cdn_refreshed()
    assert tracker.to_metric().cdn_url_refreshed is True

    tracker.mark_completed()
    assert tracker.to_metric().is_active is False


def test_playback_telemetry_collector_lifecycle_and_ring_buffer():
    collector = PlaybackTelemetryCollector()
    collector._sessions.clear()
    collector._events.clear()
    collector._total_streams_count = 0
    collector._total_bytes_transferred = 0
    collector._total_cache_hits = 0
    collector._total_cache_misses = 0
    collector._cdn_refresh_count = 0
    collector._stream_error_count = 0

    # 1. Register stream start
    collector.register_stream_start(
        stream_id="stream_abc",
        title="Oppenheimer (2023)",
        media_item_id=101,
        client_ip="127.0.0.1",
        client_user_agent="VLC/3.0.18",
        provider="alldebrid",
    )

    snapshot1 = collector.get_snapshot()
    assert isinstance(snapshot1, PlaybackTelemetrySnapshot)
    assert snapshot1.aggregate.active_streams_count == 1
    assert snapshot1.aggregate.total_streams_count == 1
    assert len(snapshot1.active_streams) == 1
    assert snapshot1.active_streams[0].title == "Oppenheimer (2023)"
    assert len(snapshot1.recent_events) == 1
    assert snapshot1.recent_events[0].event_type == "STREAM_START"

    # 2. Record reads and cache hits
    collector.record_stream_read(
        stream_id="stream_abc",
        nbytes=20 * 1024 * 1024,
        from_cache=False,
    )
    collector.record_cache_hit(
        stream_id="stream_abc",
        title="Oppenheimer (2023)",
        nbytes=4 * 1024 * 1024,
    )
    collector.record_cache_miss(
        stream_id="stream_abc",
        title="Oppenheimer (2023)",
        nbytes=4 * 1024 * 1024,
    )

    # 3. Record CDN refresh
    collector.record_cdn_refresh(
        stream_id="stream_abc",
        title="Oppenheimer (2023)",
        details={"reason": "Link expired"},
    )

    # 4. Record error
    collector.record_stream_error(
        stream_id="stream_abc",
        title="Oppenheimer (2023)",
        error_msg="Connection reset by peer",
    )

    # 5. Register completion
    collector.register_stream_complete(
        stream_id="stream_abc",
        title="Oppenheimer (2023)",
    )

    snapshot2 = collector.get_snapshot()
    assert snapshot2.aggregate.active_streams_count == 0
    assert snapshot2.aggregate.total_streams_count == 1
    assert snapshot2.aggregate.total_cache_hits == 1
    assert snapshot2.aggregate.total_cache_misses == 1
    assert snapshot2.aggregate.total_chunk_requests == 2
    assert snapshot2.aggregate.aggregate_cache_hit_rate_pct == 50.0
    assert snapshot2.aggregate.cdn_refresh_count == 1
    assert snapshot2.aggregate.stream_error_count == 1

    # Events in reverse chronological order:
    event_types = [ev.event_type for ev in snapshot2.recent_events]
    assert event_types == [
        "STREAM_COMPLETE",
        "STREAM_ERROR",
        "CDN_REFRESH",
        "CACHE_MISS",
        "CACHE_HIT",
        "STREAM_START",
    ]


def test_playback_telemetry_redaction():
    collector = PlaybackTelemetryCollector()
    collector._sessions.clear()
    collector._events.clear()

    # Pass sensitive keys in event details
    collector.register_stream_start(
        stream_id="stream_sensitive",
        title="Secret Movie",
        provider="realdebrid",
    )
    collector.record_cdn_refresh(
        stream_id="stream_sensitive",
        title="Secret Movie",
        details={
            "api_key": "supersecretapikey1234567890123456",
            "token": "rd_token_abc_xyz",
            "url": "https://download.real-debrid.com/d/movie.mkv?token=secrettoken12345",
        },
    )

    snapshot = collector.get_snapshot()
    cdn_refresh_event = next(
        ev for ev in snapshot.recent_events if ev.event_type == "CDN_REFRESH"
    )
    assert cdn_refresh_event.details["api_key"] == "[REDACTED]"
    assert cdn_refresh_event.details["token"] == "[REDACTED]"
    assert "secrettoken12345" not in cdn_refresh_event.details["url"]
    assert "[REDACTED]" in cdn_refresh_event.details["url"]
