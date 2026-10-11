import asyncio
from copy import copy
from typing import Annotated, Any, cast

from fastapi import APIRouter, Body, HTTPException, Path, Query
from kink import di
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from program.services.streaming.cache_autotune import (
    AutoTuneJobSnapshot,
    AutoTuneProfile,
    autotune_job_manager,
)
from program.settings import settings_manager
from program.settings.models import AppModel
from program.settings.mutation import (
    API_KEY_SENTINEL,
    apply_canonical_settings_mutation,
)
from program.settings.ranking_descriptions import enrich_ranking_schema
from program.utils.connection_tests import (
    SUPPORTED_SERVICES,
    ConnectionService,
    ConnectionTestResponse,
    run_connection_test,
)

from ..models.shared import MessageResponse

router = APIRouter(
    prefix="/settings",
    tags=["settings"],
    responses={404: {"description": "Not found"}},
)

_schema_cache: dict[str, Any] | None = None


@router.get(
    "/schema",
    operation_id="get_settings_schema",
    response_model=dict[str, Any],
)
async def get_settings_schema() -> dict[str, Any]:
    """Get the JSON schema for the settings. Cached for faster repeated loads."""
    global _schema_cache
    if _schema_cache is None:
        schema = settings_manager.settings.model_json_schema()
        enrich_ranking_schema(schema)
        _schema_cache = schema
    return _schema_cache


@router.get(
    "/schema/keys",
    operation_id="get_settings_schema_for_keys",
    response_model=dict[str, Any],
)
async def get_settings_schema_for_keys(
    keys: Annotated[
        str,
        Query(
            description="Comma-separated list of top-level keys to get schema for (e.g., 'version,api_key,updaters')",
            min_length=1,
        ),
    ],
    title: Annotated[
        str,
        Query(
            description="Title of the schema",
        ),
    ] = "FilteredSettings",
) -> dict[str, Any]:
    model_fields = AppModel.model_fields
    requested_keys = [k.strip() for k in keys.split(",") if k.strip()]

    if not requested_keys:
        raise HTTPException(
            status_code=400,
            detail="At least one key must be provided",
        )

    valid_keys = set(model_fields.keys())
    invalid_keys = [k for k in requested_keys if k not in valid_keys]
    if invalid_keys:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid keys: {', '.join(invalid_keys)}. Valid keys are: {', '.join(sorted(valid_keys))}",
        )

    all_defs: dict[str, Any] = {}
    properties: dict[str, Any] = {}
    required: list[str] = []

    for key in requested_keys:
        field_info = model_fields[key]
        adapter: TypeAdapter[Any] = TypeAdapter(field_info.annotation)
        field_schema = adapter.json_schema(ref_template="#/$defs/{model}")

        if "$defs" in field_schema:
            all_defs.update(field_schema.pop("$defs"))

        # Inject original field description if it exists
        if field_info.description:
            field_schema["description"] = field_info.description

        properties[key] = field_schema

        if field_info.is_required():
            required.append(key)

    filtered_schema: dict[str, Any] = {
        "properties": properties,
        "required": required,
        "title": title,
        "type": "object",
    }

    if all_defs:
        filtered_schema["$defs"] = all_defs

    if "ranking" in properties:
        enrich_ranking_schema(filtered_schema)

    return filtered_schema


@router.get(
    "/load",
    operation_id="load_settings",
    response_model=MessageResponse,
)
async def load_settings() -> MessageResponse:
    settings_manager.load()

    return MessageResponse(message="Settings loaded!")


@router.post(
    "/save",
    operation_id="save_settings",
    response_model=MessageResponse,
)
async def save_settings() -> MessageResponse:
    settings_manager.save()

    return MessageResponse(message="Settings saved!")


@router.get(
    "/get/all",
    operation_id="get_all_settings",
    response_model=AppModel,
)
async def get_all_settings() -> AppModel:
    masked = copy(settings_manager.settings)
    if masked.api_key:
        masked.api_key = API_KEY_SENTINEL
    return masked


@router.get(
    "/get/{paths}",
    operation_id="get_settings",
    response_model=dict[str, Any],
)
async def get_settings(
    paths: Annotated[
        str,
        Path(
            description="Comma-separated list of settings paths",
            min_length=1,
        ),
    ],
) -> dict[str, Any]:
    current_settings = settings_manager.settings.model_dump()
    if current_settings.get("api_key"):
        current_settings["api_key"] = API_KEY_SENTINEL

    data = dict[str, Any]()

    for path in paths.split(","):
        keys = path.split(".")
        current_obj = current_settings

        for k in keys:
            if k not in current_obj:
                continue

            current_obj = current_obj[k]

        data[path] = current_obj

    return data


