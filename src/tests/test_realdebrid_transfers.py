"""Unit tests for Real-Debrid transfer monitoring, correlation, and safe management."""

from __future__ import annotations

import hashlib
import hmac
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from program.contracts.operation_ledger import OperationLedger, enqueue_operation
from program.db.base_model import get_base_metadata
from program.media.item import Movie
from program.media.models import ActiveStream
from program.media.state import States
from program.media.stream import Stream
from program.services.downloaders.models import TorrentFile, TorrentInfo
from program.services.downloaders.realdebrid import (
    RealDebridError,
    RealDebridTorrentInfo,
)
from program.services.downloaders.realdebrid_transfers import (
    rd_transfers_service,
)
from program.settings import settings_manager
from program.utils.request import CircuitBreakerOpen
from routers import app_router

TEST_BFF_API_KEY = "test-bff-service-key"
TEST_ACTOR_SECRET = "test-actor-context-secret"


def _signed_bff_headers(roles: str) -> dict[str, str]:
    actor_id = "transfers-tester"
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


@pytest.fixture
def api_client(monkeypatch):
    """Set up TestClient with an isolated in-memory SQLite database and test API key."""
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

    monkeypatch.setattr("routers.secure.operations.db_session", _test_session)
    monkeypatch.setattr(
        "program.services.downloaders.realdebrid_transfers.db_session", _test_session
    )
    monkeypatch.setattr("program.contracts.dispatcher.db_session", _test_session)
    monkeypatch.setenv("BFF_API_KEY", TEST_BFF_API_KEY)
    monkeypatch.setenv("ACTOR_CONTEXT_SECRET", TEST_ACTOR_SECRET)

    # Invalidate transfer service cache before each test
    rd_transfers_service.invalidate_cache()

    app = FastAPI()
    app.include_router(app_router)
    client = TestClient(app)
    return client, TestingSession


def _mock_mock_rd_torrents():
    return [
        {
            "id": "rd-active-1",
            "filename": "Oppenheimer.2023.2160p.mkv",
            "hash": "0123456789abcdef0123456789abcdef01234567",
            "bytes": 15000000000,
            "progress": 45.5,
            "status": "downloading",
            "added": "2026-11-07T12:00:00.000Z",
            "ended": None,
            "speed": 12500000,
            "seeders": 42,
            "links": [],
        },
        {
            "id": "rd-queued-2",
            "filename": "Inception.2010.1080p.mkv",
            "hash": "abcdefabcdefabcdefabcdefabcdefabcdefabcd",
            "bytes": 8500000000,
            "progress": 0.0,
            "status": "queued",
            "added": "2026-11-07T12:05:00.000Z",
            "ended": None,
            "speed": None,
            "seeders": None,
            "links": [],
        },
        {
            "id": "rd-downloaded-3",
            "filename": "Interstellar.2014.1080p.mkv",
            "hash": "1111222233334444555566667777888899990000",
            "bytes": 12000000000,
            "progress": 100.0,
            "status": "downloaded",
            "added": "2026-11-07T11:00:00.000Z",
            "ended": "2026-11-07T11:20:00.000Z",
            "speed": 0,
            "seeders": 10,
            "links": ["https://real-debrid.com/d/abc"],
        },
        {
            "id": "rd-external-4",
            "filename": "External.Linux.Distro.iso",
            "hash": "9999888877776666555544443333222211110000",
            "bytes": 2000000000,
            "progress": 100.0,
            "status": "downloaded",
            "added": "2026-11-07T10:00:00.000Z",
            "ended": "2026-11-07T10:05:00.000Z",
            "speed": None,
            "seeders": None,
            "links": ["https://real-debrid.com/d/xyz"],
        },
        {
            "id": "rd-failed-5",
            "filename": "Corrupt.Torrent.2020.mkv",
            "hash": "deaddeaddeaddeaddeaddeaddeaddeaddeaddead",
            "bytes": 500000000,
            "progress": 0.0,
            "status": "error",
            "added": "2026-11-07T09:00:00.000Z",
            "ended": None,
            "speed": None,
            "seeders": None,
            "links": [],
        },
    ]


