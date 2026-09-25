"""Thread-safe in-memory collector and ring-buffer for real-time playback telemetry."""

from __future__ import annotations

import collections
import threading
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from program.contracts.telemetry import redact_sensitive_data
from schemas.playback_telemetry import (
    AggregatePlaybackMetrics,
    PlaybackTelemetryEvent,
    PlaybackTelemetrySnapshot,
    StreamSessionMetric,
)


class StreamSessionTracker:
    """Tracks byte counters and rolling throughput for a single active stream."""

    def __init__(
        self,
        stream_id: str,
        title: str,
        media_item_id: int | None = None,
        client_ip: str | None = None,
        client_user_agent: str | None = None,
        provider: str | None = None,
        user_name: str | None = None,
        player_device: str | None = None,
        playback_state: str = "playing",
        decision: str | None = None,
        video_decision: str | None = None,
        audio_decision: str | None = None,
        quality_profile: str | None = None,
        media_resolution: str | None = None,
        media_bitrate_kbps: int | None = None,
    ) -> None:
        self.stream_id = stream_id
        self.title = title
        self.media_item_id = media_item_id
        self.client_ip = client_ip
        self.client_user_agent = client_user_agent
        self.provider = provider
        self.user_name = user_name
        self.player_device = player_device
        self.playback_state = playback_state
        self.decision = decision
        self.video_decision = video_decision
        self.audio_decision = audio_decision
        self.quality_profile = quality_profile
        self.media_resolution = media_resolution
        self.media_bitrate_kbps = media_bitrate_kbps
        self.started_at = datetime.now(UTC)
        self.last_read_at = self.started_at
        self.bytes_transferred = 0
        self.bytes_from_cache = 0
        self.is_active = True
        self.cdn_url_refreshed = False
        self._lock = threading.Lock()

        # Rolling window: deque of (timestamp, byte_count) for throughput calculation
        self._window: collections.deque[tuple[float, int]] = collections.deque()
        self._window_duration = 5.0  # 5-second window

    def update_attribution(
        self,
        *,
        user_name: str | None = None,
        player_device: str | None = None,
        playback_state: str | None = None,
        decision: str | None = None,
        video_decision: str | None = None,
        audio_decision: str | None = None,
        quality_profile: str | None = None,
        media_resolution: str | None = None,
        media_bitrate_kbps: int | None = None,
    ) -> None:
        """Update Plex/Tautulli attribution details on the active tracker."""
        with self._lock:
            if user_name is not None:
                self.user_name = user_name
            if player_device is not None:
                self.player_device = player_device
            if playback_state is not None:
                norm_state = playback_state.lower().strip()
                if norm_state in {"playing", "paused", "buffering", "stopped"}:
                    self.playback_state = norm_state
                elif norm_state in {"play", "resume"}:
                    self.playback_state = "playing"
                elif norm_state in {"pause"}:
                    self.playback_state = "paused"
                elif norm_state in {"stop", "scrobble"}:
                    self.playback_state = "stopped"
                else:
                    self.playback_state = "playing"

                if self.playback_state == "stopped":
                    self.is_active = False
            if decision is not None:
                norm_decision = (
                    decision.lower().strip().replace(" ", "").replace("-", "_")
                )
                if norm_decision in {"direct_play", "directplay", "direct"}:
                    self.decision = "direct_play"
                elif norm_decision in {"direct_stream", "directstream"}:
                    self.decision = "direct_stream"
                elif norm_decision in {"transcode", "transcoding"}:
                    self.decision = "transcode"
                elif norm_decision in {"unknown"}:
                    self.decision = "unknown"
                else:
                    self.decision = "unknown"
            if video_decision is not None:
                self.video_decision = video_decision
            if audio_decision is not None:
                self.audio_decision = audio_decision
            if quality_profile is not None:
                self.quality_profile = quality_profile
            if media_resolution is not None:
                self.media_resolution = media_resolution
                if not self.quality_profile:
                    res_upper = media_resolution.upper()
                    self.quality_profile = f"{res_upper}p" if res_upper.isdigit() else res_upper
            if media_bitrate_kbps is not None:
                self.media_bitrate_kbps = media_bitrate_kbps

    def _format_session_badge_locked(
        self, throughput_mbps: float, cache_rate: float
    ) -> str:
        """Format live session badge: e.g. 'Alice · 4K Direct Play · 42 Mbps · RD Cache'."""
        parts: list[str] = []

        # 1. User / Player
        user_part = self.user_name or ""
        if self.player_device:
            user_part = (
                f"{user_part} ({self.player_device})"
                if user_part
                else self.player_device
            )
        if user_part:
            parts.append(user_part)

        # 2. Quality & Decision
        decision_label = ""
        if self.decision in {"direct_play", "direct"}:
            decision_label = "Direct Play"
        elif self.decision == "direct_stream":
            decision_label = "Direct Stream"
        elif self.decision == "transcode":
            decision_label = "Transcode"

        quality_label = self.quality_profile or ""
        if not quality_label and self.media_resolution:
            res = self.media_resolution.upper()
            quality_label = f"{res}p" if res.isdigit() else res

        if quality_label and decision_label:
            parts.append(f"{quality_label} {decision_label}")
        elif quality_label:
            parts.append(quality_label)
        elif decision_label:
            parts.append(decision_label)

        # 3. Bitrate / Speed
        if throughput_mbps > 0.0:
            speed_mbps = round(throughput_mbps * 8.0, 1)  # MB/s to Mbps
            parts.append(f"{speed_mbps} Mbps")
        elif self.media_bitrate_kbps and self.media_bitrate_kbps > 0:
            speed_mbps = round(self.media_bitrate_kbps / 1000.0, 1)
            parts.append(f"{speed_mbps} Mbps")

        # 4. Storage / Cache source
        prov = (self.provider or "Debrid").upper()
        if prov.startswith("REAL"):
            prov = "RD"
        elif prov.startswith("ALL"):
            prov = "AD"
        elif prov.startswith("TORBOX"):
            prov = "TB"

        if cache_rate >= 50.0:
            parts.append(f"{prov} Cache")
        else:
            parts.append(f"{prov} Stream")

        return " · ".join(parts)

    def record_read(self, nbytes: int, from_cache: bool = False) -> None:
        """Record bytes read in this session."""
        now_mono = time.monotonic()
        with self._lock:
            self.bytes_transferred += nbytes
            if from_cache:
                self.bytes_from_cache += nbytes
            self.last_read_at = datetime.now(UTC)
            self._window.append((now_mono, nbytes))
            self._prune_window(now_mono)

    def mark_cdn_refreshed(self) -> None:
        with self._lock:
            self.cdn_url_refreshed = True

    def mark_completed(self) -> None:
        with self._lock:
            self.is_active = False

    def _prune_window(self, now_mono: float) -> None:
        cutoff = now_mono - self._window_duration
        while self._window and self._window[0][0] < cutoff:
            self._window.popleft()

    def _get_current_throughput_mbps_locked(self, now_mono: float) -> float:
        """Calculate throughput in MB/s over rolling window without re-locking."""
        self._prune_window(now_mono)
        if not self._window:
            return 0.0
        total_bytes = sum(b for _, b in self._window)
        time_span = max(0.5, now_mono - self._window[0][0])
        mb_transferred = total_bytes / (1024 * 1024)
        return round(mb_transferred / time_span, 2)

    def get_current_throughput_mbps(self) -> float:
        """Calculate throughput in MB/s over rolling window."""
        now_mono = time.monotonic()
        with self._lock:
            return self._get_current_throughput_mbps_locked(now_mono)

    def to_metric(self) -> StreamSessionMetric:
        now_mono = time.monotonic()
        with self._lock:
            cache_rate = 0.0
            if self.bytes_transferred > 0:
                cache_rate = round(
                    (self.bytes_from_cache / self.bytes_transferred) * 100.0, 1
                )
            throughput = self._get_current_throughput_mbps_locked(now_mono)
            badge = self._format_session_badge_locked(throughput, cache_rate)
            return StreamSessionMetric(
                stream_id=self.stream_id,
                media_item_id=self.media_item_id,
                title=self.title,
                client_ip=self.client_ip,
                client_user_agent=self.client_user_agent,
                bytes_transferred=self.bytes_transferred,
                bytes_from_cache=self.bytes_from_cache,
                cache_hit_rate_pct=cache_rate,
                current_throughput_mbps=throughput,
                started_at=self.started_at,
                last_read_at=self.last_read_at,
                is_active=self.is_active,
                provider=self.provider,
                cdn_url_refreshed=self.cdn_url_refreshed,
                user_name=self.user_name,
                player_device=self.player_device,
                playback_state=self.playback_state,  # type: ignore[arg-type]
                decision=self.decision,  # type: ignore[arg-type]
                video_decision=self.video_decision,
                audio_decision=self.audio_decision,
                quality_profile=self.quality_profile,
                media_resolution=self.media_resolution,
                media_bitrate_kbps=self.media_bitrate_kbps,
                session_badge=badge if badge else None,
            )


