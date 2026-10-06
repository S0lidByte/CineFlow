"""Canonical settings mutation pipeline.

Shared by:
- POST /api/v1/settings/set/all
- POST /api/v1/settings/autotune/apply
"""

from __future__ import annotations

from typing import Any, cast

from program.settings import settings_manager
from program.settings.models import AppModel
from program.settings.ranking_patterns import validate_ranking_payload_patterns

API_KEY_SENTINEL = "********"


def _validate_ranking_in_settings_dict(settings_dict: dict[str, Any]) -> None:
    ranking = settings_dict.get("ranking")
    if isinstance(ranking, dict):
        try:
            validate_ranking_payload_patterns(cast(dict[str, Any], ranking))
        except ValueError as exc:
            raise ValueError(str(exc)) from exc


def deep_merge_settings(current_obj: dict[str, Any], new_obj: dict[str, Any]) -> None:
    """Recursively merge new_obj into current_obj in place."""
    for key, value in new_obj.items():
        if (
            isinstance(value, dict)
            and key in current_obj
            and isinstance(current_obj[key], dict)
        ):
            deep_merge_settings(current_obj[key], cast(dict[str, Any], value))
        else:
            current_obj[key] = value


def apply_canonical_settings_mutation(new_settings: dict[str, Any]) -> AppModel:
    """
    Authoritative settings mutation pipeline for CineFlow.

    Performs:
    1. API key sentinel preservation and validation
    2. Deep nested merge into current settings dictionary
    3. Ranking payload pattern validation
    4. Pydantic validation via AppModel
    5. SettingsManager.load() (diffing top-level keys & observer notification)
    6. SettingsManager.save() (persistence to disk)

    Raises:
        ValueError: If validation fails (e.g. invalid api_key, ranking, or Pydantic fields).
    """
    mutated_payload = dict(new_settings)

    if "api_key" in mutated_payload:
        if mutated_payload["api_key"] == API_KEY_SENTINEL:
            mutated_payload["api_key"] = settings_manager.settings.api_key
        elif (
            not isinstance(mutated_payload["api_key"], str)
            or not mutated_payload["api_key"].strip()
        ):
            raise ValueError("api_key cannot be empty or whitespace")

    current_settings = settings_manager.settings.model_dump()
    deep_merge_settings(current_settings, mutated_payload)
    _validate_ranking_in_settings_dict(current_settings)

    # Validate and save the updated settings
    try:
        updated_settings = settings_manager.settings.model_validate(current_settings)
        settings_manager.load(settings_dict=updated_settings.model_dump())
        settings_manager.save()

        # Preserve mount-derived effective capacity: configured MB values are not
        # necessarily safe runtime limits (tmpfs and free-space clamps apply).
        from kink import di

        from program.services.streaming.cache import Cache

        if Cache in di:
            fs = updated_settings.filesystem
            di[Cache].update_watermarks(
                hot_watermark_high_pct=fs.hot_cache_watermark_high_pct,
                hot_watermark_low_pct=fs.hot_cache_watermark_low_pct,
                warm_watermark_high_pct=fs.warm_cache_watermark_high_pct,
                warm_watermark_low_pct=fs.warm_cache_watermark_low_pct,
                warm_min_free_mb=fs.warm_cache_min_free_mb,
            )

        return updated_settings
    except Exception as e:
        raise ValueError(str(e)) from e