def test_list_realdebrid_transfers_with_correlation_and_stats(api_client, monkeypatch):
    client, session_factory = api_client

    # Seed CineFlow database with Movie and Streams
    with session_factory() as session:
        movie1 = Movie({"title": "Oppenheimer"})
        movie1.id = 101
        movie1.last_state = States.Scraped
        movie1.active_stream = ActiveStream(
            infohash="0123456789abcdef0123456789abcdef01234567",
            id="rd-active-1",
        )

        movie2 = Movie({"title": "Interstellar"})
        movie2.id = 102
        movie2.updated = True
        movie2.last_state = States.Completed
        movie2.active_stream = ActiveStream(
            infohash="1111222233334444555566667777888899990000",
            id="rd-downloaded-3",
        )

        session.add_all([movie1, movie2])
        session.commit()

        # Add mock operation for Oppenheimer
        enqueue_operation(
            session=session,
            operation_type="item.download",
            media_item_id=101,
            payload={"torrent_id": "rd-active-1"},
        )
        session.commit()

    mock_rd = MagicMock()
    mock_rd.initialized = True
    mock_rd.get_torrents.return_value = _mock_mock_rd_torrents()
    mock_rd.get_active_count.return_value = {"nb": 1, "limit": 20}

    monkeypatch.setattr(
        "program.services.downloaders.realdebrid_transfers._get_rd_downloader",
        lambda: mock_rd,
    )
    monkeypatch.setattr(
        settings_manager.settings.downloaders.real_debrid, "enabled", True
    )
    monkeypatch.setattr(
        settings_manager.settings.downloaders.real_debrid,
        "api_key",
        "mock_valid_rd_token_32_characters",
    )

    response = client.get(
        "/api/v1/operations/realdebrid/transfers",
        headers=_signed_bff_headers("library:read"),
    )
    assert response.status_code == 200
    data = response.json()

    assert data["rd_enabled"] is True
    assert data["error"] is None
    assert len(data["transfers"]) == 5

    # Verify Summary Statistics
    stats = data["stats"]
    assert stats["total"] == 5
    assert stats["active"] == 1
    assert stats["queued"] == 1
    assert stats["downloaded"] == 2
    assert stats["failed"] == 1
    assert stats["active_slots_used"] == 1
    assert stats["active_slots_limit"] == 20

    # Verify CineFlow correlation on active transfer
    active_item = next(t for t in data["transfers"] if t["id"] == "rd-active-1")
    assert active_item["is_cineflow_managed"] is True
    assert active_item["media_item_id"] == 101
    assert active_item["media_title"] == "Oppenheimer"
    assert active_item["can_cancel"] is True
    assert active_item["ready_in_cineflow"] is False

    # Verify external transfer
    external_item = next(t for t in data["transfers"] if t["id"] == "rd-external-4")
    assert external_item["is_cineflow_managed"] is False
    assert external_item["media_item_id"] is None
    assert external_item["can_cancel"] is False
    assert external_item["ready_in_cineflow"] is False