class PlaybackTelemetryCollector:
    """Thread-safe singleton collector for playback observability and live telemetry."""

    _instance: PlaybackTelemetryCollector | None = None
    _singleton_lock = threading.Lock()

    def __new__(cls) -> Any:
        with cls._singleton_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        self._lock = threading.Lock()
        self._sessions: dict[str, StreamSessionTracker] = {}
        self._events: collections.deque[PlaybackTelemetryEvent] = collections.deque(
            maxlen=50
        )

        # Cumulative atomic counters
        self._total_streams_count = 0
        self._total_bytes_transferred = 0
        self._total_cache_hits = 0
        self._total_cache_misses = 0
        self._cdn_refresh_count = 0
        self._stream_error_count = 0
        self._cached_chunks_in_memory = 0
        self._cached_chunks_on_disk = 0
        self._active_pool_connections = 0

        self._initialized = True
        logger.info("PlaybackTelemetryCollector initialized")

    def register_stream_start(
        self,
        stream_id: str,
        title: str,
        media_item_id: int | None = None,
        client_ip: str | None = None,
        client_user_agent: str | None = None,
        provider: str | None = None,
    ) -> StreamSessionTracker:
        """Register commencement of a new media stream session."""
        with self._lock:
            self._total_streams_count += 1
            tracker = StreamSessionTracker(
                stream_id=stream_id,
                title=title,
                media_item_id=media_item_id,
                client_ip=client_ip,
                client_user_agent=client_user_agent,
                provider=provider,
            )
            self._sessions[stream_id] = tracker
            self._add_event_locked(
                stream_id=stream_id,
                event_type="STREAM_START",
                title=title,
                details={
                    "client_ip": client_ip,
                    "client_user_agent": client_user_agent,
                    "provider": provider,
                    "media_item_id": media_item_id,
                },
            )
            return tracker

    def get_session(self, stream_id: str) -> StreamSessionTracker | None:
        """Retrieve a specific session tracker by stream_id."""
        with self._lock:
            return self._sessions.get(stream_id)

    def record_stream_read(
        self, stream_id: str, nbytes: int, from_cache: bool = False
    ) -> None:
        """Record a data chunk transferred to a client."""
        with self._lock:
            self._total_bytes_transferred += nbytes
            tracker = self._sessions.get(stream_id)
            if tracker:
                tracker.record_read(nbytes, from_cache=from_cache)

    def correlate_plex_session(
        self,
        *,
        event: str,
        user_name: str | None = None,
        player_device: str | None = None,
        client_ip: str | None = None,
        file_path: str | None = None,
        title: str | None = None,
        decision: str | None = None,
        video_decision: str | None = None,
        audio_decision: str | None = None,
        quality_profile: str | None = None,
        media_resolution: str | None = None,
        media_bitrate_kbps: int | None = None,
        guids: list[str] | None = None,
    ) -> int:
        """Correlate incoming Plex/Tautulli webhook event with active VFS/HTTP stream sessions.

        Returns number of active stream sessions updated.
        """
        # Map Plex webhook event to normalized playback_state
        state = "playing"
        if "pause" in event.lower():
            state = "paused"
        elif "stop" in event.lower() or "scrobble" in event.lower():
            state = "stopped"
        elif "buffer" in event.lower():
            state = "buffering"

        matched_count = 0
        with self._lock:
            clean_file = (file_path or "").strip().lower()
            file_name = clean_file.split("/")[-1].split("\\")[-1] if clean_file else ""
            clean_title = (title or "").strip().lower()

            for stream_id, tracker in self._sessions.items():
                if not tracker.is_active and state != "stopped":
                    continue

                matched = False
                stream_title_lower = tracker.title.lower()
                stream_id_lower = stream_id.lower()

                # 1. Match by file path / file name in stream_id or title
                if (
                    file_name
                    and (
                        file_name in stream_title_lower or file_name in stream_id_lower
                    )
                    or clean_file
                    and (
                        clean_file in stream_title_lower
                        or clean_file in stream_id_lower
                    )
                    or clean_title
                    and (
                        clean_title in stream_title_lower
                        or stream_title_lower in clean_title
                    )
                    or client_ip
                    and tracker.client_ip == client_ip
                ):
                    matched = True

                if matched:
                    tracker.update_attribution(
                        user_name=user_name,
                        player_device=player_device,
                        playback_state=state,
                        decision=decision,
                        video_decision=video_decision,
                        audio_decision=audio_decision,
                        quality_profile=quality_profile,
                        media_resolution=media_resolution,
                        media_bitrate_kbps=media_bitrate_kbps,
                    )
                    matched_count += 1

            # Also log discrete telemetry event for visibility
            self._add_event_locked(
                stream_id=f"plex:{user_name or 'user'}:{event}",
                event_type="STREAM_START"
                if state == "playing"
                else ("STREAM_COMPLETE" if state == "stopped" else "STREAM_READ"),
                title=title or file_name or "Plex Media",
                details={
                    "event": event,
                    "user": user_name,
                    "player": player_device,
                    "decision": decision,
                    "matched_streams": matched_count,
                    "guids": guids or [],
                },
            )

        return matched_count

    def record_cache_hit(
        self, stream_id: str | None = None, title: str | None = None, nbytes: int = 0
    ) -> None:
        """Record a chunk cache hit."""
        with self._lock:
            self._total_cache_hits += 1
            if stream_id and title:
                self._add_event_locked(
                    stream_id=stream_id,
                    event_type="CACHE_HIT",
                    title=title,
                    details={"bytes": nbytes},
                )

    def record_cache_miss(
        self, stream_id: str | None = None, title: str | None = None, nbytes: int = 0
    ) -> None:
        """Record a chunk cache miss."""
        with self._lock:
            self._total_cache_misses += 1
            if stream_id and title:
                self._add_event_locked(
                    stream_id=stream_id,
                    event_type="CACHE_MISS",
                    title=title,
                    details={"bytes": nbytes},
                )

    def record_cdn_refresh(
        self, stream_id: str, title: str, details: dict[str, Any] | None = None
    ) -> None:
        """Record a CDN link refresh event."""
        with self._lock:
            self._cdn_refresh_count += 1
            tracker = self._sessions.get(stream_id)
            if tracker:
                tracker.mark_cdn_refreshed()
            self._add_event_locked(
                stream_id=stream_id,
                event_type="CDN_REFRESH",
                title=title,
                details=details or {},
            )

    def record_stream_error(
        self,
        stream_id: str,
        title: str,
        error_msg: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Record a stream failure/error event."""
        with self._lock:
            self._stream_error_count += 1
            event_details = details.copy() if details else {}
            event_details["error"] = error_msg
            self._add_event_locked(
                stream_id=stream_id,
                event_type="STREAM_ERROR",
                title=title,
                details=event_details,
            )

    def register_stream_complete(
        self, stream_id: str, title: str, details: dict[str, Any] | None = None
    ) -> None:
        """Mark a stream session as terminated/released."""
        with self._lock:
            tracker = self._sessions.get(stream_id)
            if tracker:
                tracker.mark_completed()
            self._add_event_locked(
                stream_id=stream_id,
                event_type="STREAM_COMPLETE",
                title=title,
                details=details or {},
            )

    def update_pool_connections(self, active_count: int) -> None:
        """Update active HTTP pool connection count."""
        with self._lock:
            self._active_pool_connections = max(0, active_count)

    def update_cache_counts(self, memory_chunks: int, disk_chunks: int) -> None:
        """Update cached chunk counts."""
        with self._lock:
            self._cached_chunks_in_memory = max(0, memory_chunks)
            self._cached_chunks_on_disk = max(0, disk_chunks)

    def _add_event_locked(
        self,
        stream_id: str,
        event_type: Any,
        title: str,
        details: dict[str, Any],
    ) -> None:
        event = PlaybackTelemetryEvent(
            event_id=str(uuid.uuid4()),
            stream_id=stream_id,
            event_type=event_type,
            title=title,
            timestamp=datetime.now(UTC),
            details=redact_sensitive_data(details),
        )
        self._events.append(event)

    def get_snapshot(self) -> PlaybackTelemetrySnapshot:
        """Generate a complete telemetry snapshot with active streams, metrics, and events."""
        with self._lock:
            active_list: list[StreamSessionMetric] = []
            total_active_throughput = 0.0

            # Prune completed or ancient inactive sessions older than 1 hour
            now_dt = datetime.now(UTC)
            stale_keys = [
                k
                for k, v in self._sessions.items()
                if (not v.is_active)
                and (now_dt - v.last_read_at).total_seconds() > 3600
            ]
            for k in stale_keys:
                del self._sessions[k]

            for tracker in self._sessions.values():
                metric = tracker.to_metric()
                if metric.is_active:
                    active_list.append(metric)
                    total_active_throughput += metric.current_throughput_mbps

            total_chunks = self._total_cache_hits + self._total_cache_misses
            aggregate_hit_rate = 0.0
            if total_chunks > 0:
                aggregate_hit_rate = round(
                    (self._total_cache_hits / total_chunks) * 100.0, 1
                )

            aggregate = AggregatePlaybackMetrics(
                active_streams_count=len(active_list),
                total_streams_count=self._total_streams_count,
                total_bytes_transferred=self._total_bytes_transferred,
                total_cache_hits=self._total_cache_hits,
                total_cache_misses=self._total_cache_misses,
                total_chunk_requests=total_chunks,
                aggregate_cache_hit_rate_pct=aggregate_hit_rate,
                current_total_throughput_mbps=round(total_active_throughput, 2),
                cached_chunks_in_memory=self._cached_chunks_in_memory,
                cached_chunks_on_disk=self._cached_chunks_on_disk,
                active_pool_connections=self._active_pool_connections,
                cdn_refresh_count=self._cdn_refresh_count,
                stream_error_count=self._stream_error_count,
            )

            # Return list of events in reverse chronological order (newest first)
            events_list = list(reversed(self._events))

            return PlaybackTelemetrySnapshot(
                timestamp=datetime.now(UTC),
                aggregate=aggregate,
                active_streams=active_list,
                recent_events=events_list,
            )


playback_telemetry_collector = PlaybackTelemetryCollector()
