"""Unit tests for /api/v1/items cursor-based and offset pagination."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from program.db.base_model import get_base_metadata
from program.media.item import Movie
from program.media.state import States
from routers import app_router
from routers.secure.items import _decode_cursor, _encode_cursor, _get_items_count_cache

TEST_BFF_API_KEY = "test-bff-service-key"
TEST_ACTOR_SECRET = "test-actor-context-secret"


def _signed_bff_headers(roles: str = "library:read") -> dict[str, str]:
    actor_id = "test-user"
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
def items_client(monkeypatch):
    """Set up TestClient with isolated in-memory SQLite database populated with media items."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    get_base_metadata().create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    from contextlib import contextmanager

    @contextmanager
    def _test_session():
        session = TestingSession()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr("routers.secure.items.db_session", _test_session)
    monkeypatch.setenv("BFF_API_KEY", TEST_BFF_API_KEY)
    monkeypatch.setenv("ACTOR_CONTEXT_SECRET", TEST_ACTOR_SECRET)

    # Populate 15 sample movie items with staggered dates and titles
    base_time = datetime(2026, 3, 1, 12, 0, 0)
    with TestingSession() as session:
        for i in range(1, 16):
            movie = Movie(
                {
                    "title": f"Movie {i:02d}",
                    "year": 2026,
                    "requested_at": base_time + timedelta(hours=i),
                    "is_anime": False,
                    "tmdb_id": f"tmdb_{i}",
                }
            )
            movie.last_state = States.Completed
            session.add(movie)
        session.commit()

    app = FastAPI()
    app.include_router(app_router)
    return TestClient(app)


def test_cursor_encoding_and_decoding():
    """Verify cursor encoding and decoding functions preserve keys and values."""
    dt = datetime(2026, 3, 1, 12, 30, 45)
    token = _encode_cursor("date", dt, 42)
    assert isinstance(token, str)

    decoded = _decode_cursor(token)
    assert decoded["k"] == "date"
    assert decoded["id"] == 42
    assert decoded["v"] == dt.isoformat()

    title_token = _encode_cursor("title", "Interstellar", 100)
    title_decoded = _decode_cursor(title_token)
    assert title_decoded["k"] == "title"
    assert title_decoded["v"] == "Interstellar"
    assert title_decoded["id"] == 100


