"""Run the backend quality checks required before pushing and in CI."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CI_UNIT_TESTS = (
    "src/tests/test_security_phase1.py",
    "src/tests/test_security_phase3.py",
    "src/tests/test_event_manager.py",
    "src/tests/test_hls_params.py",
    "src/tests/test_scraper_episode_season_match.py",
    "src/tests/test_downloader_cooldown.py",
    "src/tests/test_downloader_existing_filesystem_entry.py",
    "src/tests/test_blacklist_idempotent.py",
    "src/tests/test_scraper_base.py",
    "src/tests/test_scraper_language_settings.py",
    "src/tests/test_utils.py",
    "src/tests/test_tmdb_proxy.py",
    "src/tests/test_ranking.py",
    "src/tests/test_ranking_descriptions.py",
    "src/tests/test_ranking_studio_p0.py",
    "src/tests/test_ranking_scrape_quality_p1.py",
    "src/tests/test_mediaitem_runtime.py",
    "src/tests/test_async_client.py",
    "src/tests/test_http_pool_heal.py",
    "src/tests/test_media_stream_pool.py",
    "src/tests/test_opensubtitles_settings.py",
    "src/tests/test_subtitle_title_guard.py",
    "src/tests/test_subdl_provider.py",
    "src/tests/test_debrid_cdn_url.py",
    "src/tests/test_vfs_dead_link_rescrape.py",
    "src/tests/test_realdebrid_transient.py",
    "src/tests/test_realdebrid_fair_usage.py",
    "src/tests/test_requests.py",
    "src/tests/test_incremental_parse_results.py",
    "src/tests/test_scrape_funnel.py",
    "src/tests/test_ranking_soft_opt_in.py",
    "src/tests/test_anime_ranking_settings.py",
    "src/tests/test_per_library_ranking_packs.py",
    "src/tests/test_stats_needs_attention.py",
    "src/tests/test_settings_connection.py",
    "src/tests/test_settings_connection_route.py",
    "src/tests/test_stremthru_scraper.py",
    "src/tests/test_torrent_utils.py",
    "src/tests/test_zilean_scraper.py",
    "src/tests/test_remake_aliases_scrape.py",
    "src/tests/test_program_filesystem.py",
    "src/tests/test_alldebrid_downloader.py",
    "src/tests/test_stream_read_type.py",
    "src/tests/test_stream_tracing_sample.py",
    "src/tests/test_streaming_cache.py",
    "src/tests/test_cache_sizing.py",
    "src/tests/test_cache_two_tier.py",
    "src/tests/test_stream_prefetch.py",
    "src/tests/test_stream_tolerance_settings.py",
    "src/tests/test_prom_cache_metrics.py",
    "src/tests/test_rivenvfs.py",
    "src/tests/test_trakt_client_id.py",
    "src/tests/test_trakt_oauth_refresh.py",
    "src/tests/test_trakt_extract_ids.py",
    "src/tests/test_plex_trakt_history.py",
    "src/tests/test_phase2c_dep_smoke.py",
    "src/tests/test_guessit_subliminal_harness.py",
    "src/tests/test_entrypoint.py",
    "src/tests/test_main_startup.py",
    "src/tests/test_db_startup.py",
    "src/tests/test_hls_process_cleanup.py",
    "src/tests/test_debrid_matching.py",
    "src/tests/test_settings_migration.py",
    "src/tests/test_prowlarr_concurrency.py",
)


def run_check(name: str, command: Sequence[str]) -> int:
    """Run one quality check and preserve its failure code."""
    print(f"\n==> {name}")
    return subprocess.run(command, cwd=ROOT, check=False).returncode


def main() -> int:
    checks = (
        ("uv.lock is in sync", ("uv", "lock", "--check")),
        ("Ruff", ("uv", "run", "ruff", "check", "src")),
        ("Pyright", ("uv", "run", "--with", "pyright==1.1.400", "pyright")),
        ("CI unit tests", ("uv", "run", "pytest", "-q", *CI_UNIT_TESTS)),
    )
    for name, command in checks:
        if returncode := run_check(name, command):
            return returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
