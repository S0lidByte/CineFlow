"""Compatibility tests for StreamModel read-tolerance settings pass-through."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from program.services.streaming.config import Config
from program.settings.models import StreamModel


def test_stream_model_tolerance_defaults_match_config() -> None:
    model = StreamModel()
    assert model.sequential_read_tolerance_blocks == 10
    assert model.scan_tolerance_blocks == 25

    config = Config(
        chunk_size=model.chunk_size_mb * 1024 * 1024,
        activity_timeout_seconds=model.activity_timeout_seconds,
        chunk_wait_timeout_seconds=model.chunk_wait_timeout_seconds,
        connect_timeout_seconds=model.connect_timeout_seconds,
        sequential_read_tolerance_blocks=model.sequential_read_tolerance_blocks,
        scan_tolerance_blocks=model.scan_tolerance_blocks,
    )
    assert config.sequential_read_tolerance == 10 * 128 * 1024
    assert config.scan_tolerance == 25 * 128 * 1024


def test_stream_model_accepts_legacy_payload_without_tolerance_keys() -> None:
    """Existing settings.json without the new fields must keep historic defaults."""

    model = StreamModel.model_validate(
        {
            "chunk_size_mb": 1,
            "connect_timeout_seconds": 10,
            "chunk_wait_timeout_seconds": 10,
            "activity_timeout_seconds": 60,
        }
    )
    assert model.sequential_read_tolerance_blocks == 10
    assert model.scan_tolerance_blocks == 25


def test_stream_model_rejects_out_of_bounds_tolerances() -> None:
    with pytest.raises(ValidationError):
        StreamModel(sequential_read_tolerance_blocks=0)

    with pytest.raises(ValidationError):
        StreamModel(scan_tolerance_blocks=1001)


def test_stream_model_custom_tolerances_compute_expected_bytes() -> None:
    model = StreamModel(
        sequential_read_tolerance_blocks=5,
        scan_tolerance_blocks=40,
    )
    config = Config(
        chunk_size=model.chunk_size_mb * 1024 * 1024,
        activity_timeout_seconds=model.activity_timeout_seconds,
        chunk_wait_timeout_seconds=model.chunk_wait_timeout_seconds,
        connect_timeout_seconds=model.connect_timeout_seconds,
        sequential_read_tolerance_blocks=model.sequential_read_tolerance_blocks,
        scan_tolerance_blocks=model.scan_tolerance_blocks,
    )
    assert config.sequential_read_tolerance == 5 * 128 * 1024
    assert config.scan_tolerance == 40 * 128 * 1024
