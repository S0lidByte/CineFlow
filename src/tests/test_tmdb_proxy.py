import importlib.util
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

TMDB_ROUTER_PATH = (
    Path(__file__).resolve().parents[1] / "routers" / "secure" / "tmdb.py"
)
TMDB_ROUTER_SPEC = importlib.util.spec_from_file_location(
    "tmdb_router_under_test", TMDB_ROUTER_PATH
)
assert TMDB_ROUTER_SPEC and TMDB_ROUTER_SPEC.loader
tmdb_router = importlib.util.module_from_spec(TMDB_ROUTER_SPEC)
sys.modules[TMDB_ROUTER_SPEC.name] = tmdb_router
TMDB_ROUTER_SPEC.loader.exec_module(tmdb_router)


def test_validate_tmdb_path_rejects_non_v3_paths():
    with pytest.raises(HTTPException) as excinfo:
        tmdb_router._validate_tmdb_path("4/account")

    assert excinfo.value.status_code == 400


def test_proxy_tmdb_get_forwards_path_query_and_auth(monkeypatch):
    captured: dict[str, object] = {}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, path, *, params, headers):
            captured["path"] = path
            captured["params"] = params
            captured["headers"] = headers
            return httpx.Response(
                200,
                json={"results": [{"id": 1}]},
                headers={
                    "cache-control": "public, max-age=60",
                    "connection": "keep-alive",
                    "content-type": "application/json",
                },
                request=httpx.Request("GET", f"https://api.themoviedb.org{path}"),
            )

    monkeypatch.setattr(tmdb_router.httpx, "AsyncClient", FakeAsyncClient)

    app = FastAPI()
    app.include_router(tmdb_router.router)

    response = TestClient(app).get(
        "/tmdb/3/trending/movie/day?page=2&language=en-US"
    )

    assert response.status_code == 200
    assert response.json() == {"results": [{"id": 1}]}
    assert captured["path"] == "/3/trending/movie/day"
    assert captured["params"] == [("page", "2"), ("language", "en-US")]
    assert captured["headers"]["authorization"].startswith("Bearer ")
    assert response.headers["cache-control"] == "public, max-age=60"
    assert "connection" not in response.headers