def test_distinguish_rd_downloaded_from_ready_in_cineflow(api_client, monkeypatch):
    """
    Test distinction between:
    1) RD Downloaded AND Ready in CineFlow (VFS ingested / Completed state)
    2) RD Downloaded but NOT Ready in CineFlow (still in Scraped state, or external)
    """
    client, session_factory = api_client

    with session_factory() as session:
        # Item A: Downloaded on RD and Completed in CineFlow -> ready_in_cineflow = True
        item_a = Movie({"title": "Ready Movie"})
        item_a.id = 201
        item_a.updated = True
        item_a.last_state = States.Completed
        item_a.active_stream = ActiveStream(infohash="hash-ready", id="rd-ready")

        # Item B: Downloaded on RD but still Scraped in CineFlow -> ready_in_cineflow = False
        item_b = Movie({"title": "Pending Ingestion Movie"})
        item_b.id = 202
        item_b.last_state = States.Scraped
        item_b.active_stream = ActiveStream(infohash="hash-pending", id="rd-pending")

        session.add_all([item_a, item_b])
        session.commit()

    torrents = [
        {
            "id": "rd-ready",
            "filename": "Ready.Movie.mkv",
            "hash": "hash-ready",
            "bytes": 5000000000,
            "progress": 100.0,
            "status": "downloaded",
            "added": "2026-11-07T10:00:00.000Z",
            "ended": "2026-11-07T10:15:00.000Z",
            "links": ["https://rd/1"],
        },
        {
            "id": "rd-pending",
            "filename": "Pending.Movie.mkv",
            "hash": "hash-pending",
            "bytes": 6000000000,
            "progress": 100.0,
            "status": "downloaded",
            "added": "2026-11-07T10:00:00.000Z",
            "ended": "2026-11-07T10:15:00.000Z",
            "links": ["https://rd/2"],
        },
        {
            "id": "rd-external",
            "filename": "External.Torrent.mkv",
            "hash": "hash-external",
            "bytes": 3000000000,
            "progress": 100.0,
            "status": "downloaded",
            "added": "2026-11-07T10:00:00.000Z",
            "ended": "2026-11-07T10:15:00.000Z",
            "links": ["https://rd/3"],
        },
    ]

    mock_rd = MagicMock()
    mock_rd.initialized = True
    mock_rd.get_torrents.return_value = torrents
    mock_rd.get_active_count.return_value = {"nb": 0, "limit": 20}

    monkeypatch.setattr(
        "program.services.downloaders.realdebrid_transfers._get_rd_downloader",
        lambda: mock_rd,
    )
    monkeypatch.setattr(
        settings_manager.settings.downloaders.real_debrid, "enabled", True
    )
    monkeypatch.setattr(
        settings_manager.settings.downloaders.real_debrid, "api_key", "rd_key"
    )

    response = client.get(
        "/api/v1/operations/realdebrid/transfers",
        headers=_signed_bff_headers("library:read"),
    )
    assert response.status_code == 200
    data = response.json()

    item_ready = next(t for t in data["transfers"] if t["id"] == "rd-ready")
    item_pending = next(t for t in data["transfers"] if t["id"] == "rd-pending")
    item_external = next(t for t in data["transfers"] if t["id"] == "rd-external")

    assert item_ready["status"] == "downloaded"
    assert item_ready["ready_in_cineflow"] is True
    assert item_ready["is_cineflow_managed"] is True

    assert item_pending["status"] == "downloaded"
    assert item_pending["ready_in_cineflow"] is False
    assert item_pending["is_cineflow_managed"] is True

    assert item_external["status"] == "downloaded"
    assert item_external["ready_in_cineflow"] is False
    assert item_external["is_cineflow_managed"] is False


def test_transfers_missing_metrics_do_not_fail(api_client, monkeypatch):
    """Missing speed, seeders, or ended fields must not cause errors."""
    client, _ = api_client

    torrents = [
        {
            "id": "rd-sparse-1",
            "filename": "Sparse.Video.mkv",
            "hash": "sparse123",
            "bytes": 1000,
            "progress": 50.0,
            "status": "downloading",
            "added": "2026-11-07T12:00:00.000Z",
            # speed, seeders, ended omitted completely
        }
    ]

    mock_rd = MagicMock()
    mock_rd.initialized = True
    mock_rd.get_torrents.return_value = torrents
    mock_rd.get_active_count.return_value = {"nb": 1, "limit": 20}

    monkeypatch.setattr(
        "program.services.downloaders.realdebrid_transfers._get_rd_downloader",
        lambda: mock_rd,
    )
    monkeypatch.setattr(
        settings_manager.settings.downloaders.real_debrid, "enabled", True
    )
    monkeypatch.setattr(
        settings_manager.settings.downloaders.real_debrid, "api_key", "rd_key"
    )

    response = client.get(
        "/api/v1/operations/realdebrid/transfers",
        headers=_signed_bff_headers("library:read"),
    )
    assert response.status_code == 200
    data = response.json()
    item = data["transfers"][0]
    assert item["speed"] is None
    assert item["seeders"] is None
    assert item["ended"] is None


