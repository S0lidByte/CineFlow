"""Helpers for normalizing media runtime metadata (minutes)."""

from __future__ import annotations

import math
from typing import Any


def coerce_runtime_minutes(value: Any) -> float | None:
    """Return a positive finite runtime in minutes, or None if unusable."""

    if value is None:
        return None

    try:
        runtime = float(value)
    except (TypeError, ValueError):
        return None

    if not math.isfinite(runtime) or runtime <= 0:
        return None

    return runtime
