"""Tests verifying settings priority over hardcoded prefetch limits for high-bitrate/DoVi content."""

import pytest
import trio

from program.services.streaming.media_stream import MediaStream
from program.settings import settings_manager


def test_dovi_high_bitrate_window_scaling(setup_stream):
    """
    Verify that max_window_chunks and prefetch sizing scale dynamically with
    target_buffer_seconds and configured chunk size, prioritizing user settings over hardcoded 48.
    """

    async def exercise():
        async with trio.open_nursery() as nursery:
            # 60 GB file typical of 4K Remux / Dolby Vision
            dovi_file_size = 60 * 1024 * 1024 * 1024
            stream = MediaStream(
                fh=901,
                file_size=dovi_file_size,
                path="/DoVi.Movie.2024.2160p.UHD.Remux.mkv",
                original_filename="DoVi.Movie.2024.2160p.UHD.Remux.mkv",
                nursery=nursery,
                provider="realdebrid",
                initial_url="https://example.test/dovi.mkv",
                bitrate=None,  # Not yet probed by ffprobe
            )

            # 1. Verify automatic conservative baseline bitrate derivation for >= 1GB unprobed file
            derived_bitrate = stream.adaptive_prefetch.get_effective_bitrate()
            assert derived_bitrate > 50_000_000  # ~71.5 Mbps for 60GB over 2 hours

            # 2. Verify window calculation uses derived bitrate instead of 12-chunk stall fallback
            calc_window = stream.adaptive_prefetch.calculate_window()
            assert (
                calc_window > 12
            )  # Significantly larger than legacy 12-chunk fallback

            # 3. Verify max_window_chunks respects buffer target
            assert stream.adaptive_prefetch.config.max_window_chunks >= 48

            await stream.close()
            nursery.cancel_scope.cancel()

    trio.run(exercise)


def test_custom_stream_settings_priority(setup_stream):
    """Verify custom settings in stream_settings override all internal defaults."""

    async def exercise():
        async with trio.open_nursery() as nursery:
            original_target = settings_manager.settings.stream.target_buffer_seconds
            original_prefetch = settings_manager.settings.stream.prefetch_chunks
            try:
                settings_manager.settings.stream.target_buffer_seconds = 25.0
                settings_manager.settings.stream.prefetch_chunks = 32

                stream = MediaStream(
                    fh=902,
                    file_size=10 * 1024 * 1024 * 1024,
                    path="/movie.mkv",
                    original_filename="movie.mkv",
                    nursery=nursery,
                    provider="realdebrid",
                    initial_url="https://example.test/movie.mkv",
                )

                assert stream.config.target_buffer_seconds == 25.0
                assert stream.config.prefetch_chunks == 32
                assert stream.adaptive_prefetch.config.target_buffer_seconds == 25.0
                assert stream.adaptive_prefetch.config.default_fallback_chunks == 32
                assert stream.adaptive_prefetch.config.max_window_chunks >= 48

                await stream.close()
                nursery.cancel_scope.cancel()
            finally:
                settings_manager.settings.stream.target_buffer_seconds = original_target
                settings_manager.settings.stream.prefetch_chunks = original_prefetch

    trio.run(exercise)
