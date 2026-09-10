"""Tests for Prowlarr scraper infohash fetch concurrency capping and logging."""

import threading
import time

import pytest
from pydantic import ValidationError

from program.media.item import Movie
from program.services.scrapers.prowlarr import (
    Capabilities,
    Indexer,
    Prowlarr,
    SearchParams,
)
from program.settings.models import ProwlarrConfig


def test_prowlarr_config_defaults_and_bounds():
    """Verify default and validation bounds for max_concurrent_infohash_fetches."""
    config = ProwlarrConfig()
    assert config.max_concurrent_infohash_fetches == 10

    custom = ProwlarrConfig(max_concurrent_infohash_fetches=5)
    assert custom.max_concurrent_infohash_fetches == 5

    with pytest.raises(ValidationError):
        ProwlarrConfig(max_concurrent_infohash_fetches=0)

    with pytest.raises(ValidationError):
        ProwlarrConfig(max_concurrent_infohash_fetches=51)


def test_prowlarr_bounded_semaphore_initialization(monkeypatch):
    """Verify BoundedSemaphore capacity matches configuration."""
    prowlarr = Prowlarr()
    prowlarr.settings.max_concurrent_infohash_fetches = 4
    prowlarr._infohash_semaphore = None

    sem = prowlarr._get_infohash_semaphore()
    assert isinstance(sem, threading.BoundedSemaphore)

    # Acquire 4 times successfully, 5th acquire with timeout=0 should fail
    for _ in range(4):
        assert sem.acquire(blocking=False)
    assert not sem.acquire(blocking=False)

    # Release all 4
    for _ in range(4):
        sem.release()


