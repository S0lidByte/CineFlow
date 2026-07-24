"""HLS ffmpeg query param allowlisting."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from program.utils.hls_params import scale_filter_for_resolution, validate_hls_params


def test_validate_hls_params_accepts_none():
    assert validate_hls_params() == (None, None, None, None)


def test_validate_hls_params_accepts_safe_values():
    assert validate_hls_params(
        pix_fmt="yuv420p",
        video_profile="high",
        level="4.1",
        resolution="1920x1080",
    ) == ("yuv420p", "high", "4.1", "1920x1080")


def test_validate_hls_params_rejects_filter_injection():
    with pytest.raises(HTTPException) as exc:
        validate_hls_params(resolution="foo][x=1")
    assert exc.value.status_code == 400


def test_validate_hls_params_rejects_bad_pix_fmt():
    with pytest.raises(HTTPException):
        validate_hls_params(pix_fmt="evil;rm")


def test_scale_filter_for_resolution():
    assert scale_filter_for_resolution("1920x1080") == "scale=1920:1080"
    assert scale_filter_for_resolution("720") == "scale=-2:720"