def test_legacy_offset_pagination_backward_compatibility(items_client):
    """Ensure standard offset pagination works seamlessly without cursors."""
    headers = _signed_bff_headers()
    response = items_client.get("/api/v1/items?limit=5&page=1", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["total_items"] == 15
    assert data["total_pages"] == 3
    assert data["page"] == 1
    assert data["limit"] == 5
    assert len(data["items"]) == 5
    assert data["has_more"] is True
    assert data["next_cursor"] is not None

    # Verify second page
    response_p2 = items_client.get("/api/v1/items?limit=5&page=2", headers=headers)
    assert response_p2.status_code == 200
    data_p2 = response_p2.json()
    assert data_p2["page"] == 2
    assert len(data_p2["items"]) == 5
    assert data_p2["items"][0]["title"] != data["items"][0]["title"]


def test_keyset_cursor_forward_pagination_date_desc(items_client):
    """Verify forward cursor pagination with default date_desc ordering."""
    headers = _signed_bff_headers()
    # Initial page with limit 5
    r1 = items_client.get("/api/v1/items?limit=5", headers=headers)
    assert r1.status_code == 200
    d1 = r1.json()
    assert len(d1["items"]) == 5
    assert d1["has_more"] is True
    cursor_1 = d1["next_cursor"]
    assert cursor_1 is not None

    # First item should be the newest (Movie 15)
    assert d1["items"][0]["title"] == "Movie 15"
    assert d1["items"][4]["title"] == "Movie 11"

    # Page 2 using cursor
    r2 = items_client.get(f"/api/v1/items?limit=5&cursor={cursor_1}", headers=headers)
    assert r2.status_code == 200
    d2 = r2.json()
    assert len(d2["items"]) == 5
    assert d2["items"][0]["title"] == "Movie 10"
    assert d2["items"][4]["title"] == "Movie 06"
    assert d2["has_more"] is True
    cursor_2 = d2["next_cursor"]

    # Page 3 using second cursor
    r3 = items_client.get(f"/api/v1/items?limit=5&cursor={cursor_2}", headers=headers)
    assert r3.status_code == 200
    d3 = r3.json()
    assert len(d3["items"]) == 5
    assert d3["items"][0]["title"] == "Movie 05"
    assert d3["items"][4]["title"] == "Movie 01"
    assert d3["has_more"] is False
    assert d3["next_cursor"] is None


def test_keyset_cursor_title_sorting_and_bidirectional(items_client):
    """Verify keyset pagination works with title sorting and prev/next direction."""
    headers = _signed_bff_headers()
    r1 = items_client.get("/api/v1/items?limit=5&sort=title_asc", headers=headers)
    assert r1.status_code == 200
    d1 = r1.json()
    assert len(d1["items"]) == 5
    assert d1["items"][0]["title"] == "Movie 01"
    assert d1["items"][4]["title"] == "Movie 05"
    c1 = d1["next_cursor"]

    # Fetch next page
    r2 = items_client.get(
        f"/api/v1/items?limit=5&sort=title_asc&cursor={c1}", headers=headers
    )
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["items"][0]["title"] == "Movie 06"
    assert d2["items"][4]["title"] == "Movie 10"

    # Fetch backward (prev) using cursor of page 2
    prev_c = d2["prev_cursor"]
    assert prev_c is not None
    r_prev = items_client.get(
        f"/api/v1/items?limit=5&sort=title_asc&cursor={prev_c}&direction=prev",
        headers=headers,
    )
    assert r_prev.status_code == 200
    d_prev = r_prev.json()
    assert len(d_prev["items"]) == 5
    assert d_prev["items"][0]["title"] == "Movie 01"
    assert d_prev["items"][4]["title"] == "Movie 05"


def test_keyset_cursor_title_desc_sorting(items_client):
    """Verify keyset pagination works with title_desc ordering."""
    headers = _signed_bff_headers()
    r1 = items_client.get("/api/v1/items?limit=5&sort=title_desc", headers=headers)
    assert r1.status_code == 200
    d1 = r1.json()
    assert len(d1["items"]) == 5
    assert d1["items"][0]["title"] == "Movie 15"
    assert d1["items"][4]["title"] == "Movie 11"
    c1 = d1["next_cursor"]
    assert c1 is not None

    r2 = items_client.get(
        f"/api/v1/items?limit=5&sort=title_desc&cursor={c1}", headers=headers
    )
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["items"][0]["title"] == "Movie 10"
    assert d2["items"][4]["title"] == "Movie 06"


def test_keyset_cursor_date_asc_sorting(items_client):
    """Verify keyset pagination works with date_asc ordering."""
    headers = _signed_bff_headers()
    r1 = items_client.get("/api/v1/items?limit=5&sort=date_asc", headers=headers)
    assert r1.status_code == 200
    d1 = r1.json()
    assert len(d1["items"]) == 5
    assert d1["items"][0]["title"] == "Movie 01"
    assert d1["items"][4]["title"] == "Movie 05"
    c1 = d1["next_cursor"]
    assert c1 is not None

    r2 = items_client.get(
        f"/api/v1/items?limit=5&sort=date_asc&cursor={c1}", headers=headers
    )
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["items"][0]["title"] == "Movie 06"
    assert d2["items"][4]["title"] == "Movie 10"


def test_empty_library_cursor_pagination(monkeypatch):
    """Verify pagination behavior on empty database."""
    _get_items_count_cache.clear()
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    get_base_metadata().create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    from contextlib import contextmanager

    @contextmanager
    def _empty_session():
        session = TestingSession()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr("routers.secure.items.db_session", _empty_session)
    monkeypatch.setenv("BFF_API_KEY", TEST_BFF_API_KEY)
    monkeypatch.setenv("ACTOR_CONTEXT_SECRET", TEST_ACTOR_SECRET)

    app = FastAPI()
    app.include_router(app_router)
    client = TestClient(app)

    headers = _signed_bff_headers()
    res = client.get("/api/v1/items?limit=10", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["items"] == []
    assert data["total_items"] == 0
    assert data["has_more"] is False
    assert data["next_cursor"] is None
    assert data["prev_cursor"] is None


def test_invalid_cursor_handling(items_client):
    """Verify malformed or mismatched cursor returns HTTP 400."""
    headers = _signed_bff_headers()
    res = items_client.get(
        "/api/v1/items?cursor=invalid_base64_string", headers=headers
    )
    assert res.status_code == 400
    assert "Invalid pagination cursor" in res.json()["detail"]

    # Cursor with mismatched sort key
    title_cursor = _encode_cursor("title", "Movie 05", 5)
    res_mismatch = items_client.get(
        f"/api/v1/items?sort=date_desc&cursor={title_cursor}", headers=headers
    )
    assert res_mismatch.status_code == 400
    assert (
        "Cursor sort key 'title' does not match requested sort 'date'"
        in res_mismatch.json()["detail"]
    )
