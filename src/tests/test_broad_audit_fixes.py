"""Regression tests for broad backend audit & bug-hunt fixes."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import MagicMock

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from program.contracts.operation_ledger import OperationLedger, enqueue_operation
from program.db.base_model import get_base_metadata
from program.media.item import Movie
from program.media.models import ActiveStream
from program.media.state import States
from program.services.downloaders.models import (
    DebridFile,
    TorrentContainer,
    TorrentInfo,
)
from program.services.downloaders.realdebrid_transfers import (
    RealDebridTransfersService,
)
from program.settings import settings_manager
from program.utils.request import SmartSession
from routers.secure.stream import _extract_response_headers


def test_compute_retry_delay_http_date_without_cast_regression():
    session = SmartSession()
    try:
        resp = httpx.Response(
            429,
            headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"},
        )
        delay = session._compute_retry_delay(resp, attempt=1)
        assert delay >= 0.0
    finally:
        session.close()


def test_extract_response_headers_sanitizes_content_disposition():
    resp = httpx.Response(200, headers={"content-type": "video/x-matroska"})
    headers = _extract_response_headers(resp, 'Evil"Name\r\nX-Injected: bad\\path.mkv')
    disp = headers["content-disposition"]
    assert "\r" not in disp
    assert "\n" not in disp
    assert "\\" not in disp
    assert disp == 'inline; filename="EvilNameX-Injected: badpath.mkv"'


def test_realdebrid_transfers_populates_parsed_title_and_year(monkeypatch):
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

    monkeypatch.setattr(
        "program.services.downloaders.realdebrid_transfers.db_session",
        _test_session,
    )
    monkeypatch.setattr(
        settings_manager.settings.downloaders.real_debrid, "enabled", True
    )
    monkeypatch.setattr(
        settings_manager.settings.downloaders.real_debrid, "api_key", "rd_key"
    )

    mock_rd = MagicMock()
    mock_rd.initialized = True
    mock_rd.get_torrents.return_value = [
        {
            "id": "rd-1",
            "filename": "The.Odyssey.2026.2160p.UHD.BluRay.Remux.mkv",
            "hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "bytes": 1000,
            "progress": 50.0,
            "status": "downloading",
            "added": "2026-11-10T12:00:00Z",
        }
    ]
    mock_rd.get_active_count.return_value = {"nb": 1, "limit": 20}
    monkeypatch.setattr(
        "program.services.downloaders.realdebrid_transfers._get_rd_downloader",
        lambda: mock_rd,
    )

    svc = RealDebridTransfersService()
    res = svc.get_transfers(force_refresh=True)
    assert len(res.transfers) == 1
    assert res.transfers[0].parsed_title == "The Odyssey"
    assert res.transfers[0].year == 2026


def test_poll_and_ingest_transitions_state_and_emits_downloader_event(monkeypatch):
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

    monkeypatch.setattr(
        "program.services.downloaders.realdebrid_transfers.db_session",
        _test_session,
    )

    test_hash = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    with TestingSession() as session:
        movie = Movie(
            {
                "title": "State Transition Test",
                "year": 2026,
                "aired_at": datetime(2026, 1, 1, tzinfo=UTC),
            }
        )
        movie.id = 901
        movie.last_state = States.Scraped
        movie.active_stream = ActiveStream(infohash=test_hash, id="rd-ingest-901")
        session.add(movie)
        session.commit()
        enqueue_operation(
            session=session,
            operation_type="realdebrid.poll_transfer",
            media_item_id=901,
            payload={"torrent_id": "rd-ingest-901", "infohash": test_hash},
        )
        session.commit()

    info = TorrentInfo(
        id="rd-ingest-901",
        name="State.Transition.Test.2026.1080p.mkv",
        status="downloaded",
        bytes=2000000000,
        progress=100.0,
        infohash=test_hash,
        files={},
        links=["https://real-debrid.com/d/901"],
    )
    container = TorrentContainer(
        infohash=test_hash,
        files=[
            DebridFile(
                file_id=1,
                filename="State.Transition.Test.2026.1080p.mkv",
                filesize=2000000000,
                download_url="https://real-debrid.com/d/901",
            )
        ],
    )

    mock_rd = MagicMock()
    mock_rd.initialized = True
    mock_rd.get_torrents.return_value = [
        {
            "id": "rd-ingest-901",
            "filename": "State.Transition.Test.2026.1080p.mkv",
            "hash": test_hash,
            "bytes": 2000000000,
            "progress": 100.0,
            "status": "downloaded",
            "added": "2026-11-10T12:00:00Z",
        }
    ]
    mock_rd._process_torrent.return_value = (container, "ok", info)

    mock_downloader = MagicMock()
    mock_downloader.update_item_attributes.return_value = True

    from kink import di

    from program.program import Program

    mock_program = MagicMock()
    mock_program.services.downloader = mock_downloader
    monkeypatch.setitem(di._services, Program, mock_program)

    svc = RealDebridTransfersService()
    res = svc.poll_and_ingest_transfers(rd=mock_rd)
    assert res == {"polled": 1, "ingested": 1}

    with TestingSession() as session:
        refreshed = session.get(Movie, 901)
        assert refreshed is not None
        assert refreshed.last_state == States.Downloaded
        op = (
            session.query(OperationLedger)
            .filter(OperationLedger.media_item_id == 901)
            .first()
        )
        assert op is not None
        assert op.status == "completed"

    mock_program.em.add_event.assert_called_once()
    emitted_event = mock_program.em.add_event.call_args.args[0]
    assert emitted_event.emitted_by == "Downloader"
    assert emitted_event.item_id == 901