def test_transfers_status_and_search_filtering(api_client, monkeypatch):
    """Test in-memory status filtering and search query."""
    client, _ = api_client

    mock_rd = MagicMock()
    mock_rd.initialized = True
    mock_rd.get_torrents.return_value = _mock_mock_rd_torrents()
    mock_rd.get_active_count.return_value = {"nb": 1, "limit": 20}

    monkeypatch.setattr(
        "program.services.downloaders.realdebrid_transfers._get_rd_downloader",
        lambda: mock_rd,
    )
    monkeypatch.setattr(
        settings_manager.settings.downloaders.real_debrid, "enabled", True
    )
    monkeypatch.setattr(
        settings_manager.settings.downloaders.real_debrid, "api_key", "rd_key"
    )

    # Filter: active
    res_active = client.get(
        "/api/v1/operations/realdebrid/transfers?status=active",
        headers=_signed_bff_headers("library:read"),
    )
    assert res_active.status_code == 200
    data_active = res_active.json()
    assert len(data_active["transfers"]) == 1
    assert data_active["transfers"][0]["id"] == "rd-active-1"

    # Filter: failed
    res_failed = client.get(
        "/api/v1/operations/realdebrid/transfers?status=failed",
        headers=_signed_bff_headers("library:read"),
    )
    assert res_failed.status_code == 200
    data_failed = res_failed.json()
    assert len(data_failed["transfers"]) == 1
    assert data_failed["transfers"][0]["id"] == "rd-failed-5"

    # Search: Inception
    res_search = client.get(
        "/api/v1/operations/realdebrid/transfers?search=Inception",
        headers=_signed_bff_headers("library:read"),
    )
    assert res_search.status_code == 200
    data_search = res_search.json()
    assert len(data_search["transfers"]) == 1
    assert data_search["transfers"][0]["filename"] == "Inception.2010.1080p.mkv"


def test_transfers_cache_and_force_refresh(api_client, monkeypatch):
    """Ensure short TTL cache avoids repeated Real-Debrid API calls, and force_refresh bypasses."""
    client, _ = api_client

    mock_rd = MagicMock()
    mock_rd.initialized = True
    mock_rd.get_torrents.return_value = _mock_mock_rd_torrents()
    mock_rd.get_active_count.return_value = {"nb": 1, "limit": 20}

    monkeypatch.setattr(
        "program.services.downloaders.realdebrid_transfers._get_rd_downloader",
        lambda: mock_rd,
    )
    monkeypatch.setattr(
        settings_manager.settings.downloaders.real_debrid, "enabled", True
    )
    monkeypatch.setattr(
        settings_manager.settings.downloaders.real_debrid, "api_key", "rd_key"
    )

    # First call: hits RD
    res1 = client.get(
        "/api/v1/operations/realdebrid/transfers",
        headers=_signed_bff_headers("library:read"),
    )
    assert res1.status_code == 200
    assert mock_rd.get_torrents.call_count == 1

    # Second call within TTL: served from cache without extra RD call
    res2 = client.get(
        "/api/v1/operations/realdebrid/transfers",
        headers=_signed_bff_headers("library:read"),
    )
    assert res2.status_code == 200
    assert mock_rd.get_torrents.call_count == 1

    # Third call with force_refresh=true: bypasses cache
    res3 = client.get(
        "/api/v1/operations/realdebrid/transfers?force_refresh=true",
        headers=_signed_bff_headers("library:read"),
    )
    assert res3.status_code == 200
    assert mock_rd.get_torrents.call_count == 2


def test_transfers_circuit_breaker_resilience(api_client, monkeypatch):
    """When circuit breaker is open, serve cached transfers with warning if available."""
    client, _ = api_client

    mock_rd = MagicMock()
    mock_rd.initialized = True
    mock_rd.get_torrents.return_value = _mock_mock_rd_torrents()
    mock_rd.get_active_count.return_value = {"nb": 1, "limit": 20}

    monkeypatch.setattr(
        "program.services.downloaders.realdebrid_transfers._get_rd_downloader",
        lambda: mock_rd,
    )
    monkeypatch.setattr(
        settings_manager.settings.downloaders.real_debrid, "enabled", True
    )
    monkeypatch.setattr(
        settings_manager.settings.downloaders.real_debrid, "api_key", "rd_key"
    )

    # Populate cache
    res1 = client.get(
        "/api/v1/operations/realdebrid/transfers",
        headers=_signed_bff_headers("library:read"),
    )
    assert res1.status_code == 200

    # Invalidate cache timestamp to force a refresh, but trip the breaker
    rd_transfers_service._cache_timestamp = 0.0
    mock_rd.get_torrents.side_effect = CircuitBreakerOpen(
        "api.real-debrid.com", retry_after_seconds=30.0
    )

    res2 = client.get(
        "/api/v1/operations/realdebrid/transfers",
        headers=_signed_bff_headers("library:read"),
    )
    assert res2.status_code == 200
    data2 = res2.json()
    assert "circuit breaker" in data2["error"].lower()
    assert len(data2["transfers"]) == 5


