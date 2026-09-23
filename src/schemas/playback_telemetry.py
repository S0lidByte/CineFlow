"""Pydantic models and schemas for CineFlow Playback Telemetry & Observability."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


class StreamSessionMetric(BaseModel):
    """Real-time metric snapshot for an active VFS or HTTP media stream."""

    model_config = ConfigDict(extra="ignore")

    stream_id: str = Field(..., description="Unique identifier for the stream session")
    media_item_id: int | None = Field(
        None, description="Optional media item ID if mapped"
    )
    title: str = Field(..., description="Sanitized media title or file path")
    client_ip: str | None = Field(
        None, description="Client IP address or host if available"
    )
    client_user_agent: str | None = Field(
        None, description="Client user agent string (e.g. Plex, VLC, ExoPlayer)"
    )
    bytes_transferred: int = Field(
        0, ge=0, description="Total bytes streamed to the client in this session"
    )
    bytes_from_cache: int = Field(
        0, ge=0, description="Total bytes served directly from the chunk cache"
    )
    cache_hit_rate_pct: float = Field(
        0.0, ge=0.0, le=100.0, description="Percentage of bytes served from cache"
    )
    current_throughput_mbps: float = Field(
        0.0,
        ge=0.0,
        description="Instantaneous network transfer rate in megabytes per second (MB/s) over rolling window",
    )
    started_at: datetime = Field(
        ..., description="Timestamp when stream session commenced"
    )
    last_read_at: datetime = Field(
        ..., description="Timestamp of the most recent read chunk/request"
    )
    is_active: bool = Field(
        True, description="Whether the stream is actively reading or connected"
    )
    provider: str | None = Field(
        None, description="Debrid / upstream provider name (e.g. realdebrid, alldebrid)"
    )
    cdn_url_refreshed: bool = Field(
        False, description="Whether CDN link refresh was triggered during playback"
    )


class AggregatePlaybackMetrics(BaseModel):
    """System-wide aggregated playback and VFS cache metrics."""

    model_config = ConfigDict(extra="ignore")

    active_streams_count: int = Field(
        0, ge=0, description="Number of currently active media streams"
    )
    total_streams_count: int = Field(
        0, ge=0, description="Cumulative count of stream sessions since startup"
    )
    total_bytes_transferred: int = Field(
        0, ge=0, description="Cumulative bytes transferred across all streams"
    )
    total_cache_hits: int = Field(
        0, ge=0, description="Cumulative cache hits in chunks"
    )
    total_cache_misses: int = Field(
        0, ge=0, description="Cumulative cache misses in chunks"
    )
    total_chunk_requests: int = Field(
        0, ge=0, description="Cumulative chunk requests (hits + misses)"
    )
    aggregate_cache_hit_rate_pct: float = Field(
        0.0,
        ge=0.0,
        le=100.0,
        description="Global cache hit rate percentage across all chunk requests",
    )
    current_total_throughput_mbps: float = Field(
        0.0,
        ge=0.0,
        description="Total aggregate network transfer throughput across all active streams (MB/s)",
    )
    cached_chunks_in_memory: int = Field(
        0, ge=0, description="Current number of chunks in memory LRU cache"
    )
    cached_chunks_on_disk: int = Field(
        0, ge=0, description="Current number of chunks in disk/tmpfs cache"
    )
    active_pool_connections: int = Field(
        0, ge=0, description="Active upstream HTTP connections in Trio/async pool"
    )
    cdn_refresh_count: int = Field(
        0, ge=0, description="Total number of CDN link refreshes performed"
    )
    stream_error_count: int = Field(
        0, ge=0, description="Total number of stream errors recorded"
    )


class PlaybackTelemetryEvent(BaseModel):
    """Discrete operational event emitted during streaming lifecycle."""

    model_config = ConfigDict(extra="ignore")

    event_id: str = Field(..., description="Unique event identifier (UUID)")
    stream_id: str = Field(..., description="Associated stream session ID")
    event_type: Literal[
        "STREAM_START",
        "STREAM_READ",
        "CACHE_HIT",
        "CACHE_MISS",
        "CDN_REFRESH",
        "STREAM_ERROR",
        "STREAM_COMPLETE",
    ] = Field(..., description="Categorized playback event type")
    title: str = Field(..., description="Sanitized media title or file path")
    timestamp: datetime = Field(
        ..., description="UTC timestamp when the event occurred"
    )
    details: dict[str, Any] = Field(
        default_factory=dict,
        description="Sanitized contextual metadata and parameters",
    )


class PlaybackTelemetrySnapshot(BaseModel):
    """Complete real-time playback telemetry snapshot delivered via REST and 1Hz SSE."""

    model_config = ConfigDict(extra="ignore")

    timestamp: datetime = Field(
        ..., description="UTC timestamp of the telemetry sample"
    )
    aggregate: AggregatePlaybackMetrics = Field(
        ..., description="System-wide aggregate playback metrics"
    )
    active_streams: list[StreamSessionMetric] = Field(
        default_factory=list, description="List of currently active stream sessions"
    )
    recent_events: list[PlaybackTelemetryEvent] = Field(
        default_factory=list,
        description="Chronological ring-buffer of recent playback events (latest 50)",
    )
