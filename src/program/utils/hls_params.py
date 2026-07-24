"""Validated HLS/ffmpeg query parameters."""

from __future__ import annotations

import re

from fastapi import HTTPException, status

# Common libx264 / ffmpeg pixel formats and profiles used by clients.
_ALLOWED_PIX_FMT = frozenset(
    {
        "yuv420p",
        "yuv422p",
        "yuv444p",
        "yuv420p10le",
        "yuv422p10le",
        "yuv444p10le",
        "nv12",
        "nv21",
    }
)
_ALLOWED_PROFILES = frozenset(
    {
        "baseline",
        "main",
        "high",
        "high10",
        "high422",
        "high444",
    }
)
_RESOLUTION_RE = re.compile(r"^(\d+)(?:x(\d+))?$")
_LEVEL_RE = re.compile(r"^\d(?:\.\d)?$")

FFPROBE_TIMEOUT_SECONDS = 15


def validate_hls_params(
    *,
    pix_fmt: str | None = None,
    video_profile: str | None = None,
    level: str | None = None,
    resolution: str | None = None,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Allowlist HLS transcode query params; raise 400 on invalid input."""

    if pix_fmt is not None:
        if pix_fmt not in _ALLOWED_PIX_FMT:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid pix_fmt: {pix_fmt}",
            )

    if video_profile is not None:
        normalized = video_profile.lower()
        if normalized not in _ALLOWED_PROFILES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid profile: {video_profile}",
            )
        video_profile = normalized

    if level is not None and not _LEVEL_RE.fullmatch(level):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid level: {level}",
        )

    if resolution is not None and not _RESOLUTION_RE.fullmatch(resolution):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid resolution: {resolution}",
        )

    return pix_fmt, video_profile, level, resolution


def scale_filter_for_resolution(resolution: str) -> str:
    """Build a safe ffmpeg scale filter from a validated resolution string."""

    match = _RESOLUTION_RE.fullmatch(resolution)
    if not match:
        raise ValueError(f"resolution was not validated: {resolution}")
    width, height = match.group(1), match.group(2)
    if height is not None:
        return f"scale={width}:{height}"
    return f"scale=-2:{width}"