def test_transfers_disabled_settings(api_client, monkeypatch):
    """When Real-Debrid is disabled in settings, return clean disabled response."""
    client, _ = api_client

    monkeypatch.setattr(
        settings_manager.settings.downloaders.real_debrid, "enabled", False
    )

    response = client.get(
        "/api/v1/operations/realdebrid/transfers",
        headers=_signed_bff_headers("library:read"),
    )
    assert response.status_code == 200
    data = response.json()
    assert data["rd_enabled"] is False
    assert "not enabled" in data["error"].lower()
    assert len(data["transfers"]) == 0


def test_get_realdebrid_transfer_detail(api_client, monkeypatch):
    """Fetch complete detail including file breakdown."""
    client, _ = api_client

    mock_info = TorrentInfo(
        id="rd-active-1",
        name="Oppenheimer.2023.2160p.mkv",
        status="downloading",
        infohash="0123456789abcdef0123456789abcdef01234567",
        bytes=15000000000,
        created_at=datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC),
        alternative_filename="Oppenheimer.orig.mkv",
        progress=45.5,
        files={
            1: TorrentFile(
                id=1,
                path="/Oppenheimer.2023.2160p.mkv",
                bytes=15000000000,
                selected=1,
                download_url="",
            )
        },
        links=[],
    )

    mock_rd = MagicMock()
    mock_rd.initialized = True
    mock_rd.get_torrent_info.return_value = mock_info
    mock_rd.get_torrents.return_value = _mock_mock_rd_torrents()
    mock_rd.get_active_count.return_value = {"nb": 1, "limit": 20}

    monkeypatch.setattr(
        "program.services.downloaders.realdebrid_transfers._get_rd_downloader",
        lambda: mock_rd,
    )
    monkeypatch.setattr(
        settings_manager.settings.downloaders.real_debrid, "enabled", True
    )
    monkeypatch.setattr(
        settings_manager.settings.downloaders.real_debrid, "api_key", "rd_key"
    )

    response = client.get(
        "/api/v1/operations/realdebrid/transfers/rd-active-1",
        headers=_signed_bff_headers("library:read"),
    )
    assert response.status_code == 200
    detail = response.json()
    assert detail["id"] == "rd-active-1"
    assert detail["filename"] == "Oppenheimer.2023.2160p.mkv"
    assert len(detail["files"]) == 1
    assert detail["files"][0]["path"] == "/Oppenheimer.2023.2160p.mkv"


def test_cancel_cineflow_managed_transfer_success(api_client, monkeypatch):
    """Test safe cancellation of active CineFlow-managed transfer."""
    client, session_factory = api_client

    with session_factory() as session:
        movie = Movie(
            {
                "title": "Dune Part Two",
                "year": 2024,
                "aired_at": datetime(2024, 3, 1, tzinfo=UTC),
            }
        )
        movie.id = 301
        movie.last_state = States.Scraped
        movie.active_stream = ActiveStream(
            infohash="dunedunedunedunedunedunedunedunedunedune",
            id="rd-cancel-1",
        )
        session.add(movie)
        session.commit()

        op = enqueue_operation(
            session=session,
            operation_type="item.download",
            media_item_id=301,
            payload={"torrent_id": "rd-cancel-1"},
        )
        op.status = "processing"
        session.commit()

    mock_info = TorrentInfo(
        id="rd-cancel-1",
        name="Dune.Part.Two.2024.mkv",
        status="downloading",
        infohash="dunedunedunedunedunedunedunedunedunedune",
        bytes=10000000000,
        created_at=datetime.now(UTC),
        alternative_filename="Dune.Part.Two.2024.mkv",
        progress=20.0,
        files={},
        links=[],
    )

    mock_rd = MagicMock()
    mock_rd.initialized = True
    mock_rd.get_torrent_info.return_value = mock_info
    mock_rd.get_torrents.return_value = [
        {
            "id": "rd-cancel-1",
            "filename": "Dune.Part.Two.2024.mkv",
            "hash": "dunedunedunedunedunedunedunedunedunedune",
            "bytes": 10000000000,
            "progress": 20.0,
            "status": "downloading",
            "added": "2026-11-07T12:00:00.000Z",
        }
    ]
    mock_rd.get_active_count.return_value = {"nb": 1, "limit": 20}

    monkeypatch.setattr(
        "program.services.downloaders.realdebrid_transfers._get_rd_downloader",
        lambda: mock_rd,
    )
    monkeypatch.setattr(
        settings_manager.settings.downloaders.real_debrid, "enabled", True
    )
    monkeypatch.setattr(
        settings_manager.settings.downloaders.real_debrid, "api_key", "rd_key"
    )

    response = client.post(
        "/api/v1/operations/realdebrid/transfers/rd-cancel-1/cancel",
        headers=_signed_bff_headers("platform:admin"),
    )
    assert response.status_code == 200
    action_data = response.json()
    assert action_data["success"] is True

    # Real-Debrid delete_torrent must have been invoked
    mock_rd.delete_torrent.assert_called_once_with("rd-cancel-1")

    # Verify OperationLedger updated to cancelled
    with session_factory() as session:
        cancelled_op = (
            session.query(OperationLedger)
            .filter(OperationLedger.media_item_id == 301)
            .first()
        )
        assert cancelled_op.status == "failed"
        assert cancelled_op.error_classification == "CANCELLED"
        assert "cancelled by user" in cancelled_op.error_message.lower()

        # Verify MediaItem active_stream cleared and state reverted to Indexed
        updated_movie = session.get(Movie, 301)
        assert updated_movie.active_stream is None
        assert updated_movie.state == States.Indexed