@router.post(
    "/set/all",
    operation_id="set_all_settings",
    response_model=MessageResponse,
)
async def set_all_settings(
    new_settings: Annotated[
        dict[str, Any],
        Body(description="New settings to apply"),
    ],
) -> MessageResponse:
    try:
        apply_canonical_settings_mutation(new_settings)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    return MessageResponse(message="All settings updated successfully!")


@router.post(
    "/set/{paths}",
    operation_id="set_settings",
    response_model=MessageResponse,
)
async def set_settings(
    paths: Annotated[
        str,
        Path(
            description="Comma-separated list of settings paths to update",
            min_length=1,
        ),
    ],
    values: Annotated[
        dict[str, Any],
        Body(description="Dictionary mapping paths to their new values"),
    ],
) -> MessageResponse:
    if "api_key" in values:
        if values["api_key"] == API_KEY_SENTINEL:
            values["api_key"] = settings_manager.settings.api_key
        elif not isinstance(values["api_key"], str) or not values["api_key"].strip():
            raise HTTPException(
                status_code=400, detail="api_key cannot be empty or whitespace"
            )

    current_settings = settings_manager.settings.model_dump()
    requested_paths = [p.strip() for p in paths.split(",") if p.strip()]

    missing_values = [p for p in requested_paths if p not in values]
    if missing_values:
        raise HTTPException(
            status_code=400,
            detail=f"Missing values for paths: {', '.join(missing_values)}",
        )

    # Build mutation delta from requested paths while validating structure
    mutation_delta: dict[str, Any] = {}
    for path in requested_paths:
        keys = path.split(".")
        current_obj: Any = current_settings

        # Navigate to the parent object to validate path existence
        for k in keys[:-1]:
            if not isinstance(current_obj, dict):
                raise HTTPException(
                    status_code=400,
                    detail=f"Cannot traverse path '{path}': intermediate value is not an object.",
                )
            if k not in current_obj:
                raise HTTPException(
                    status_code=400,
                    detail=f"Path '{path}' does not exist.",
                )
            current_obj = cast(Any, current_obj[k])

        if not isinstance(current_obj, dict):
            raise HTTPException(
                status_code=400,
                detail=f"Cannot set value at '{path}': parent is not an object.",
            )
        if keys[-1] not in current_obj:
            raise HTTPException(
                status_code=400,
                detail=f"Key '{keys[-1]}' does not exist in path '{'.'.join(keys[:-1]) or 'root'}'.",
            )

        # Place into mutation_delta
        target_delta = mutation_delta
        for k in keys[:-1]:
            target_delta = target_delta.setdefault(k, {})
        target_delta[keys[-1]] = values[path]

    # Preserve active Trakt OAuth tokens if client payload submitted empty strings or omitted them
    trakt_oauth = mutation_delta.get("content", {}).get("trakt", {}).get("oauth")
    if isinstance(trakt_oauth, dict):
        existing_oauth = settings_manager.settings.content.trakt.oauth
        if not trakt_oauth.get("access_token") and existing_oauth.access_token:
            trakt_oauth["access_token"] = existing_oauth.access_token
        if not trakt_oauth.get("refresh_token") and existing_oauth.refresh_token:
            trakt_oauth["refresh_token"] = existing_oauth.refresh_token

    try:
        apply_canonical_settings_mutation(mutation_delta)
    except (ValueError, ValidationError) as e:
        raise HTTPException(
            status_code=400,
            detail=f"Failed to update settings: {str(e)}",
        ) from e

    return MessageResponse(message="Settings updated successfully.")


@router.post(
    "/test-connection/{service}",
    operation_id="test_settings_connection",
    response_model=ConnectionTestResponse,
)
async def test_settings_connection(
    service: Annotated[
        ConnectionService,
        Path(description="Integration to probe (saved settings only)"),
    ],
) -> ConnectionTestResponse:
    """Probe a third-party integration using saved settings.

    Returns ``{ok, latency_ms, message}`` without secrets. The response wait is
    bounded to five seconds; cancellation of an in-flight sync probe is
    cooperative and the worker may finish later.
    """
    if service not in SUPPORTED_SERVICES:
        raise HTTPException(status_code=404, detail="Unknown service")
    return await asyncio.to_thread(run_connection_test, service)


class AutoTuneRunRequest(BaseModel):
    profile: AutoTuneProfile = Field(
        default="balanced", description="Target optimization profile"
    )
    bench_bytes: int = Field(
        default=8 * 1024 * 1024,
        ge=1024 * 1024,
        le=64 * 1024 * 1024,
        description="Benchmark payload size in bytes",
    )


