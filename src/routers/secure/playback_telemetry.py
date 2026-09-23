"""Playback telemetry and real-time observability API routes."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from program.services.streaming.telemetry import playback_telemetry_collector
from schemas.playback_telemetry import PlaybackTelemetrySnapshot

router = APIRouter(
    prefix="/telemetry/playback",
    tags=["telemetry"],
    responses={404: {"description": "Not found"}},
)


@router.get(
    "/metrics",
    operation_id="get_playback_telemetry_snapshot",
    response_model=PlaybackTelemetrySnapshot,
)
async def get_playback_telemetry_snapshot() -> PlaybackTelemetrySnapshot:
    """Get an instantaneous snapshot of active streams, aggregate stats, and recent events."""
    return playback_telemetry_collector.get_snapshot()


@router.get(
    "/live",
    operation_id="stream_playback_telemetry",
)
async def stream_playback_telemetry() -> StreamingResponse:
    """Stream real-time playback telemetry snapshots at 1Hz via Server-Sent Events (SSE)."""

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            while True:
                snapshot = playback_telemetry_collector.get_snapshot()
                # model_dump_json serializes datetimes to ISO 8601 strings
                payload = snapshot.model_dump_json()
                yield f"data: {payload}\n\n"
                await asyncio.sleep(1.0)
        except (asyncio.CancelledError, GeneratorExit):
            pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