def test_cancel_unmanaged_transfer_forbidden(api_client, monkeypatch):
    """Attempting to cancel an unmanaged external transfer returns HTTP 403."""
    client, _ = api_client

    mock_info = TorrentInfo(
        id="rd-external-4",
        name="External.Torrent.mkv",
        status="downloading",
        infohash="externalhash123",
        bytes=1000,
        created_at=datetime.now(UTC),
        alternative_filename="External.Torrent.mkv",
        progress=10.0,
        files={},
        links=[],
    )

    mock_rd = MagicMock()
    mock_rd.initialized = True
    mock_rd.get_torrent_info.return_value = mock_info
    mock_rd.get_torrents.return_value = [
        {
            "id": "rd-external-4",
            "filename": "External.Torrent.mkv",
            "hash": "externalhash123",
            "bytes": 1000,
            "progress": 10.0,
            "status": "downloading",
            "added": "2026-11-07T12:00:00.000Z",
        }
    ]
    mock_rd.get_active_count.return_value = {"nb": 1, "limit": 20}

    monkeypatch.setattr(
        "program.services.downloaders.realdebrid_transfers._get_rd_downloader",
        lambda: mock_rd,
    )
    monkeypatch.setattr(
        settings_manager.settings.downloaders.real_debrid, "enabled", True
    )
    monkeypatch.setattr(
        settings_manager.settings.downloaders.real_debrid, "api_key", "rd_key"
    )

    response = client.post(
        "/api/v1/operations/realdebrid/transfers/rd-external-4/cancel",
        headers=_signed_bff_headers("platform:admin"),
    )
    assert response.status_code == 403
    assert "cannot cancel unmanaged" in response.json()["detail"].lower()
    mock_rd.delete_torrent.assert_not_called()


def test_cancel_completed_transfer_bad_request(api_client, monkeypatch):
    """Attempting to cancel a completed/downloaded transfer returns HTTP 400 (no auto-deletion)."""
    client, session_factory = api_client

    with session_factory() as session:
        movie = Movie({"title": "Completed Movie"})
        movie.id = 401
        movie.updated = True
        movie.last_state = States.Completed
        movie.active_stream = ActiveStream(infohash="completedhash", id="rd-comp-1")
        session.add(movie)
        session.commit()

    mock_info = TorrentInfo(
        id="rd-comp-1",
        name="Completed.Movie.mkv",
        status="downloaded",
        infohash="completedhash",
        bytes=1000,
        created_at=datetime.now(UTC),
        alternative_filename="Completed.Movie.mkv",
        progress=100.0,
        files={},
        links=[],
    )

    mock_rd = MagicMock()
    mock_rd.initialized = True
    mock_rd.get_torrent_info.return_value = mock_info
    mock_rd.get_torrents.return_value = [
        {
            "id": "rd-comp-1",
            "filename": "Completed.Movie.mkv",
            "hash": "completedhash",
            "bytes": 1000,
            "progress": 100.0,
            "status": "downloaded",
            "added": "2026-11-07T12:00:00.000Z",
        }
    ]
    mock_rd.get_active_count.return_value = {"nb": 0, "limit": 20}

    monkeypatch.setattr(
        "program.services.downloaders.realdebrid_transfers._get_rd_downloader",
        lambda: mock_rd,
    )
    monkeypatch.setattr(
        settings_manager.settings.downloaders.real_debrid, "enabled", True
    )
    monkeypatch.setattr(
        settings_manager.settings.downloaders.real_debrid, "api_key", "rd_key"
    )

    response = client.post(
        "/api/v1/operations/realdebrid/transfers/rd-comp-1/cancel",
        headers=_signed_bff_headers("platform:admin"),
    )
    assert response.status_code == 400
    assert "cannot cancel a completed" in response.json()["detail"].lower()
    mock_rd.delete_torrent.assert_not_called()


