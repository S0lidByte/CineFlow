"""Phase 3: incremental scrape_streaming parse equivalence tests."""

from __future__ import annotations

from program.services.scrapers.shared import merge_parse_results, parse_results
from program.settings import settings_manager


class DummyItem:
    top_title = "Stargate: Continuum"
    log_string = "Stargate: Continuum"
    country = None
    is_anime = False
    aired_at = None

    @staticmethod
    def get_aliases():
        return {}


def test_merge_parse_results_matches_full_parse_results():
    batch_a = {
        "a" * 40: "Stargate Continuum 2008 1080p BluRay x264-OFT",
    }
    batch_b = {
        "b" * 40: "Stargate Continuum 2008 720p BluRay x264-GROUP",
        "a" * 40: "Stargate Continuum 2008 1080p BluRay x264-OFT",  # duplicate
    }
    combined = {**batch_a, **batch_b}

    with settings_manager.override(languages={"required": []}):
        full = parse_results(DummyItem(), combined, manual=True)

        torrents = set()
        processed = set[str]()
        incremental = merge_parse_results(
            DummyItem(), batch_a, torrents, processed, manual=True
        )
        incremental = merge_parse_results(
            DummyItem(),
            {k: v for k, v in batch_b.items() if k not in processed},
            torrents,
            processed,
            manual=True,
        )

    assert set(incremental) == set(full)
    assert len(processed) >= len(full)
    # Ranking order should match for the shared keys
    assert list(incremental.keys()) == list(full.keys())


def test_merge_parse_results_skips_already_processed_hashes():
    results = {
        "c" * 40: "Stargate Continuum 2008 1080p BluRay x264-OFT",
    }

    with settings_manager.override(languages={"required": []}):
        torrents = set()
        processed = set[str]()
        first = merge_parse_results(
            DummyItem(), results, torrents, processed, manual=True
        )
        torrent_count = len(torrents)
        second = merge_parse_results(
            DummyItem(), results, torrents, processed, manual=True
        )

    assert first == second
    assert len(torrents) == torrent_count


def test_conflicting_infohash_titles_first_wins():
    """scrape_streaming must keep the first title for a duplicate infohash."""
    infohash = "d" * 40
    first_title = "Stargate Continuum 2008 1080p BluRay x264-OFT"
    conflict_title = "Totally Different Title 480p CAM"

    all_raw_results: dict[str, str] = {}
    batches = [
        {infohash: first_title},
        {infohash: conflict_title},
    ]

    torrents = set()
    processed = set[str]()
    with settings_manager.override(languages={"required": []}):
        for raw_results in batches:
            delta_results = {
                h: title
                for h, title in raw_results.items()
                if h not in all_raw_results
            }
            all_raw_results.update(delta_results)
            merge_parse_results(
                DummyItem(), delta_results, torrents, processed, manual=True
            )

    assert all_raw_results[infohash] == first_title
    assert infohash in processed
    # Conflicting title must not create a second ranking pass
    assert len(processed) == 1
