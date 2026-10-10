import math
from dataclasses import dataclass

from loguru import logger


@dataclass
class AdaptivePrefetchConfig:
    """Configuration parameters for adaptive prefetch window calculation."""

    min_window_chunks: int = 4
    max_window_chunks: int = 48
    target_buffer_seconds: float = 15.0
    chunk_size_bytes: int = 1048576  # 1 MB
    default_fallback_chunks: int = 12
    sequential_tolerance_bytes: int = 1310720  # 1.25 MB (10 blocks)
    ema_alpha: float = 0.35
    pool_throttle_threshold: int = (
        32  # Start scaling down if active leases > 32 (of 64 limit)
    )
    cache_pressure_threshold_pct: float = 85.0
    min_safe_runway_seconds: float = 4.0


class AdaptivePrefetchManager:
    """
    Dynamically sizes streaming prefetch window based on media bitrate,
    consumption velocity, and resource pressure while managing generation tokens
    for immediate seek cancellation.
    """

    def __init__(
        self,
        config: AdaptivePrefetchConfig | None = None,
        initial_bitrate: int | None = None,
    ):
        self.config = config or AdaptivePrefetchConfig()
        self.metadata_bitrate: int | None = (
            initial_bitrate
            if (
                isinstance(initial_bitrate, int)
                and not isinstance(initial_bitrate, bool)
                and initial_bitrate > 0
            )
            else None
        )
        self.estimated_bitrate: float = 0.0
        self.last_read_time: float | None = None
        self.last_read_end: int | None = None
        self.current_generation: int = 0
        self.last_calculated_window: int = self.config.default_fallback_chunks
        self.seek_count: int = 0
        self.consecutive_sequential_reads: int = 0
        self.aborted_prefetches: int = 0
        self._recent_fetch_durations: list[float] = []
        self._starvation_count: int = 0
        self._last_consumption_bps: float = 0.0
        self._cached_runway_bytes: int = 0
        self._inflight_runway_bytes: int = 0
        self._accumulated_read_bytes: int = 0
        self._accumulated_read_start_time: float | None = None

    def set_metadata_bitrate(self, bitrate: int | None) -> None:
        """Set known bitrate from media metadata or prober (in bits per second)."""
        if isinstance(bitrate, int) and not isinstance(bitrate, bool) and bitrate > 0:
            self.metadata_bitrate = bitrate
        elif bitrate is None:
            self.metadata_bitrate = None

    def get_effective_bitrate(self) -> int:
        """
        Return the most accurate bitrate available:
        Prioritizes verified metadata bitrate, then estimated consumption bitrate.
        Returns 0 if no reliable bitrate is available.
        """
        if self.metadata_bitrate is not None and self.metadata_bitrate > 0:
            return self.metadata_bitrate
        if self.estimated_bitrate > 0:
            return int(self.estimated_bitrate)
        return 0

    def record_read(self, start: int, end: int, current_time: float) -> bool:
        """
        Record a read request to track sequentiality and estimate consumption bitrate.
        Returns True if the read was contiguous/sequential with prior playback.
        Returns False if a seek occurred (which increments the generation token).
        """
        size = end - start + 1
        is_sequential = False

        if self.last_read_end is not None and self.last_read_time is not None:
            # Check sequential proximity
            expected_next = self.last_read_end + 1
            if abs(start - expected_next) <= self.config.sequential_tolerance_bytes:
                is_sequential = True
                if self._accumulated_read_start_time is None:
                    self._accumulated_read_start_time = self.last_read_time
                self._accumulated_read_bytes += size

                delta_t = current_time - self._accumulated_read_start_time
                if 0.05 <= delta_t <= 10.0:
                    instant_bitrate = (self._accumulated_read_bytes * 8.0) / delta_t
                    # Reset accumulator window after taking a valid sample
                    self._accumulated_read_bytes = 0
                    self._accumulated_read_start_time = current_time

                    # Reasonable range: 200 Kbps to 500 Mbps
                    if 200_000 <= instant_bitrate <= 500_000_000:
                        if self.estimated_bitrate <= 0:
                            self.estimated_bitrate = instant_bitrate
                        else:
                            alpha = self.config.ema_alpha
                            self.estimated_bitrate = (
                                alpha * instant_bitrate
                                + (1.0 - alpha) * self.estimated_bitrate
                            )
                        self._last_consumption_bps = instant_bitrate
                elif delta_t > 10.0:
                    # Stale accumulator window (e.g. pause/idle); reset
                    self._accumulated_read_bytes = size
                    self._accumulated_read_start_time = self.last_read_time
            else:
                # Discontiguous sequential jump: reset accumulator
                self._accumulated_read_bytes = 0
                self._accumulated_read_start_time = None
        else:
            self._accumulated_read_bytes = 0
            self._accumulated_read_start_time = None

        if not is_sequential and self.last_read_end is not None:
            # Non-sequential jump detected: Player sought
            self.current_generation += 1
            self.seek_count += 1
            self.consecutive_sequential_reads = 0
            self._accumulated_read_bytes = 0
            self._accumulated_read_start_time = None
            logger.debug(
                f"Seek detected: jumped from {self.last_read_end} to {start}. "
                f"Advanced generation to {self.current_generation}"
            )
        elif is_sequential:
            self.consecutive_sequential_reads += 1

        self.last_read_end = end
        self.last_read_time = current_time
        return is_sequential

    def record_provider_fetch(self, duration_seconds: float) -> None:
        """Record fetch-through-publication latency (network plus cache admission).

        The legacy method name is retained for callers; this is not network-only p95.
        """
        if duration_seconds > 0:
            self._recent_fetch_durations.append(duration_seconds)
            if len(self._recent_fetch_durations) > 20:
                self._recent_fetch_durations.pop(0)

    def record_runway(self, *, cached_bytes: int, in_flight_bytes: int = 0) -> None:
        """Record contiguous runway observations for pressure decisions."""
        self._cached_runway_bytes = max(0, cached_bytes)
        self._inflight_runway_bytes = max(0, in_flight_bytes)

    def cached_runway_seconds(self) -> float:
        """Return cached contiguous runway using observed consumption rate."""
        bitrate = self.get_effective_bitrate()
        if bitrate <= 0:
            return 0.0
        return self._cached_runway_bytes * 8.0 / bitrate

    def effective_runway_seconds(self) -> float:
        """Return cached plus known in-flight contiguous runway."""
        bitrate = self.get_effective_bitrate()
        if bitrate <= 0:
            return 0.0
        return (self._cached_runway_bytes + self._inflight_runway_bytes) * 8.0 / bitrate

    def should_prefetch_now(self) -> bool:
        """Indicate whether observed runway is approaching the fetch-latency tail."""
        bitrate = self.get_effective_bitrate()
        if bitrate <= 0:
            return False
        return self.cached_runway_seconds() < max(
            self.config.min_safe_runway_seconds,
            self.get_fetch_p95() + 0.5,
        ) and self.effective_runway_seconds() < max(
            self.config.min_safe_runway_seconds,
            self.get_fetch_p95() + 0.5,
        )

    def record_starvation(self) -> None:
        """Record event where foreground consumer awaited an uncached chunk."""
        self._starvation_count += 1

    @property
    def starvation_count(self) -> int:
        return self._starvation_count

    def get_fetch_p95(self) -> float:
        if not self._recent_fetch_durations:
            return 2.5
        sorted_d = sorted(self._recent_fetch_durations)
        idx = math.ceil(0.95 * len(sorted_d)) - 1
        return sorted_d[idx]

    def is_runway_under_pressure(
        self,
        *,
        cached_runway_bytes: int,
        in_flight_bytes: int = 0,
    ) -> bool:
        """Return True if contiguous playback runway is nearing provider fetch tail."""
        bitrate = self.get_effective_bitrate()
        if bitrate <= 0:
            return False
        total_runway_bytes = cached_runway_bytes + in_flight_bytes
        runway_seconds = (total_runway_bytes * 8.0) / bitrate
        required_safety = max(
            self.config.min_safe_runway_seconds, self.get_fetch_p95() * 1.5
        )
        return runway_seconds < required_safety

    def calculate_window(
        self,
        *,
        active_leases: int = 0,
        max_leases: int = 64,
        cache_usage_pct: float = 0.0,
        cache_protected_pct: float | None = None,
    ) -> int:
        """
        Calculate the optimal prefetch window (in chunks) using:
        W = max(W_min, min(W_max, ceil(bitrate_bps * T_buffer / (8 * chunk_bytes))))
        Throttled by active connection leases and cache capacity.
        """
        bitrate = self.get_effective_bitrate()

        if bitrate <= 0:
            window = self.config.default_fallback_chunks
        else:
            target_seconds = max(
                self.config.target_buffer_seconds, self.get_fetch_p95() * 1.5
            )
            buffer_bytes = (bitrate * target_seconds) / 8.0
            raw_chunks = math.ceil(buffer_bytes / self.config.chunk_size_bytes)
            window = max(
                self.config.min_window_chunks,
                min(self.config.max_window_chunks, raw_chunks),
            )

        # Connection pool pressure throttling
        if active_leases > self.config.pool_throttle_threshold and max_leases > 0:
            remaining = max(1, max_leases - active_leases)
            throttle_scale = min(
                1.0, remaining / (max_leases - self.config.pool_throttle_threshold)
            )
            window = max(self.config.min_window_chunks, int(window * throttle_scale))

        # Cache capacity pressure throttling:
        # Prioritize active playback over cache retention.
        # If cache_protected_pct is provided, throttle ONLY when un-evictable protected
        # playback data nears capacity. Historical evictable cache does not throttle prefetch.
        raw_pressure = (
            cache_protected_pct if cache_protected_pct is not None else cache_usage_pct
        )
        effective_cache_pressure = float(raw_pressure)
        if effective_cache_pressure >= 95.0:
            window = self.config.min_window_chunks
        elif effective_cache_pressure >= self.config.cache_pressure_threshold_pct:
            window = max(self.config.min_window_chunks, int(window * 0.5))

        # Post-seek ramp-up: ramp up prefetch gradually to conserve bandwidth during seeking/scrubbing
        if self.seek_count > 0 and self.consecutive_sequential_reads < 3:
            ramp_cap = max(
                self.config.min_window_chunks,
                8 * (self.consecutive_sequential_reads + 1),
            )
            window = min(window, ramp_cap)

        # Unknown-bitrate fallback must obey the same hard resource bounds.
        window = max(
            self.config.min_window_chunks,
            min(self.config.max_window_chunks, window),
        )
        self.last_calculated_window = window
        return window

    def should_abort_prefetch(self, prefetch_generation: int) -> bool:
        """
        Check if an in-flight prefetch should abort immediately because the player
        has sought to a new position.
        """
        aborted = prefetch_generation != self.current_generation
        if aborted:
            self.aborted_prefetches += 1
        return aborted