def test_cancel_requires_admin_role(api_client, monkeypatch):
    """Users without platform:admin cannot cancel transfers."""
    client, _ = api_client

    response = client.post(
        "/api/v1/operations/realdebrid/transfers/rd-any/cancel",
        headers=_signed_bff_headers("library:read"),
    )
    assert response.status_code == 403


def test_retry_cineflow_managed_transfer_success(api_client, monkeypatch):
    """Test retrying a failed CineFlow-managed transfer re-queues outbox operation."""
    client, session_factory = api_client

    with session_factory() as session:
        movie = Movie({"title": "Failed Movie"})
        movie.id = 501
        movie.last_state = States.Failed
        movie.active_stream = ActiveStream(infohash="failedhash", id="rd-fail-1")
        session.add(movie)
        session.commit()

        op = enqueue_operation(
            session=session,
            operation_type="item.download",
            media_item_id=501,
            payload={"torrent_id": "rd-fail-1"},
        )
        op.status = "failed"
        op.error_classification = "NetworkError"
        session.commit()

    mock_info = TorrentInfo(
        id="rd-fail-1",
        name="Failed.Movie.mkv",
        status="error",
        infohash="failedhash",
        bytes=1000,
        created_at=datetime.now(UTC),
        alternative_filename="Failed.Movie.mkv",
        progress=0.0,
        files={},
        links=[],
    )

    mock_rd = MagicMock()
    mock_rd.initialized = True
    mock_rd.get_torrent_info.return_value = mock_info
    mock_rd.get_torrents.return_value = [
        {
            "id": "rd-fail-1",
            "filename": "Failed.Movie.mkv",
            "hash": "failedhash",
            "bytes": 1000,
            "progress": 0.0,
            "status": "error",
            "added": "2026-11-07T12:00:00.000Z",
        }
    ]
    mock_rd.get_active_count.return_value = {"nb": 0, "limit": 20}

    monkeypatch.setattr(
        "program.services.downloaders.realdebrid_transfers._get_rd_downloader",
        lambda: mock_rd,
    )
    monkeypatch.setattr(
        settings_manager.settings.downloaders.real_debrid, "enabled", True
    )
    monkeypatch.setattr(
        settings_manager.settings.downloaders.real_debrid, "api_key", "rd_key"
    )

    response = client.post(
        "/api/v1/operations/realdebrid/transfers/rd-fail-1/retry",
        headers=_signed_bff_headers("platform:admin"),
    )
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True

    # Check operation is now pending
    with session_factory() as session:
        retried_op = (
            session.query(OperationLedger)
            .filter(OperationLedger.media_item_id == 501)
            .first()
        )
        assert retried_op.status == "pending"


