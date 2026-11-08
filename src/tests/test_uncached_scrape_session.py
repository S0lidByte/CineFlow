"""Focused regression tests for uncached manual scraping provider selection and circuit breaker handling."""

from __future__ import annotations

import hashlib
import hmac
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from program.contracts.operation_ledger import OperationLedger
from program.db.base_model import get_base_metadata
from program.media.item import Movie
from program.services.downloaders.alldebrid import AllDebridDownloader
from program.services.downloaders.models import (
    DebridFile,
    TorrentContainer,
    TorrentFile,
    TorrentInfo,
)
from program.services.downloaders.realdebrid import (
    RealDebridDownloader,
    RealDebridTransientError,
)
from program.services.downloaders.realdebrid_transfers import rd_transfers_service
from program.utils.request import CircuitBreakerOpen
from routers import app_router

TEST_BFF_API_KEY = "test-bff-service-key"
TEST_ACTOR_SECRET = "test-actor-context-secret"


def _signed_bff_headers(roles: str = "library:read,media:request") -> dict[str, str]:
    actor_id = "scrape-session-tester"
    actor_client = "cineflow-web-bff"
    timestamp = str(int(datetime.now(UTC).timestamp()))
    payload = json.dumps(
        {
            "actor_id": actor_id,
            "actor_roles": roles,
            "actor_client": actor_client,
            "actor_timestamp": timestamp,
        },
        separators=(",", ":"),
    ).encode()
    signature = hmac.new(
        TEST_ACTOR_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    return {
        "x-api-key": TEST_BFF_API_KEY,
        "x-actor-id": actor_id,
        "x-actor-roles": roles,
        "x-actor-client": actor_client,
        "x-actor-timestamp": timestamp,
        "x-actor-signature": signature,
    }


class FakeDownloaderHolder:
    def __init__(self, services_dict, initialized_services):
        self.services = services_dict
        self.initialized_services = initialized_services
        self.service = initialized_services[0] if initialized_services else None
        self._service_cooldowns = {}


@pytest.fixture
def scrape_client(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    get_base_metadata().create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    @contextmanager
    def _test_session():
        session = TestingSession()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr("routers.secure.scrape.db_session", _test_session)
    monkeypatch.setattr(
        "program.services.downloaders.realdebrid_transfers.db_session", _test_session
    )
    monkeypatch.setattr("program.contracts.dispatcher.db_session", _test_session)
    monkeypatch.setenv("BFF_API_KEY", TEST_BFF_API_KEY)
    monkeypatch.setenv("ACTOR_CONTEXT_SECRET", TEST_ACTOR_SECRET)

    rd_transfers_service.invalidate_cache()

    app = FastAPI()
    app.include_router(app_router)
    client = TestClient(app)
    return client, TestingSession


def _make_torrent_info(
    tid: str, name: str = "Test.Movie.2026.mkv", status: str = "downloading"
):
    return TorrentInfo(
        id=tid,
        name=name,
        status=status,
        infohash="1122334455667788990011223344556677889900",
        bytes=1000000000,
        created_at=datetime.now(UTC),
        alternative_filename=name,
        progress=25.0,
        files={
            1: TorrentFile(
                id=1,
                path=f"/{name}",
                bytes=1000000000,
                selected=1,
                download_url="",
            )
        },
        links=[],
    )


def test_uncached_multiple_services_submits_and_retrieves_via_realdebrid(
    scrape_client, monkeypatch
):
    """Uncached result with multiple configured downloaders submits and retrieves metadata using Real-Debrid."""
    client, session_factory = scrape_client

    with session_factory() as session:
        movie = Movie({"title": "Multi Service Movie", "year": 2026})
        movie.id = 701
        session.add(movie)
        session.commit()

    # Mock AllDebrid as primary service, RealDebrid as secondary
    mock_ad = MagicMock()
    mock_ad.key = "alldebrid"
    mock_ad.initialized = True

    mock_rd = MagicMock()
    mock_rd.key = "realdebrid"
    mock_rd.initialized = True
    mock_rd.add_torrent.return_value = "rd-uncached-701"
    mock_rd.get_torrent_info.return_value = _make_torrent_info("rd-uncached-701")
    mock_rd._fair_usage_remaining.return_value = 0.0
    mock_rd.api.session.breakers = {}

    downloader = FakeDownloaderHolder(
        services_dict={AllDebridDownloader: mock_ad, RealDebridDownloader: mock_rd},
        initialized_services=[mock_ad, mock_rd],
    )

    mock_prog = MagicMock()
    mock_prog.services.downloader = downloader
    monkeypatch.setattr(
        "routers.secure.scrape.di",
        {__import__("program.program").program.Program: mock_prog},
    )

    # Uncached availability miss
    monkeypatch.setattr(
        "routers.secure.scrape.resolve_torrent_container",
        AsyncMock(return_value=(None, "No files found in torrent", None)),
    )

    test_hash = "1122334455667788990011223344556677889900"
    magnet = f"magnet:?xt=urn:btih:{test_hash}&dn=Multi.Service.Movie.mkv"

    res = client.post(
        "/api/v1/scrape/start_session",
        params={"item_id": 701, "allow_uncached": True, "magnet": magnet},
        headers=_signed_bff_headers(),
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["torrent_id"] == "rd-uncached-701"

    # Verify Real-Debrid was called with preserved original magnet URI
    mock_rd.add_torrent.assert_called_once_with(magnet)
    # AllDebrid must NOT be touched for RD uncached submission or metadata
    mock_ad.add_torrent.assert_not_called()
    mock_ad.get_torrent_info.assert_not_called()


def test_open_realdebrid_breaker_no_additional_submission(scrape_client, monkeypatch):
    """OPEN Real-Debrid breaker prevents additional submissions and returns retryable 503."""
    client, session_factory = scrape_client

    with session_factory() as session:
        movie = Movie({"title": "Breaker Movie", "year": 2026})
        movie.id = 702
        session.add(movie)
        session.commit()

    mock_rd = MagicMock()
    mock_rd.key = "realdebrid"
    mock_rd.initialized = True

    # Breaker is OPEN
    mock_breaker = MagicMock()
    mock_breaker.check_available.side_effect = CircuitBreakerOpen(
        "api.real-debrid.com", retry_after_seconds=42.0
    )
    mock_rd.api.session.breakers = {"api.real-debrid.com": mock_breaker}

    downloader = FakeDownloaderHolder(
        services_dict={RealDebridDownloader: mock_rd},
        initialized_services=[mock_rd],
    )

    mock_prog = MagicMock()
    mock_prog.services.downloader = downloader
    monkeypatch.setattr(
        "routers.secure.scrape.di",
        {__import__("program.program").program.Program: mock_prog},
    )

    monkeypatch.setattr(
        "routers.secure.scrape.resolve_torrent_container",
        AsyncMock(return_value=(None, "No files found in torrent", None)),
    )

    test_hash = "2233445566778899001122334455667788990011"
    magnet = f"magnet:?xt=urn:btih:{test_hash}&dn=Breaker.Movie.mkv"

    res = client.post(
        "/api/v1/scrape/start_session",
        params={"item_id": 702, "allow_uncached": True, "magnet": magnet},
        headers=_signed_bff_headers(),
    )
    assert res.status_code == 503
    assert res.headers.get("retry-after") == "42"
    assert "circuit breaker is open" in res.json()["detail"].lower()

    # Crucial: NO submission attempted
    mock_rd.add_torrent.assert_not_called()


def test_transient_429_503_classified_without_blacklisting(scrape_client, monkeypatch):
    """HTTP 429/503 from Real-Debrid is classified as transient retryable without blacklisting."""
    client, session_factory = scrape_client

    with session_factory() as session:
        movie = Movie({"title": "Rate Limit Movie", "year": 2026})
        movie.id = 703
        session.add(movie)
        session.commit()

    mock_rd = MagicMock()
    mock_rd.key = "realdebrid"
    mock_rd.initialized = True
    mock_rd.api.session.breakers = {}
    mock_rd._fair_usage_remaining.return_value = 0.0

    # Simulate 429 during add_torrent
    mock_rd.add_torrent.side_effect = RealDebridTransientError(
        "[429] Too Many Requests", status_code=429, retry_after_seconds=60.0
    )

    downloader = FakeDownloaderHolder(
        services_dict={RealDebridDownloader: mock_rd},
        initialized_services=[mock_rd],
    )
    mock_prog = MagicMock()
    mock_prog.services.downloader = downloader
    monkeypatch.setattr(
        "routers.secure.scrape.di",
        {__import__("program.program").program.Program: mock_prog},
    )

    monkeypatch.setattr(
        "routers.secure.scrape.resolve_torrent_container",
        AsyncMock(return_value=(None, "No files found in torrent", None)),
    )

    test_hash = "3344556677889900112233445566778899001122"
    magnet = f"magnet:?xt=urn:btih:{test_hash}&dn=Rate.Limit.Movie.mkv"

    res = client.post(
        "/api/v1/scrape/start_session",
        params={"item_id": 703, "allow_uncached": True, "magnet": magnet},
        headers=_signed_bff_headers(),
    )
    assert res.status_code == 429
    assert res.headers.get("retry-after") == "60"

    # Verify movie has NO blacklisted streams
    with session_factory() as session:
        item = session.get(Movie, 703)
        assert len(item.blacklisted_streams) == 0


def test_successful_uncached_submission_creates_durable_monitoring(
    scrape_client, monkeypatch
):
    """Successful uncached submission returns correct transfer ID and enqueues outbox polling."""
    client, session_factory = scrape_client

    with session_factory() as session:
        movie = Movie({"title": "Durable Movie", "year": 2026})
        movie.id = 704
        session.add(movie)
        session.commit()

    test_hash = "4455667788990011223344556677889900112233"
    magnet = f"magnet:?xt=urn:btih:{test_hash}&dn=Durable.Movie.mkv"

    mock_info = _make_torrent_info(
        "rd-durable-704", name="Durable.Movie.mkv", status="waiting_files_selection"
    )

    mock_rd = MagicMock()
    mock_rd.key = "realdebrid"
    mock_rd.initialized = True
    mock_rd.api.session.breakers = {}
    mock_rd._fair_usage_remaining.return_value = 0.0
    mock_rd.add_torrent.return_value = "rd-durable-704"
    mock_rd.get_torrent_info.return_value = mock_info

    downloader = FakeDownloaderHolder(
        services_dict={RealDebridDownloader: mock_rd},
        initialized_services=[mock_rd],
    )
    mock_prog = MagicMock()
    mock_prog.services.downloader = downloader
    monkeypatch.setattr(
        "routers.secure.scrape.di",
        {__import__("program.program").program.Program: mock_prog},
    )

    monkeypatch.setattr(
        "routers.secure.scrape.resolve_torrent_container",
        AsyncMock(return_value=(None, "No files found in torrent", None)),
    )

    res = client.post(
        "/api/v1/scrape/start_session",
        params={"item_id": 704, "allow_uncached": True, "magnet": magnet},
        headers=_signed_bff_headers(),
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["torrent_id"] == "rd-durable-704"

    # Verify durable monitoring in database
    with session_factory() as session:
        item = session.get(Movie, 704)
        assert item.active_stream is not None
        assert item.active_stream.id == "rd-durable-704"
        assert item.active_stream.infohash == test_hash

        op = (
            session.query(OperationLedger)
            .filter(OperationLedger.media_item_id == 704)
            .first()
        )
        assert op is not None
        assert op.operation_type == "realdebrid.poll_transfer"
        assert op.status == "pending"
        assert op.payload["torrent_id"] == "rd-durable-704"


def test_cached_manual_session_workflow_preserved(scrape_client, monkeypatch):
    """Cached manual-session workflow behaviour is preserved and bypasses cloud submission."""
    client, session_factory = scrape_client

    with session_factory() as session:
        movie = Movie({"title": "Cached Movie", "year": 2026})
        movie.id = 705
        session.add(movie)
        session.commit()

    test_hash = "5566778899001122334455667788990011223344"
    magnet = f"magnet:?xt=urn:btih:{test_hash}&dn=Cached.Movie.mkv"

    cached_file = DebridFile.create(
        path="/Cached.Movie.mkv",
        filename="Cached.Movie.mkv",
        filesize_bytes=2000000000,
        filetype="movie",
        file_id=1,
    )
    cached_container = TorrentContainer(
        infohash=test_hash,
        files=[cached_file],
        torrent_id="cached-container-id",
        torrent_info=None,
    )

    mock_rd = MagicMock()
    mock_rd.key = "realdebrid"
    mock_rd.initialized = True
    mock_rd.get_torrent_info.return_value = _make_torrent_info(
        "cached-container-id", name="Cached.Movie.mkv"
    )

    downloader = FakeDownloaderHolder(
        services_dict={RealDebridDownloader: mock_rd},
        initialized_services=[mock_rd],
    )
    mock_prog = MagicMock()
    mock_prog.services.downloader = downloader
    monkeypatch.setattr(
        "routers.secure.scrape.di",
        {__import__("program.program").program.Program: mock_prog},
    )

    monkeypatch.setattr(
        "routers.secure.scrape.resolve_torrent_container",
        AsyncMock(return_value=(cached_container, None, mock_rd)),
    )

    res = client.post(
        "/api/v1/scrape/start_session",
        params={"item_id": 705, "allow_uncached": False, "magnet": magnet},
        headers=_signed_bff_headers(),
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["session_id"] is not None
    assert len(data["parsed_files"]) > 0

    # Crucial: NO uncached submission called
    mock_rd.add_torrent.assert_not_called()


def test_failure_after_remote_creation_cleans_up_created_transfer_only(
    scrape_client, monkeypatch
):
    """Failure during database persistence deletes only the newly created remote transfer and returns 500."""
    client, session_factory = scrape_client

    with session_factory() as session:
        movie = Movie({"title": "Rollback Movie", "year": 2026})
        movie.id = 706
        session.add(movie)
        session.commit()

    test_hash = "6677889900112233445566778899001122334455"
    magnet = f"magnet:?xt=urn:btih:{test_hash}&dn=Rollback.Movie.mkv"

    mock_rd = MagicMock()
    mock_rd.key = "realdebrid"
    mock_rd.initialized = True
    mock_rd.api.session.breakers = {}
    mock_rd._fair_usage_remaining.return_value = 0.0
    mock_rd.add_torrent.return_value = "rd-cleanup-706"
    mock_rd.get_torrent_info.return_value = _make_torrent_info("rd-cleanup-706")

    # Simulate database failure during submit_transfer association
    original_get = session_factory().get

    def fail_get(model, ident):
        if ident == 706 and model == Movie:
            raise RuntimeError("Database failure after remote transfer created")
        return original_get(model, ident)

    with patch("sqlalchemy.orm.Session.get", side_effect=fail_get):
        downloader = FakeDownloaderHolder(
            services_dict={RealDebridDownloader: mock_rd},
            initialized_services=[mock_rd],
        )
        mock_prog = MagicMock()
        mock_prog.services.downloader = downloader
        monkeypatch.setattr(
            "routers.secure.scrape.di",
            {__import__("program.program").program.Program: mock_prog},
        )

        monkeypatch.setattr(
            "routers.secure.scrape.resolve_torrent_container",
            AsyncMock(return_value=(None, "No files found in torrent", None)),
        )

        res = client.post(
            "/api/v1/scrape/start_session",
            params={"item_id": 706, "allow_uncached": True, "magnet": magnet},
            headers=_signed_bff_headers(),
        )
        assert res.status_code == 500
        # The created transfer was deleted to avoid leaving orphaned remote transfers
        mock_rd.delete_torrent.assert_called_once_with("rd-cleanup-706")