class AutoTuneApplyRequest(BaseModel):
    run_id: str = Field(description="Run ID of the completed auto-tune job")
    force: bool = Field(
        default=False,
        description="Apply even if open VFS media handles are detected",
    )


@router.post(
    "/autotune/run",
    operation_id="run_cache_autotune",
    response_model=AutoTuneJobSnapshot,
)
async def run_cache_autotune(
    request: AutoTuneRunRequest,
) -> AutoTuneJobSnapshot:
    """Start an isolated synthetic cache microbenchmark and optimization job."""
    try:
        job = autotune_job_manager.start_job(
            profile=request.profile,
            bench_bytes=request.bench_bytes,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return job.snapshot()


@router.get(
    "/autotune/status/{run_id}",
    operation_id="get_cache_autotune_status",
    response_model=AutoTuneJobSnapshot,
)
async def get_cache_autotune_status(
    run_id: Annotated[str, Path(description="Auto-tune run ID")],
) -> AutoTuneJobSnapshot:
    """Get the progress status and recommendation of an auto-tune job."""
    snapshot = autotune_job_manager.get_job_snapshot(run_id)
    if not snapshot:
        raise HTTPException(
            status_code=404, detail=f"Auto-tune run '{run_id}' not found"
        )
    return snapshot


@router.post(
    "/autotune/cancel/{run_id}",
    operation_id="cancel_cache_autotune",
    response_model=MessageResponse,
)
async def cancel_cache_autotune(
    run_id: Annotated[str, Path(description="Auto-tune run ID")],
) -> MessageResponse:
    """Cancel a running auto-tune job."""
    cancelled = autotune_job_manager.cancel_job(run_id)
    if not cancelled:
        job = autotune_job_manager.get_job(run_id)
        if not job:
            raise HTTPException(
                status_code=404, detail=f"Auto-tune run '{run_id}' not found"
            )
        return MessageResponse(
            message=f"Auto-tune run '{run_id}' cannot be cancelled (status: {job.status})"
        )
    return MessageResponse(message=f"Auto-tune run '{run_id}' cancelled successfully.")


@router.post(
    "/autotune/apply",
    operation_id="apply_cache_autotune",
    response_model=MessageResponse,
)
async def apply_cache_autotune(
    request: AutoTuneApplyRequest,
) -> MessageResponse:
    """Apply recommendations from a completed auto-tune job to live settings."""
    job = autotune_job_manager.get_job(request.run_id)
    if not job:
        raise HTTPException(
            status_code=404, detail=f"Auto-tune run '{request.run_id}' not found"
        )
    if job.status != "completed" or not job.recommendation:
        raise HTTPException(
            status_code=400,
            detail=f"Auto-tune run '{request.run_id}' has not completed successfully (status: {job.status})",
        )

    # Check VFS media handle safety
    if not request.force:
        try:
            from program.program import Program

            services = di[Program].services if Program in di else None
            vfs = (
                getattr(services.filesystem, "riven_vfs", None)
                if services and hasattr(services, "filesystem")
                else None
            )
            if vfs and vfs.has_open_media_handles():
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Active VFS media handles detected. Applying cache adjustments now "
                        "could interrupt active playback. Pass force=true to override."
                    ),
                )
        except HTTPException:
            raise
        except Exception:
            # If VFS is not initialized or in unit test mode, allow applying
            pass

    rec = job.recommendation
    mutation_payload = {
        "filesystem": {
            "autotune_mode": rec.profile,
            "cache_max_size_mb": rec.recommended_warm_cache_max_mb,
            "tmpfs_cache_max_mb": rec.recommended_hot_cache_max_mb,
            "hot_cache_reserve_pct": rec.recommended_hot_reserve_pct,
            "hot_cache_watermark_high_pct": rec.recommended_hot_watermark_high_pct,
            "hot_cache_watermark_low_pct": rec.recommended_hot_watermark_low_pct,
            "warm_cache_reserve_pct": rec.recommended_warm_reserve_pct,
            "warm_cache_watermark_high_pct": rec.recommended_warm_watermark_high_pct,
            "warm_cache_watermark_low_pct": rec.recommended_warm_watermark_low_pct,
            "warm_cache_min_free_mb": rec.recommended_warm_min_free_mb,
        }
    }

    try:
        apply_canonical_settings_mutation(mutation_payload)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Failed to apply auto-tune settings: {e}",
        )

    return MessageResponse(
        message=f"Auto-tune '{rec.profile}' settings applied successfully!"
    )