def test_submit_uncached_transfer_and_outbox_enqueue(api_client, monkeypatch):
    """Test submitting an uncached magnet adds to RD, selects files, and enqueues outbox operation."""
    client, session_factory = api_client

    with session_factory() as session:
        movie = Movie({"title": "Test Uncached Movie", "year": 2026})
        movie.id = 601
        session.add(movie)
        session.commit()

    test_hash = "abcde12345abcde12345abcde12345abcde12345"
    magnet = f"magnet:?xt=urn:btih:{test_hash}&dn=Test.Movie.mkv"

    mock_info = TorrentInfo(
        id="rd-uncached-new",
        name="Test.Movie.mkv",
        status="waiting_files_selection",
        infohash=test_hash,
        bytes=500000000,
        created_at=datetime.now(UTC),
        alternative_filename="Test.Movie.mkv",
        progress=0.0,
        files={
            1: TorrentFile(
                id=1,
                path="/Test.Movie.mkv",
                bytes=500000000,
                selected=1,
                download_url="",
            )
        },
        links=[],
    )

    mock_rd = MagicMock()
    mock_rd.initialized = True
    mock_rd.add_torrent.return_value = "rd-uncached-new"
    mock_rd.get_torrent_info.return_value = mock_info
    mock_rd.get_torrents.return_value = []
    mock_rd.get_active_count.return_value = {"nb": 1, "limit": 20}

    monkeypatch.setattr(
        "program.services.downloaders.realdebrid_transfers._get_rd_downloader",
        lambda: mock_rd,
    )
    monkeypatch.setattr(
        settings_manager.settings.downloaders.real_debrid, "enabled", True
    )
    monkeypatch.setattr(
        settings_manager.settings.downloaders.real_debrid, "api_key", "rd_key"
    )

    response = client.post(
        "/api/v1/operations/realdebrid/transfers/submit",
        json={"magnet": magnet, "media_item_id": 601},
        headers=_signed_bff_headers("platform:admin"),
    )
    assert response.status_code == 200
    res_data = response.json()
    assert res_data["success"] is True
    assert res_data["transfer_id"] == "rd-uncached-new"

    # Verifications on mock RD
    mock_rd.add_torrent.assert_called_once_with(magnet)
    mock_rd.select_files.assert_called_once_with("rd-uncached-new", [1])

    # Verifications on DB
    with session_factory() as session:
        updated_movie = session.get(Movie, 601)
        assert updated_movie.active_stream is not None
        assert updated_movie.active_stream.id == "rd-uncached-new"
        assert updated_movie.active_stream.infohash == test_hash

        op = (
            session.query(OperationLedger)
            .filter(OperationLedger.media_item_id == 601)
            .first()
        )
        assert op is not None
        assert op.operation_type == "realdebrid.poll_transfer"
        assert op.status == "pending"


def test_poll_and_ingest_completed_transfers(api_client, monkeypatch):
    """Test polling completed Real-Debrid transfer automatically ingests into CineFlow."""
    client, session_factory = api_client

    test_hash = "7777777777777777777777777777777777777777"
    with session_factory() as session:
        movie = Movie(
            {
                "title": "Completed Ingestion Target",
                "year": 2026,
                "aired_at": datetime(2026, 1, 1, tzinfo=UTC),
            }
        )
        movie.id = 701
        movie.last_state = States.Scraped
        movie.active_stream = ActiveStream(infohash=test_hash, id="rd-poll-completed")
        session.add(movie)
        session.commit()

        enqueue_operation(
            session=session,
            operation_type="realdebrid.poll_transfer",
            media_item_id=701,
            payload={"torrent_id": "rd-poll-completed", "infohash": test_hash},
        )
        session.commit()

    mock_rd = MagicMock()
    mock_rd.initialized = True
    mock_rd.get_torrents.return_value = [
        {
            "id": "rd-poll-completed",
            "filename": "Completed.Ingestion.Target.2026.mkv",
            "hash": test_hash,
            "bytes": 2000000000,
            "progress": 100.0,
            "status": "downloaded",
            "added": "2026-11-07T12:00:00.000Z",
        }
    ]

    mock_downloader = MagicMock()
    mock_downloader.start_manual_download.return_value = True

    monkeypatch.setattr(
        "program.services.downloaders.realdebrid_transfers._get_rd_downloader",
        lambda: mock_rd,
    )
    from kink import di

    from program.program import Program

    mock_program = MagicMock()
    mock_program.services.downloader = mock_downloader
    monkeypatch.setitem(di._services, Program, mock_program)

    monkeypatch.setattr(
        settings_manager.settings.downloaders.real_debrid, "enabled", True
    )
    monkeypatch.setattr(
        settings_manager.settings.downloaders.real_debrid, "api_key", "rd_key"
    )

    response = client.post(
        "/api/v1/operations/realdebrid/transfers/poll",
        headers=_signed_bff_headers("platform:admin"),
    )
    assert response.status_code == 200
    res_json = response.json()
    assert res_json["polled"] == 1
    assert res_json["ingested"] == 1

    # Verify start_manual_download was invoked
    assert mock_downloader.start_manual_download.called

    # Verify OperationLedger updated to completed
    with session_factory() as session:
        completed_op = (
            session.query(OperationLedger)
            .filter(OperationLedger.media_item_id == 701)
            .first()
        )
        assert completed_op.status == "completed"
