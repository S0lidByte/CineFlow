"""CORS middleware configuration helpers."""

from typing import Any


def build_cors_config(origins: list[str] | None) -> dict[str, Any]:
    """Build Starlette CORSMiddleware kwargs from configured origins.

    Wildcard ``*`` is incompatible with credentialed CORS; when origins are
    empty or include ``*``, credentials are disabled and origins become ``*``.
    Explicit origin lists keep credentials enabled.
    """

    normalized = [o.strip() for o in (origins or []) if o and o.strip()]
    if not normalized or "*" in normalized:
        return {
            "allow_origins": ["*"],
            "allow_credentials": False,
            "allow_methods": ["*"],
            "allow_headers": ["*"],
        }

    return {
        "allow_origins": normalized,
        "allow_credentials": True,
        "allow_methods": ["*"],
        "allow_headers": ["*"],
    }
