from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from kink import di

from auth import resolve_api_key
from program.program import Program
from routers.secure import default as default_router_mod
from routers.secure.default import get_vfs_stats


@pytest.mark.asyncio
async def test_get_vfs_stats_returns_representative_runtime_statistics():
    stats = {
        "movie-123": {
            "opens": 4,
            "bytes_read": 8192,
            "cache_hits": 3,
            "cache_misses": 1,
        },
        "show-456": {
            "opens": 2,
            "bytes_read": 4096,
            "cache_hits": 1,
            "cache_misses": 1,
        },
    }
    original = di[Program] if Program in di else None
    di[Program] = SimpleNamespace(
        services=SimpleNamespace(
            filesystem=SimpleNamespace(
                riven_vfs=SimpleNamespace(opener_stats=stats)
            )
        )
    )

    try:
        response = await get_vfs_stats()
    finally:
        if original is not None:
            di[Program] = original
        elif Program in di:
            del di[Program]

    assert response.stats == stats
    assert response.stats["movie-123"]["cache_hits"] == 3


@pytest.mark.asyncio
async def test_get_vfs_stats_returns_empty_statistics_when_no_files_opened():
    original = di[Program] if Program in di else None
    di[Program] = SimpleNamespace(
        services=SimpleNamespace(
            filesystem=SimpleNamespace(
                riven_vfs=SimpleNamespace(opener_stats={})
            )
        )
    )

    try:
        response = await get_vfs_stats()
    finally:
        if original is not None:
            di[Program] = original
        elif Program in di:
            del di[Program]

    assert response.stats == {}


def test_vfs_stats_endpoint_requires_auth_and_returns_representative_stats(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        default_router_mod.settings_manager,
        "settings",
        MagicMock(api_key="k" * 32),
    )
    stats = {"movie-123": {"opens": 1, "bytes_read": 1024}}
    original = di[Program] if Program in di else None
    di[Program] = SimpleNamespace(
        services=SimpleNamespace(
            filesystem=SimpleNamespace(
                riven_vfs=SimpleNamespace(opener_stats=stats)
            )
        )
    )
    try:
        app = FastAPI()
        app.include_router(
            default_router_mod.router,
            dependencies=[Depends(resolve_api_key)],
        )
        client = TestClient(app)
        assert client.get("/vfs_stats").status_code == 401
        response = client.get("/vfs_stats", headers={"x-api-key": "k" * 32})
        assert response.status_code == 200
        assert response.json() == {"stats": stats}
    finally:
        if original is not None:
            di[Program] = original
        elif Program in di:
            del di[Program]
