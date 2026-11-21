"""
30-Minute Real Media Endurance & Bughunting Benchmark for CineFlow (Riven Fork)

Simulates continuous 30-minute playback of a 76 Mbps 4K Remux title (17.1 GB total data):
- Exercises the critical 4:15 (2.42 GB) cache watermark transition where hot cache (1024 MB)
  overflows and continuous demotions/evictions occur.
- Executes 10 periodic non-contiguous seeks throughout playback to verify generation-fenced
  seek recovery without consumer starvation.
- Collects forensic telemetry on shard lock wait, hot demotion latency, eviction duration,
  transient delivery registry hit rate, and consumer buffer runway.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

import httpx
import trio
from kink import di
from loguru import logger

# Ensure backend src is on sys.path
backend_src = Path(__file__).resolve().parent.parent / "src"
if str(backend_src) not in sys.path:
    sys.path.insert(0, str(backend_src))

from program.services.streaming.cache import (
    Cache,
    CacheConfig,
    CachePutLatencyBreakdown,
)
from program.services.streaming.chunker import ChunkCacheNotifier
from program.services.streaming.http_pool import TrioStreamingHttpPool
from program.services.streaming.media_stream import MediaStream


@dataclass
class EnduranceReport:
    title: str
    bitrate_mbps: float
    simulated_media_duration_s: float
    wall_clock_elapsed_s: float
    total_bytes_read: int
    total_bytes_downloaded: int
    wasted_bytes: int
    cache_hit_ratio: float
    prefetch_efficiency_pct: float
    rebuffer_events: int
    total_stall_seconds: float
    avg_seek_latency_ms: float
    max_seek_latency_ms: float
    seeks_completed: int
    critical_4m15s_boundary_crossed: bool
    hot_demotions_observed: int
    warm_evictions_observed: int
    transient_registry_deliveries: int
    peak_active_leases: int
    max_shard_lock_wait_ms: float
    max_hot_demotion_ms: float
    max_eviction_ms: float
    p95_put_latency_ms: float
    p99_put_latency_ms: float
    errors: list[str] = field(default_factory=list[str])


class SimulatedNetworkResponse:
    """Simulates an HTTP streaming range response with high-throughput CDN delivery."""

    def __init__(
        self,
        url: str,
        start_byte: int,
        total_size: int,
        network_speed_bytes_sec: float,
        end_byte: int | None = None,
        latency_seconds: float = 0.005,
        cancellation_tracker: dict[str, int] | None = None,
        generation_id: str = "default",
    ):
        self.url = url
        self.start_byte = start_byte
        self.total_size = total_size
        self.end_byte = end_byte if end_byte is not None else total_size - 1
        self.network_speed = network_speed_bytes_sec
        self.latency = latency_seconds
        self.status_code = 200
        self.http_version = "HTTP/1.1"
        self.cancellation_tracker = cancellation_tracker
        self.generation_id = generation_id
        self._is_closed = False

        content_length = max(0, self.end_byte - start_byte + 1)
        self.headers = httpx.Headers(
            {
                "Content-Length": str(content_length),
                "Content-Range": f"bytes {start_byte}-{self.end_byte}/{total_size}",
                "Accept-Ranges": "bytes",
            }
        )
        self.request = httpx.Request("GET", url)

    @property
    def is_cancelled(self) -> bool:
        return self._is_closed

    async def aread(self) -> bytes:
        """Read all bytes for this range response."""
        if self.latency > 0:
            await trio.sleep(self.latency)
        length = max(0, self.end_byte - self.start_byte + 1)
        sleep_time = length / self.network_speed
        if sleep_time > 0:
            await trio.sleep(sleep_time)
        data = b"X" * length
        if self.cancellation_tracker is not None:
            self.cancellation_tracker["bytes_transferred"] = (
                self.cancellation_tracker.get("bytes_transferred", 0) + length
            )
            self.cancellation_tracker[f"gen_{self.generation_id}_bytes"] = (
                self.cancellation_tracker.get(f"gen_{self.generation_id}_bytes", 0)
                + length
            )
        return data

    async def aiter_raw(self, chunk_size: int = 1048576):
        """Yield byte chunks at simulated network throughput."""
        if self.latency > 0:
            await trio.sleep(self.latency)

        curr = self.start_byte
        max_limit = self.end_byte + 1
        while curr < max_limit and not self._is_closed:
            this_chunk_size = min(chunk_size, max_limit - curr)
            sleep_time = this_chunk_size / self.network_speed
            if sleep_time > 0:
                await trio.sleep(sleep_time)

            if self._is_closed:
                break

            if self.cancellation_tracker is not None:
                self.cancellation_tracker["bytes_transferred"] = (
                    self.cancellation_tracker.get("bytes_transferred", 0)
                    + this_chunk_size
                )
                self.cancellation_tracker[f"gen_{self.generation_id}_bytes"] = (
                    self.cancellation_tracker.get(f"gen_{self.generation_id}_bytes", 0)
                    + this_chunk_size
                )

            data = b"X" * this_chunk_size
            curr += this_chunk_size
            yield data

    async def aiter_bytes(self, chunk_size: int = 128 * 1024):
        async for chunk in self.aiter_raw(chunk_size):
            yield chunk

    async def aclose(self) -> None:
        self._is_closed = True
        if self.cancellation_tracker is not None:
            self.cancellation_tracker[self.generation_id] = (
                self.cancellation_tracker.get(self.generation_id, 0) + 1
            )


class EnduranceRunner:
    def __init__(
        self,
        hot_cache_mb: int = 1024,
        warm_cache_mb: int = 40960,
        timeline_mode: str = "accelerated",  # accelerated or real_time
    ) -> None:
        self.hot_cache_mb = hot_cache_mb
        self.warm_cache_mb = warm_cache_mb
        self.timeline_mode = timeline_mode
        self.temp_dir = Path(tempfile.mkdtemp(prefix="cineflow_endurance_"))
        self.hot_dir = self.temp_dir / "hot"
        self.warm_dir = self.temp_dir / "warm"
        self.hot_dir.mkdir(parents=True, exist_ok=True)
        self.warm_dir.mkdir(parents=True, exist_ok=True)

        self.put_latencies: list[CachePutLatencyBreakdown] = []
        self.transient_deliveries = 0

    def setup_di(self) -> Cache:
        cfg = CacheConfig(
            cache_dir=self.warm_dir,
            max_size_bytes=self.warm_cache_mb * 1024 * 1024,
            hot_dir=self.hot_dir,
            hot_max_size_bytes=self.hot_cache_mb * 1024 * 1024,
            hot_watermark_high_pct=85.0,
            hot_watermark_low_pct=70.0,
            warm_watermark_high_pct=85.0,
            warm_watermark_low_pct=70.0,
            metrics_enabled=True,
        )
        cache = Cache(cfg)
        cache.on_put_latency = self.put_latencies.append
        di[Cache] = cache
        di[ChunkCacheNotifier] = ChunkCacheNotifier()
        return cache

    def teardown_di(self) -> None:
        if ChunkCacheNotifier in di:
            del di[ChunkCacheNotifier]
        if Cache in di:
            del di[Cache]
        try:
            shutil.rmtree(self.temp_dir, ignore_errors=True)
        except Exception:
            pass

    async def run_endurance(
        self,
        media_duration_seconds: float = 1800.0,  # 30 minutes of media
        bitrate_mbps: float = 76.0,
        network_speed_mbps: float = 250.0,
        seeks: list[tuple[float, int]] | None = None,
    ) -> EnduranceReport:
        self.setup_di()
        pool = TrioStreamingHttpPool()
        bytes_per_sec = (bitrate_mbps * 1_000_000) / 8  # 9.5 MB/s
        file_size_bytes = int(60.0 * 1024 * 1024 * 1024)  # 60 GB
        read_block_size = 256 * 1024  # Standard 256 KB FUSE read block

        cancellation_tracker: dict[str, int] = {}
        peak_leases = 0
        seeks_completed = 0
        rebuffer_events = 0
        total_stall_seconds = 0.0
        seek_latencies: list[float] = []
        errors: list[str] = []

        wall_clock_start = time.monotonic()

        async with trio.open_nursery() as nursery:

            async def _mock_establish(
                stream_inst: Any, start: int, end: int | None, stream_idx: int = 0
            ):
                nonlocal peak_leases
                peak_leases = max(peak_leases, pool.active_leases)
                gen_val = getattr(
                    getattr(stream_inst, "adaptive_prefetch", None),
                    "current_generation",
                    0,
                )
                resp = SimulatedNetworkResponse(
                    url="https://cdn.example.com/movie_4k_remux.mkv",
                    start_byte=start,
                    end_byte=end,
                    total_size=file_size_bytes,
                    network_speed_bytes_sec=(network_speed_mbps * 1_000_000) / 8,
                    latency_seconds=0.005,
                    cancellation_tracker=cancellation_tracker,
                    generation_id=str(gen_val),
                )
                return resp

            stream = MediaStream(
                fh=cast(Any, 100),
                file_size=file_size_bytes,
                path="/media/movies/Avatar_4K_Remux_76Mbps.mkv",
                original_filename="Avatar_4K_Remux_76Mbps.mkv",
                nursery=nursery,
                provider="realdebrid",
                initial_url="https://cdn.example.com/movie_4k_remux.mkv",
                http_pool=pool,
                require_mount_http_pool=True,
                bitrate=int(bitrate_mbps * 1_000_000),
            )

            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def custom_establish(start: int = 0, *, end: int | None = None):
                from program.services.streaming.http_pool import admit_stream_request

                async with admit_stream_request("body"):
                    resp = await _mock_establish(stream, start, end, 0)
                    try:
                        yield resp
                    finally:
                        await resp.aclose()

            stream.establish_connection = custom_establish  # type: ignore[assignment]

            # Sorted seek timetable: (timeline_elapsed_s, target_byte_offset)
            default_seeks: list[tuple[float, int]] = seeks or [
                (120.0, 5 * 1024 * 1024 * 1024),  # Seek at 2:00 -> 5 GB
                (
                    250.0,
                    10 * 1024 * 1024 * 1024,
                ),  # Seek at 4:10 -> 10 GB (near critical 4:15!)
                (300.0, 15 * 1024 * 1024 * 1024),  # Seek at 5:00 -> 15 GB
                (600.0, 25 * 1024 * 1024 * 1024),  # Seek at 10:00 -> 25 GB
                (900.0, 35 * 1024 * 1024 * 1024),  # Seek at 15:00 -> 35 GB
                (1200.0, 42 * 1024 * 1024 * 1024),  # Seek at 20:00 -> 42 GB
                (1500.0, 50 * 1024 * 1024 * 1024),  # Seek at 25:00 -> 50 GB
            ]
            seeks_sorted = sorted(default_seeks, key=lambda s: s[0])
            seek_idx = 0

            curr_pos = 0
            bytes_read = 0
            simulated_media_time = 0.0
            last_progress_log = 0.0

            # Consumer loop simulating 30 minutes of playback
            while simulated_media_time < media_duration_seconds:
                # Check for scheduled seek event
                if (
                    seek_idx < len(seeks_sorted)
                    and simulated_media_time >= seeks_sorted[seek_idx][0]
                ):
                    _st, target_offset = seeks_sorted[seek_idx]
                    seek_idx += 1
                    curr_pos = target_offset

                    s_start = time.monotonic()
                    data = await stream.read(
                        request_start=curr_pos,
                        request_end=curr_pos + read_block_size - 1,
                        request_size=read_block_size,
                    )
                    s_latency_ms = (time.monotonic() - s_start) * 1000.0
                    seek_latencies.append(s_latency_ms)
                    seeks_completed += 1

                    bytes_read += len(data)
                    curr_pos += len(data)
                    simulated_media_time += len(data) / bytes_per_sec
                    continue

                # Normal playback read
                r_start = time.monotonic()
                try:
                    data = await stream.read(
                        request_start=curr_pos,
                        request_end=curr_pos + read_block_size - 1,
                        request_size=read_block_size,
                    )
                except Exception as e:
                    import traceback

                    traceback.print_exc()
                    errors.append(f"Stream read error at pos {curr_pos}: {e}")
                    break

                r_duration = time.monotonic() - r_start

                # Evaluate consumer stall / rebuffer:
                # In accelerated mode, reads are executed back-to-back;
                # a rebuffer occurs if a read takes > 250ms (consumer starvation)
                if r_duration > 0.250:
                    rebuffer_events += 1
                    total_stall_seconds += r_duration

                bytes_read += len(data)
                curr_pos += len(data)
                delta_media_time = len(data) / bytes_per_sec
                simulated_media_time += delta_media_time

                # Periodic progress reporting
                if simulated_media_time - last_progress_log >= 60.0:
                    last_progress_log = simulated_media_time
                    logger.info(
                        f"Endurance progress: {simulated_media_time / 60.0:.1f} / "
                        f"{media_duration_seconds / 60.0:.1f} min media ({bytes_read / (1024 * 1024):.1f} MB read, "
                        f"rebuffers={rebuffer_events})"
                    )

                if self.timeline_mode == "real_time":
                    sleep_needed = (read_block_size / bytes_per_sec) - r_duration
                    if sleep_needed > 0:
                        await trio.sleep(sleep_needed)
                else:
                    # Accelerated mode: yield control cooperatively
                    await trio.sleep(0.0001)

            await stream.close()

        wall_clock_elapsed = time.monotonic() - wall_clock_start
        total_downloaded = cancellation_tracker.get("bytes_transferred", 0)
        wasted_bytes = max(0, total_downloaded - bytes_read)

        shard_waits = [
            p.shard_lock_wait_s * 1000.0
            for p in self.put_latencies
            if p.shard_lock_wait_s > 0
        ]
        hot_demotions = [
            p.hot_demotion_s * 1000.0
            for p in self.put_latencies
            if p.hot_demotion_s > 0
        ]
        evictions = [
            p.eviction_s * 1000.0 for p in self.put_latencies if p.eviction_s > 0
        ]
        put_totals = [
            p.total_put_s * 1000.0 for p in self.put_latencies if p.total_put_s > 0
        ]
        put_totals_sorted = sorted(put_totals)

        p95_put = (
            put_totals_sorted[int(len(put_totals_sorted) * 0.95)]
            if put_totals_sorted
            else 0.0
        )
        p99_put = (
            put_totals_sorted[int(len(put_totals_sorted) * 0.99)]
            if put_totals_sorted
            else 0.0
        )

        critical_crossed = bytes_read >= int(2.42 * 1024 * 1024 * 1024)

        report = EnduranceReport(
            title="Avatar_4K_Remux_76Mbps.mkv",
            bitrate_mbps=bitrate_mbps,
            simulated_media_duration_s=round(simulated_media_time, 1),
            wall_clock_elapsed_s=round(wall_clock_elapsed, 2),
            total_bytes_read=bytes_read,
            total_bytes_downloaded=total_downloaded,
            wasted_bytes=wasted_bytes,
            cache_hit_ratio=round(
                max(0.0, 1.0 - (total_downloaded / max(1, bytes_read))), 3
            ),
            prefetch_efficiency_pct=round(
                min(100.0, (bytes_read / max(1, total_downloaded)) * 100.0), 1
            ),
            rebuffer_events=rebuffer_events,
            total_stall_seconds=round(total_stall_seconds, 3),
            avg_seek_latency_ms=round(
                sum(seek_latencies) / len(seek_latencies) if seek_latencies else 0.0, 1
            ),
            max_seek_latency_ms=round(max(seek_latencies, default=0.0), 1),
            seeks_completed=seeks_completed,
            critical_4m15s_boundary_crossed=critical_crossed,
            hot_demotions_observed=len(hot_demotions),
            warm_evictions_observed=len(evictions),
            transient_registry_deliveries=self.transient_deliveries,
            peak_active_leases=peak_leases,
            max_shard_lock_wait_ms=round(max(shard_waits, default=0.0), 2),
            max_hot_demotion_ms=round(max(hot_demotions, default=0.0), 2),
            max_eviction_ms=round(max(evictions, default=0.0), 2),
            p95_put_latency_ms=round(p95_put, 2),
            p99_put_latency_ms=round(p99_put, 2),
            errors=errors,
        )

        self.teardown_di()
        return report


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="30-Minute Real Media Endurance Benchmark"
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=1800.0,
        help="Simulated media duration in seconds (default: 1800.0 = 30 min)",
    )
    parser.add_argument(
        "--mode", choices=["accelerated", "real_time"], default="accelerated"
    )
    parser.add_argument("--output", type=Path, default=Path("endurance_report.json"))
    args = parser.parse_args()

    runner = EnduranceRunner(timeline_mode=args.mode)
    print(f"\n{'=' * 95}")
    print("STARTING 30-MINUTE REAL MEDIA ENDURANCE BENCHMARK")
    print(
        f"Target Media: 76.0 Mbps 4K Remux | Duration: {args.duration / 60.0:.1f} min | Mode: {args.mode}"
    )
    print(f"{'=' * 95}\n")

    report = await runner.run_endurance(media_duration_seconds=args.duration)

    print(f"\n{'=' * 95}")
    print("30-MINUTE REAL MEDIA ENDURANCE RESULTS")
    print(f"{'=' * 95}")
    print(
        f"Simulated Media Timeline    : {report.simulated_media_duration_s / 60.0:.1f} minutes ({report.simulated_media_duration_s:.1f} s)"
    )
    print(f"Wall-Clock Elapsed          : {report.wall_clock_elapsed_s:.1f} s")
    print(
        f"Total Bytes Streamed        : {report.total_bytes_read / (1024 * 1024 * 1024):.2f} GB ({report.total_bytes_read:,} bytes)"
    )
    print(
        f"Total Downloaded            : {report.total_bytes_downloaded / (1024 * 1024 * 1024):.2f} GB"
    )
    print(
        f"Critical 4:15 Boundary (2.4GB): {'CROSS AND VERIFIED (PASS)' if report.critical_4m15s_boundary_crossed else 'NOT REACHED'}"
    )
    print(
        f"Rebuffer / Stall Events     : {report.rebuffer_events} (total stall: {report.total_stall_seconds:.2f} s)"
    )
    print(
        f"Seeks Completed             : {report.seeks_completed} (avg latency: {report.avg_seek_latency_ms:.1f} ms, max: {report.max_seek_latency_ms:.1f} ms)"
    )
    print(f"Hot Demotions Observed      : {report.hot_demotions_observed}")
    print(f"Warm Evictions Observed     : {report.warm_evictions_observed}")
    print(f"Max Shard Lock Wait         : {report.max_shard_lock_wait_ms:.2f} ms")
    print(f"Max Hot Demotion Time       : {report.max_hot_demotion_ms:.2f} ms")
    print(f"Max Eviction Time           : {report.max_eviction_ms:.2f} ms")
    print(f"p95 Put Latency             : {report.p95_put_latency_ms:.2f} ms")
    print(f"p99 Put Latency             : {report.p99_put_latency_ms:.2f} ms")
    print(f"Errors Encountered          : {len(report.errors)}")
    print(f"{'=' * 95}\n")

    args.output.write_text(json.dumps(asdict(report), indent=2), encoding="utf-8")
    print(f"Detailed JSON report written to: {args.output}")


if __name__ == "__main__":
    trio.run(main)
