"""
VFS Streaming Benchmark Suite for CineFlow (Riven Fork)
Measures streaming performance across Scenarios A-F:
- Scenario A: Low-bitrate 1080p stream (~2 Mbps, 0.25 MB/s)
- Scenario B: Medium-bitrate 1080p stream (~10 Mbps, 1.25 MB/s)
- Scenario C: High-bitrate 4K Remux (~80 Mbps, 10 MB/s)
- Scenario D: Sustained sequential playback (60s simulated media)
- Scenario E: Random seek playback (5 non-contiguous seeks)
- Scenario F: Multi-stream concurrency (2, 4, 8 concurrent streams)

Outputs structured metric tables comparing TTFB, rebuffer events,
cache hit ratio, prefetch efficiency, wasted bandwidth, and seek latency.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import httpx
import trio
from kink import di

# Ensure backend src is on sys.path
backend_src = Path(__file__).resolve().parent.parent / "src"
if str(backend_src) not in sys.path:
    sys.path.insert(0, str(backend_src))

from program.services.streaming.cache import Cache, CacheConfig
from program.services.streaming.chunker import ChunkCacheNotifier
from program.services.streaming.http_pool import TrioStreamingHttpPool
from program.services.streaming.media_stream import MediaStream


@dataclass
class ScenarioResult:
    scenario_id: str
    name: str
    bitrate_mbps: float
    total_bytes_read: int
    total_bytes_downloaded: int
    wasted_prefetch_bytes: int
    cache_hit_ratio: float
    ttfb_ms: float
    rebuffer_events: int
    total_stall_seconds: float
    avg_seek_latency_ms: float
    peak_active_leases: int
    prefetch_efficiency_pct: float
    duration_seconds: float
    custom_metrics: dict[str, Any] = field(default_factory=dict[str, Any])


class SimulatedNetworkResponse:
    """Simulates an HTTP streaming range response with controlled bandwidth and latency."""

    def __init__(
        self,
        url: str,
        start_byte: int,
        total_size: int,
        network_speed_bytes_sec: float,
        end_byte: int | None = None,
        latency_seconds: float = 0.015,
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

    async def aiter_raw(self, chunk_size: int = 1048576) -> AsyncIterator[bytes]:
        """Yield byte chunks at the simulated network throughput."""
        if self.latency > 0:
            await trio.sleep(self.latency)

        curr = self.start_byte
        max_limit = self.end_byte + 1
        while curr < max_limit and not self._is_closed:
            this_chunk_size = min(chunk_size, max_limit - curr)
            # Sleep duration to simulate network bandwidth
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

            # Generate synthetic deterministic data
            data = b"X" * this_chunk_size
            curr += this_chunk_size
            yield data

    async def aclose(self) -> None:
        self._is_closed = True


class BenchmarkRunner:
    def __init__(self, mode: str = "baseline"):
        self.mode = mode
        self.temp_dir = tempfile.mkdtemp(prefix=f"cineflow_vfs_bench_{mode}_")
        self.cache_dir = Path(self.temp_dir) / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def cleanup(self):
        try:
            shutil.rmtree(self.temp_dir, ignore_errors=True)
        except Exception:
            pass

    def setup_di(self, max_cache_mb: int = 1024):
        di[ChunkCacheNotifier] = ChunkCacheNotifier()
        di[Cache] = Cache(
            cfg=CacheConfig(
                cache_dir=self.cache_dir,
                max_size_bytes=max_cache_mb * 1024 * 1024,
                ttl_seconds=3600,
                eviction="LRU",
                metrics_enabled=True,
            )
        )

    def teardown_di(self):
        if ChunkCacheNotifier in di:
            del di[ChunkCacheNotifier]
        if Cache in di:
            del di[Cache]

    async def run_scenario(
        self,
        scenario_id: str,
        name: str,
        bitrate_mbps: float,
        file_size_gb: float,
        duration_seconds: float,
        seeks: list[tuple[float, int]] | None = None,
        concurrent_streams: int = 1,
        network_speed_mbps: float = 100.0,
    ) -> ScenarioResult:
        """Run a single benchmark scenario and measure performance metrics."""
        self.setup_di(max_cache_mb=1024)
        try:
            return await self._execute_scenario(
                scenario_id=scenario_id,
                name=name,
                bitrate_mbps=bitrate_mbps,
                file_size_bytes=int(file_size_gb * 1024 * 1024 * 1024),
                duration_seconds=duration_seconds,
                seeks=seeks or [],
                concurrent_streams=concurrent_streams,
                network_speed_bytes_sec=(network_speed_mbps * 1_000_000) / 8,
            )
        finally:
            self.teardown_di()

    async def _execute_scenario(
        self,
        scenario_id: str,
        name: str,
        bitrate_mbps: float,
        file_size_bytes: int,
        duration_seconds: float,
        seeks: list[tuple[float, int]],
        concurrent_streams: int,
        network_speed_bytes_sec: float,
    ) -> ScenarioResult:
        pool = TrioStreamingHttpPool()
        bytes_per_sec = (bitrate_mbps * 1_000_000) / 8
        read_block_size = 256 * 1024  # Standard media player 256 KB read block
        read_interval = read_block_size / bytes_per_sec

        cancellation_trackers: list[dict[str, int]] = [
            {} for _ in range(concurrent_streams)
        ]
        peak_leases = 0

        async def _mock_establish(
            stream_inst: Any, start: int, end: int | None, stream_idx: int
        ):
            nonlocal peak_leases
            curr_leases = pool.active_leases
            peak_leases = max(peak_leases, curr_leases)

            gen_val = getattr(
                getattr(stream_inst, "adaptive_prefetch", None), "current_generation", 0
            )
            resp = SimulatedNetworkResponse(
                url=f"https://cdn.example.com/media_{stream_idx}.mkv",
                start_byte=start,
                end_byte=end,
                total_size=file_size_bytes,
                network_speed_bytes_sec=network_speed_bytes_sec,
                latency_seconds=0.010,
                cancellation_tracker=cancellation_trackers[stream_idx],
                generation_id=str(gen_val),
            )
            return resp

        # Stream metrics across all simulated clients
        stream_results: list[dict[str, Any]] = []

        async with trio.open_nursery() as nursery:

            async def _run_single_client(stream_idx: int):
                stream_fh = stream_idx + 1
                bitrate_arg = (
                    int(bitrate_mbps * 1_000_000) if self.mode == "adaptive" else None
                )
                stream = MediaStream(
                    fh=cast(Any, stream_fh),
                    file_size=file_size_bytes,
                    path=f"/media/movie_{stream_idx}.mkv",
                    original_filename=f"movie_{stream_idx}.mkv",
                    nursery=nursery,
                    provider="realdebrid",
                    initial_url=f"https://cdn.example.com/media_{stream_idx}.mkv",
                    http_pool=pool,
                    require_mount_http_pool=True,
                    bitrate=bitrate_arg,
                )

                if self.mode == "baseline":
                    # In baseline mode, emulate legacy static 12-chunk window and no seek cancellation
                    stream.adaptive_prefetch.calculate_window = lambda **kwargs: 12  # type: ignore[method-assign]
                    stream.adaptive_prefetch.should_abort_prefetch = (
                        lambda prefetch_generation: False
                    )  # type: ignore[method-assign]

                # Monkey-patch establish_connection to return simulated network response
                from contextlib import asynccontextmanager

                @asynccontextmanager
                async def custom_establish(start: int = 0, *, end: int | None = None):
                    from program.services.streaming.http_pool import (
                        admit_stream_request,
                    )

                    async with admit_stream_request("body"):
                        resp = await _mock_establish(stream, start, end, stream_idx)
                        try:
                            yield resp
                        finally:
                            await resp.aclose()

                stream.establish_connection = custom_establish  # type: ignore[assignment]

                # Simulate media consumption
                ttfb_ms = 0.0
                rebuffer_events = 0
                total_stall_seconds = 0.0
                bytes_read = 0
                seek_latencies: list[float] = []
                total_reads = 0

                curr_pos = 0  # Standard player starts at byte 0 (header)
                start_clock = time.monotonic()
                seeks_sorted = sorted(seeks, key=lambda s: s[0])
                seek_idx = 0

                is_first_read = True

                while time.monotonic() - start_clock < duration_seconds:
                    elapsed = time.monotonic() - start_clock

                    # Check for scheduled seek
                    if (
                        seek_idx < len(seeks_sorted)
                        and elapsed >= seeks_sorted[seek_idx][0]
                    ):
                        _seek_time, target_offset = seeks_sorted[seek_idx]
                        seek_idx += 1
                        curr_pos = target_offset

                        # Measure seek latency
                        s_start = time.monotonic()
                        # Player issues first read after seek
                        data = await stream.read(
                            request_start=curr_pos,
                            request_end=curr_pos + read_block_size - 1,
                            request_size=read_block_size,
                        )
                        s_latency = (time.monotonic() - s_start) * 1000.0
                        seek_latencies.append(s_latency)

                        bytes_read += len(data)
                        curr_pos += len(data)
                        total_reads += 1
                        continue

                    # Regular playback read
                    r_start = time.monotonic()
                    data = await stream.read(
                        request_start=curr_pos,
                        request_end=curr_pos + read_block_size - 1,
                        request_size=read_block_size,
                    )
                    r_duration = time.monotonic() - r_start

                    if is_first_read:
                        ttfb_ms = r_duration * 1000.0
                        is_first_read = False
                    # If read took longer than 150ms and wasn't instantaneous from cache, count stall
                    elif r_duration > 0.150:
                        rebuffer_events += 1
                        total_stall_seconds += r_duration

                    bytes_read += len(data)
                    curr_pos += len(data)
                    total_reads += 1

                    # Paced consumption (sleep until next frame/block needs to be consumed)
                    sleep_needed = read_interval - r_duration
                    if sleep_needed > 0:
                        await trio.sleep(sleep_needed)

                # Close stream
                await stream.close()

                stream_results.append(
                    {
                        "bytes_read": bytes_read,
                        "ttfb_ms": ttfb_ms,
                        "rebuffer_events": rebuffer_events,
                        "total_stall_seconds": total_stall_seconds,
                        "seek_latencies": seek_latencies,
                        "total_reads": total_reads,
                    }
                )

            # Run clients concurrently
            async with trio.open_nursery() as client_nursery:
                for idx in range(concurrent_streams):
                    client_nursery.start_soon(_run_single_client, idx)

        await pool.teardown()

        # Aggregate metrics
        total_read = sum(r["bytes_read"] for r in stream_results)
        total_downloaded = sum(
            t.get("bytes_transferred", 0) for t in cancellation_trackers
        )
        wasted_bytes = max(0, total_downloaded - total_read)
        avg_ttfb = (
            sum(r["ttfb_ms"] for r in stream_results) / len(stream_results)
            if stream_results
            else 0.0
        )
        total_rebuffers = sum(r["rebuffer_events"] for r in stream_results)
        total_stalls = sum(r["total_stall_seconds"] for r in stream_results)
        all_seek_lats = [lat for r in stream_results for lat in r["seek_latencies"]]
        avg_seek_lat = sum(all_seek_lats) / len(all_seek_lats) if all_seek_lats else 0.0
        prefetch_efficiency = (
            (total_read / total_downloaded * 100.0) if total_downloaded > 0 else 100.0
        )
        cache_hit_ratio = (
            max(0.0, 1.0 - (total_downloaded / max(1, total_read)))
            if total_read > total_downloaded
            else 0.0
        )

        return ScenarioResult(
            scenario_id=scenario_id,
            name=name,
            bitrate_mbps=bitrate_mbps,
            total_bytes_read=total_read,
            total_bytes_downloaded=total_downloaded,
            wasted_prefetch_bytes=wasted_bytes,
            cache_hit_ratio=cache_hit_ratio,
            ttfb_ms=round(avg_ttfb, 2),
            rebuffer_events=total_rebuffers,
            total_stall_seconds=round(total_stalls, 3),
            avg_seek_latency_ms=round(avg_seek_lat, 2),
            peak_active_leases=peak_leases,
            prefetch_efficiency_pct=round(min(100.0, prefetch_efficiency), 1),
            duration_seconds=duration_seconds,
            custom_metrics={"concurrent_streams": concurrent_streams},
        )


async def run_benchmark_suite(mode: str = "baseline") -> list[ScenarioResult]:
    """Execute Scenarios A through F."""
    runner = BenchmarkRunner(mode=mode)
    results: list[ScenarioResult] = []

    try:
        # Scenario A: Low-bitrate 1080p stream (~2 Mbps, 0.25 MB/s)
        res_a = await runner.run_scenario(
            scenario_id="A",
            name="Low-Bitrate 1080p (2 Mbps)",
            bitrate_mbps=2.0,
            file_size_gb=1.5,
            duration_seconds=10.0,
        )
        results.append(res_a)

        # Scenario B: Medium-bitrate 1080p stream (~10 Mbps, 1.25 MB/s)
        res_b = await runner.run_scenario(
            scenario_id="B",
            name="Medium-Bitrate 1080p (10 Mbps)",
            bitrate_mbps=10.0,
            file_size_gb=7.5,
            duration_seconds=10.0,
        )
        results.append(res_b)

        # Scenario C: High-bitrate 4K Remux (~80 Mbps, 10 MB/s)
        res_c = await runner.run_scenario(
            scenario_id="C",
            name="High-Bitrate 4K Remux (80 Mbps)",
            bitrate_mbps=80.0,
            file_size_gb=60.0,
            duration_seconds=10.0,
        )
        results.append(res_c)

        # Scenario D: Sustained Sequential Playback (20s)
        res_d = await runner.run_scenario(
            scenario_id="D",
            name="Sustained Sequential Playback",
            bitrate_mbps=25.0,
            file_size_gb=20.0,
            duration_seconds=15.0,
        )
        results.append(res_d)

        # Scenario E: Random Seek Playback (5 seeks)
        seeks = [
            (2.0, 500 * 1024 * 1024),
            (4.0, 1500 * 1024 * 1024),
            (6.0, 2500 * 1024 * 1024),
            (8.0, 800 * 1024 * 1024),
            (10.0, 3000 * 1024 * 1024),
        ]
        res_e = await runner.run_scenario(
            scenario_id="E",
            name="Random Seek Playback (5 seeks)",
            bitrate_mbps=20.0,
            file_size_gb=30.0,
            duration_seconds=12.0,
            seeks=seeks,
        )
        results.append(res_e)

        # Scenario F: Multi-Stream Concurrency (4 concurrent streams)
        res_f = await runner.run_scenario(
            scenario_id="F",
            name="Multi-Stream Concurrency (4 streams)",
            bitrate_mbps=15.0,
            file_size_gb=10.0,
            duration_seconds=10.0,
            concurrent_streams=4,
        )
        results.append(res_f)

    finally:
        runner.cleanup()

    return results


def print_results_table(mode: str, results: list[ScenarioResult]) -> None:
    print(f"\n{'=' * 95}")
    print(f"CINEFLOW VFS STREAMING BENCHMARK RESULTS ({mode.upper()})")
    print(f"{'=' * 95}")
    print(
        f"{'ID':<3} | {'Scenario':<30} | {'Read (MB)':<9} | {'DL (MB)':<8} | {'Waste':<8} | {'TTFB(ms)':<8} | {'Rebuf':<5} | {'Seek(ms)':<8} | {'Eff(%)':<6}"
    )
    print(f"{'-' * 95}")
    for r in results:
        read_mb = r.total_bytes_read / (1024 * 1024)
        dl_mb = r.total_bytes_downloaded / (1024 * 1024)
        waste_mb = r.wasted_prefetch_bytes / (1024 * 1024)
        print(
            f"{r.scenario_id:<3} | {r.name:<30} | {read_mb:>9.2f} | {dl_mb:>8.2f} | {waste_mb:>8.2f} | {r.ttfb_ms:>8.1f} | {r.rebuffer_events:>5} | {r.avg_seek_latency_ms:>8.1f} | {r.prefetch_efficiency_pct:>6.1f}"
        )
    print(f"{'=' * 95}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CineFlow VFS Benchmark")
    parser.add_argument("--mode", choices=["baseline", "adaptive"], default="baseline")
    args = parser.parse_args()

    results = trio.run(run_benchmark_suite, args.mode)
    print_results_table(args.mode, results)