def test_prowlarr_concurrency_capping_and_exception_safety(monkeypatch):
    """Verify max concurrent infohash fetches cannot exceed capacity, and errors release semaphore."""
    prowlarr = Prowlarr()
    prowlarr.settings.max_concurrent_infohash_fetches = 2
    prowlarr._infohash_semaphore = None

    active_counter = 0
    max_observed_active = 0
    lock = threading.Lock()

    def mock_get_infohash_from_url(url, session=None, timeout=10.0):
        nonlocal active_counter, max_observed_active
        with lock:
            active_counter += 1
            max_observed_active = max(max_observed_active, active_counter)
        time.sleep(0.02)
        with lock:
            active_counter -= 1

        if "raise_err" in url:
            raise RuntimeError("simulated network error")
        if "hash" in url:
            return "0123456789abcdef0123456789abcdef01234567"
        return None

    monkeypatch.setattr(prowlarr, "get_infohash_from_url", mock_get_infohash_from_url)

    urls = [
        "http://prowlarr/download/1?hash=1",
        "http://prowlarr/download/2?raise_err=1",
        "http://prowlarr/download/3?hash=2",
        "http://prowlarr/download/4?hash=3",
        "http://prowlarr/download/5?raise_err=2",
    ]

    threads = []
    results = []
    errors = []

    def worker(u):
        try:
            results.append(prowlarr._fetch_infohash_bounded(u))
        except RuntimeError as exc:
            errors.append(exc)

    for u in urls:
        t = threading.Thread(target=worker, args=(u,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    assert max_observed_active <= 2
    assert len(errors) == 2
    assert len(results) == 3
    # Verify semaphore was fully released (we can acquire cap times)
    sem = prowlarr._get_infohash_semaphore()
    for _ in range(2):
        assert sem.acquire(blocking=False)
    for _ in range(2):
        sem.release()


def test_prowlarr_scrape_indexer_deduplicates_urls_and_logs(monkeypatch):
    """Verify scrape_indexer deduplicates download URLs and emits structured log summary."""
    prowlarr = Prowlarr()
    prowlarr.settings.max_concurrent_infohash_fetches = 5
    prowlarr._infohash_semaphore = None

    logged_messages = []

    def mock_debug(msg, *args, **kwargs):
        logged_messages.append(msg)

    monkeypatch.setattr("program.services.scrapers.prowlarr.logger.debug", mock_debug)

    class MockResponse:
        ok = True
        status_code = 200

        def json(self):
            return [
                {
                    "title": "Movie 1",
                    "info_hash": None,
                    "guid": None,
                    "download_url": "http://prowlarr/download/same_url",
                },
                {
                    "title": "Movie 1 Duplicate Entry",
                    "info_hash": None,
                    "guid": None,
                    "download_url": "http://prowlarr/download/same_url",
                },
                {
                    "title": "Movie 2",
                    "info_hash": None,
                    "guid": None,
                    "download_url": "http://prowlarr/download/unique_url",
                },
            ]

    class MockSession:
        def get(self, url, params=None, timeout=30, headers=None):
            return MockResponse()

    prowlarr.session = MockSession()
    prowlarr._infohash_session = MockSession()

    def mock_fetch_bounded(url, session=None, timeout=20.0, deadline=None):
        if "same_url" in url:
            return "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        return "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

    monkeypatch.setattr(prowlarr, "_fetch_infohash_bounded", mock_fetch_bounded)

    indexer = Indexer(
        id=1,
        name="TestIndexer",
        enable=True,
        protocol="torrent",
        capabilities=Capabilities(
            supports_raw_search=True,
            categories=[],
            search_params=SearchParams(search=[], movie=[], tv=[]),
        ),
    )

    item = Movie({"title": "Test Movie", "year": 2024})

    monkeypatch.setattr(
        prowlarr,
        "build_search_params",
        lambda _indexer, _item: type("Params", (), {"model_dump": lambda self: {}})(),
    )

    res = prowlarr.scrape_indexer(indexer, item)
    assert len(res) == 2
    assert "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" in res
    assert "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" in res

    # Verify structured summary log exists
    summary_log = next(
        (m for m in logged_messages if "infohash resolution complete" in m), None
    )
    assert summary_log is not None
    assert "urls_fetched=2" in summary_log
    assert "resolved=2" in summary_log
    assert "failed=0" in summary_log
    assert "timed_out=0" in summary_log


def test_prowlarr_lazy_session_initialization(monkeypatch):
    """Verify get_indexers and scrape_indexer lazily initialize session without assertion error."""
    prowlarr = Prowlarr()
    prowlarr.session = None

    created = False

    class MockCreatedSession:
        def get(self, url, params=None, timeout=30, headers=None):
            class Resp:
                ok = True

                def json(self):
                    return []

            return Resp()

    def mock_create_session():
        nonlocal created
        created = True
        return MockCreatedSession()

    monkeypatch.setattr(prowlarr, "_create_session", mock_create_session)

    # get_indexers should create session if None
    indexers = prowlarr.get_indexers()
    assert created
    assert prowlarr.session is not None
    assert indexers == []


def test_prowlarr_thread_safe_indexer_removal_on_error(monkeypatch):
    """Verify failed indexers are removed safely under concurrency without raising ValueError."""
    prowlarr = Prowlarr()

    class FailingSession:
        def get(self, url, params=None, timeout=30, headers=None):
            class Resp:
                ok = False
                status_code = 500

                def json(self):
                    return {"message": "Internal Server Error"}

            return Resp()

    prowlarr.session = FailingSession()

    indexer1 = Indexer(
        id=1,
        name="Fail1",
        enable=True,
        protocol="torrent",
        capabilities=Capabilities(
            supports_raw_search=True,
            categories=[],
            search_params=SearchParams(search=[], movie=[], tv=[]),
        ),
    )
    indexer2 = Indexer(
        id=2,
        name="Fail2",
        enable=True,
        protocol="torrent",
        capabilities=Capabilities(
            supports_raw_search=True,
            categories=[],
            search_params=SearchParams(search=[], movie=[], tv=[]),
        ),
    )

    prowlarr.indexers = [indexer1, indexer2]

    item = Movie({"title": "Test Movie", "year": 2024})
    monkeypatch.setattr(
        prowlarr,
        "build_search_params",
        lambda _indexer, _item: type("Params", (), {"model_dump": lambda self: {}})(),
    )

    # Scrape both simultaneously
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(prowlarr.scrape_indexer, indexer1, item)
        f2 = ex.submit(prowlarr.scrape_indexer, indexer2, item)
        res1 = f1.result()
        res2 = f2.result()

    assert res1 == {}
    assert res2 == {}
    assert prowlarr.indexers == []


def test_prowlarr_scrape_returns_at_indexer_deadline(monkeypatch):
    """A slow worker must not hold the outer scrape past its time budget."""
    prowlarr = Prowlarr()
    prowlarr.timeout = 0.05
    indexer = Indexer(
        id=1,
        name="SlowIndexer",
        enable=True,
        protocol="torrent",
        capabilities=Capabilities(
            supports_raw_search=True,
            categories=[],
            search_params=SearchParams(search=[], movie=[], tv=[]),
        ),
    )
    prowlarr.indexers = [indexer]

    started = threading.Event()
    release = threading.Event()

    def slow_scrape_indexer(_indexer, _item, deadline=None):
        assert deadline is not None
        started.set()
        release.wait(timeout=1)
        return {}

    monkeypatch.setattr(prowlarr, "scrape_indexer", slow_scrape_indexer)
    monkeypatch.setattr(prowlarr, "_periodic_indexer_scan", lambda: None)
    monkeypatch.setattr(
        "program.services.scrapers.prowlarr.logger.log", lambda *_: None
    )
    item = Movie({"title": "Test Movie", "year": 2024})

    start = time.monotonic()
    assert prowlarr.scrape(item) == {}
    elapsed = time.monotonic() - start
    release.set()

    assert started.is_set()
    assert elapsed < 0.15


def test_prowlarr_infohash_fetch_skips_expired_deadline(monkeypatch):
    """Expired indexer budgets must not acquire a URL-fetch worker or issue I/O."""
    prowlarr = Prowlarr()
    invoked = False

    def mock_get_infohash_from_url(*_args, **_kwargs):
        nonlocal invoked
        invoked = True
        return "0123456789abcdef0123456789abcdef01234567"

    monkeypatch.setattr(prowlarr, "get_infohash_from_url", mock_get_infohash_from_url)

    assert (
        prowlarr._fetch_infohash_bounded(
            "https://example.invalid/download",
            deadline=time.monotonic() - 1,
        )
        is None
    )
    assert not invoked
