"""Plex webhook helpers: GUID sanitization and Trakt history payload mapping."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, cast

# External provider GUIDs only — never log plex:// rating keys or tokens.
_ALLOWED_GUID_PREFIXES = ("imdb://", "tmdb://", "tvdb://")

MediaKind = Literal["movie", "episode", "unknown"]


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


def parse_provider_ids(guids: list[str]) -> dict[str, str | int]:
    """Map sanitized GUID strings to a Trakt ``ids`` object (external keys only)."""

    ids: dict[str, str | int] = {}
    for guid in guids:
        scheme, _, rest = guid.partition("://")
        if not rest:
            continue
        key = scheme.lower()
        if key == "imdb":
            ids["imdb"] = rest
        elif key in ("tmdb", "tvdb"):
            try:
                ids[key] = int(rest)
            except ValueError:
                continue
    return ids


def plex_media_kind(metadata: dict[str, Any] | None) -> MediaKind:
    """Classify Plex Metadata ``type`` into movie / episode / unknown.

    Prefers ``Metadata.type`` over ``librarySectionType`` so show-library
    episode scrobbles map correctly and show/season payloads are ignored.
    """

    if not metadata:
        return "unknown"
    type_only = str(metadata.get("type") or "").strip().lower()
    if type_only in ("movie", "movies"):
        return "movie"
    if type_only == "episode":
        return "episode"
    if type_only in ("show", "season"):
        return "unknown"
    section = str(metadata.get("librarySectionType") or "").strip().lower()
    if section in ("movie", "movies"):
        return "movie"
    return "unknown"


def history_idempotency_key(
    *,
    media_kind: MediaKind,
    guids: list[str],
    metadata: dict[str, Any] | None,
) -> str:
    """Stable key for deduping repeated Plex scrobble deliveries."""

    rating_key = ""
    if metadata:
        rk = metadata.get("ratingKey")
        if rk is not None:
            rating_key = str(rk)
    guid_part = ",".join(guids) if guids else "no-guid"
    return f"{media_kind}|{rating_key}|{guid_part}"


def build_trakt_history_payload(
    metadata: dict[str, Any] | None,
    guids: list[str],
    *,
    watched_at: str | None = None,
) -> dict[str, Any] | None:
    """Build a ``POST /sync/history`` body from Plex Metadata + sanitized GUIDs.

    Uses external IDs directly (Trakt resolves them). Movies drop ``tvdb`` (not in
    Trakt movie ids). Episodes prefer episode-level ids; if only show-level ids are
    present with season/episode numbers, nests under ``shows``.
    """

    kind = plex_media_kind(metadata)
    ids = parse_provider_ids(guids)
    if not ids or kind == "unknown":
        return None

    ts = watched_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    if kind == "movie":
        movie_ids = {k: v for k, v in ids.items() if k in ("imdb", "tmdb")}
        if not movie_ids:
            return None
        return {"movies": [{"watched_at": ts, "ids": movie_ids}]}

    # episode — require episode-level provider ids in Guid (standard Plex payload).
    episode_ids = {k: v for k, v in ids.items() if k in ("imdb", "tmdb", "tvdb")}
    if not episode_ids:
        return None
    return {"episodes": [{"watched_at": ts, "ids": episode_ids}]}
