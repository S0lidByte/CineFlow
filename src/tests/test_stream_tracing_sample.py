"""Tests for STREAM tracing hot-path sampling."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from program.services.streaming.media_stream import (
    MediaStream,
    should_emit_hot_stream_trace,
)


def test_should_emit_hot_stream_trace_every_one_logs_all():
    assert all(should_emit_hot_stream_trace(i, 1) for i in range(1, 20))


def test_should_emit_hot_stream_trace_samples_every_n():
    every = 50
    emitted = [i for i in range(1, 201) if should_emit_hot_stream_trace(i, every)]
    assert emitted == [1, 51, 101, 151]


def test_should_emit_hot_stream_trace_rejects_non_positive_counter():
    assert should_emit_hot_stream_trace(0, 50) is False


def test_trace_stream_samples_hot_events():
    stream = MediaStream.__new__(MediaStream)
    stream.enable_tracing = True
    stream._hot_trace_counter = 0
    stream.build_log_message = lambda msg: msg  # type: ignore[method-assign]

    with (
        patch(
            "program.services.streaming.media_stream.settings_manager"
        ) as mock_settings,
        patch("program.services.streaming.media_stream.logger") as mock_logger,
    ):
        mock_settings.settings.stream_tracing_sample_every = 50

        for _ in range(50):
            stream._trace_stream("hot event", hot=True)

        assert mock_logger.log.call_count == 1
        mock_logger.log.assert_called_with("STREAM", "hot event")

        stream._trace_stream("hot event", hot=True)
        assert mock_logger.log.call_count == 2


def test_trace_stream_always_logs_cold_events_when_tracing_on():
    stream = MediaStream.__new__(MediaStream)
    stream.enable_tracing = True
    stream._hot_trace_counter = 0
    stream.build_log_message = lambda msg: msg  # type: ignore[method-assign]

    with (
        patch(
            "program.services.streaming.media_stream.settings_manager"
        ) as mock_settings,
        patch("program.services.streaming.media_stream.logger") as mock_logger,
    ):
        mock_settings.settings.stream_tracing_sample_every = 50

        for i in range(5):
            stream._trace_stream(f"lifecycle {i}", hot=False)

        assert mock_logger.log.call_count == 5
        assert stream._hot_trace_counter == 0


def test_trace_stream_noop_when_tracing_disabled():
    stream = MediaStream.__new__(MediaStream)
    stream.enable_tracing = False
    stream._hot_trace_counter = 0
    stream.build_log_message = MagicMock()

    with patch("program.services.streaming.media_stream.logger") as mock_logger:
        stream._trace_stream("should not log", hot=True)
        mock_logger.log.assert_not_called()
        assert stream._hot_trace_counter == 0
