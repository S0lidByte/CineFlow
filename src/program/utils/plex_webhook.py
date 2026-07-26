"""Plex webhook helpers: GUID sanitization for dry-run scrobble logging."""

from __future__ import annotations

from typing import Any, cast

# External provider GUIDs only — never log plex:// rating keys or tokens.
_ALLOWED_GUID_PREFIXES = ("imdb://", "tmdb://", "tvdb://")


def sanitize_plex_guids(metadata: dict[str, Any] | None) -> list[str]:
    """Return sorted unique imdb/tmdb/tvdb GUIDs from a Plex Metadata object.

    Accepts both ``Guid`` (list of ``{id: ...}``) and a lone ``guid`` string.
    Non-provider GUIDs (e.g. ``plex://movie/...``) are dropped.
    """

    if not metadata:
        return []

    collected: set[str] = set()
    raw_list = cast(object, metadata.get("Guid") or metadata.get("guid"))
    candidates: list[str] = []

    if isinstance(raw_list, list):
        for entry_obj in cast(list[object], raw_list):
            if isinstance(entry_obj, dict):
                entry = cast(dict[str, object], entry_obj)
                guid_val = entry.get("id")
                if isinstance(guid_val, str):
                    candidates.append(guid_val.strip())
            elif isinstance(entry_obj, str):
                candidates.append(entry_obj.strip())
    elif isinstance(raw_list, str):
        candidates.append(raw_list.strip())

    for guid in candidates:
        if not guid:
            continue
        lower = guid.lower()
        if any(lower.startswith(prefix) for prefix in _ALLOWED_GUID_PREFIXES):
            scheme, _, rest = guid.partition("://")
            if rest:
                collected.add(f"{scheme.lower()}://{rest}")

    return sorted(collected)
