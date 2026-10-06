"""Tests for FilesystemModel cache watermark validation and canonical settings mutation."""

import pytest
from pydantic import ValidationError

from program.settings import settings_manager
from program.settings.models import FilesystemModel
from program.settings.mutation import apply_canonical_settings_mutation


def test_filesystem_model_default_watermarks():
    fs = FilesystemModel()
    assert fs.autotune_mode == "disabled"
    assert fs.hot_cache_reserve_pct == 10.0
    assert fs.hot_cache_watermark_high_pct == 85.0
    assert fs.hot_cache_watermark_low_pct == 70.0
    assert fs.warm_cache_reserve_pct == 10.0
    assert fs.warm_cache_watermark_high_pct == 85.0
    assert fs.warm_cache_watermark_low_pct == 70.0
    assert fs.warm_cache_min_free_mb == 1024


def test_filesystem_model_valid_custom_watermarks():
    fs = FilesystemModel(
        hot_cache_reserve_pct=15.0,
        hot_cache_watermark_high_pct=80.0,
        hot_cache_watermark_low_pct=60.0,
        warm_cache_reserve_pct=5.0,
        warm_cache_watermark_high_pct=90.0,
        warm_cache_watermark_low_pct=75.0,
    )
    assert fs.hot_cache_reserve_pct == 15.0
    assert fs.hot_cache_watermark_high_pct == 80.0
    assert fs.hot_cache_watermark_low_pct == 60.0
    assert fs.warm_cache_reserve_pct == 5.0
    assert fs.warm_cache_watermark_high_pct == 90.0
    assert fs.warm_cache_watermark_low_pct == 75.0


def test_filesystem_model_watermark_low_greater_or_equal_high_fails():
    with pytest.raises(ValidationError) as excinfo:
        FilesystemModel(
            hot_cache_watermark_high_pct=70.0,
            hot_cache_watermark_low_pct=75.0,
        )
    assert "must be less than" in str(excinfo.value)

    with pytest.raises(ValidationError) as excinfo:
        FilesystemModel(
            warm_cache_watermark_high_pct=70.0,
            warm_cache_watermark_low_pct=70.0,
        )
    assert "must be less than" in str(excinfo.value)


def test_filesystem_model_watermark_gap_too_narrow_fails():
    with pytest.raises(ValidationError) as excinfo:
        FilesystemModel(
            hot_cache_watermark_high_pct=80.0,
            hot_cache_watermark_low_pct=77.0,  # gap is 3.0 < 5.0
        )
    assert "watermark gap must be at least 5%" in str(excinfo.value)

    with pytest.raises(ValidationError) as excinfo:
        FilesystemModel(
            warm_cache_watermark_high_pct=80.0,
            warm_cache_watermark_low_pct=76.0,  # gap is 4.0 < 5.0
        )
    assert "watermark gap must be at least 5%" in str(excinfo.value)


def test_filesystem_model_watermark_violates_reserve_fails():
    # 100 - 20 = 80 max high watermark, but setting 85
    with pytest.raises(ValidationError) as excinfo:
        FilesystemModel(
            hot_cache_reserve_pct=20.0,
            hot_cache_watermark_high_pct=85.0,
            hot_cache_watermark_low_pct=60.0,
        )
    assert "exceeds maximum allowed threshold with reserve" in str(excinfo.value)

    with pytest.raises(ValidationError) as excinfo:
        FilesystemModel(
            warm_cache_reserve_pct=25.0,
            warm_cache_watermark_high_pct=80.0,  # max is 75
            warm_cache_watermark_low_pct=60.0,
        )
    assert "exceeds maximum allowed threshold with reserve" in str(excinfo.value)


def test_canonical_mutation_applies_watermark_settings():
    original_mode = settings_manager.settings.filesystem.autotune_mode
    try:
        updated = apply_canonical_settings_mutation(
            {
                "filesystem": {
                    "autotune_mode": "balanced",
                    "hot_cache_reserve_pct": 12.0,
                    "hot_cache_watermark_high_pct": 82.0,
                    "hot_cache_watermark_low_pct": 65.0,
                }
            }
        )
        assert updated.filesystem.autotune_mode == "balanced"
        assert updated.filesystem.hot_cache_reserve_pct == 12.0
        assert updated.filesystem.hot_cache_watermark_high_pct == 82.0
        assert updated.filesystem.hot_cache_watermark_low_pct == 65.0
        assert settings_manager.settings.filesystem.autotune_mode == "balanced"
    finally:
        # Restore
        apply_canonical_settings_mutation(
            {
                "filesystem": {
                    "autotune_mode": original_mode,
                    "hot_cache_reserve_pct": 10.0,
                    "hot_cache_watermark_high_pct": 85.0,
                    "hot_cache_watermark_low_pct": 70.0,
                }
            }
        )
