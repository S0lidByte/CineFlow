"""Tests for settings Auto-Tune endpoints."""

import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from kink import di

from program.services.streaming.cache_autotune import autotune_job_manager
from program.settings import settings_manager
from program.settings.models import AppModel
from routers.secure.settings import router as settings_router

app = FastAPI()
app.include_router(settings_router, prefix="/api/v1")

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_job_manager():
    # Cancel any leftover active job
    active = autotune_job_manager.get_active_job()
    if active:
        autotune_job_manager.cancel_job(active.run_id)
    with autotune_job_manager._lock:
        autotune_job_manager._jobs.clear()
    yield


def test_autotune_run_and_status():
    resp = client.post(
        "/api/v1/settings/autotune/run",
        json={"profile": "balanced", "bench_bytes": 1024 * 1024},
    )
    assert resp.status_code == 200
    data = resp.json()
    run_id = data["run_id"]
    assert data["profile"] == "balanced"

    # Status check
    status_resp = client.get(f"/api/v1/settings/autotune/status/{run_id}")
    assert status_resp.status_code == 200
    status_data = status_resp.json()
    assert status_data["run_id"] == run_id

    # Starting another job while running should return 409
    conflict_resp = client.post(
        "/api/v1/settings/autotune/run",
        json={"profile": "conservative", "bench_bytes": 1024 * 1024},
    )
    # Could be 409 if still running, or 200 if completed very fast
    assert conflict_resp.status_code in (200, 409)


def test_autotune_api_serialization_preserves_policy_metadata():
    resp = client.post(
        "/api/v1/settings/autotune/run",
        json={"profile": "balanced", "bench_bytes": 1024 * 1024},
    )
    run_id = resp.json()["run_id"]
    deadline = time.time() + 10.0
    status_data = {}
    while time.time() < deadline:
        status_resp = client.get(f"/api/v1/settings/autotune/status/{run_id}")
        status_data = status_resp.json()
        if status_data["status"] == "completed":
            break
        time.sleep(0.05)
    assert status_data["status"] == "completed"
    rec = status_data["recommendation"]
    policy = rec["policy_metadata"]
    assert policy["policy_id"] == "cineflow.cache-capacity"
    assert policy["policy_version"] == "1.0.0"
    assert isinstance(policy["reason"], str) and len(policy["reason"]) > 0
    assert (
        isinstance(policy["confidence"], (int, float))
        and 0.0 <= policy["confidence"] <= 1.0
    )
    assert isinstance(policy["parameters"], dict)

    # Verify strategy and all material capacity/reserve policy coefficients
    assert policy["parameters"]["profile"] == "balanced"
    policy_params = policy["parameters"]["policy"]
    assert policy_params["tmpfs_available_fraction"] == 0.75
    assert policy_params["memory_headroom_fraction"] == 0.60
    assert policy_params["disk_hot_available_fraction"] == 0.50
    assert policy_params["cgroup_unreadable_fallback_fraction"] == 0.50
    assert policy_params["balanced_hot_fraction"] == 0.20
    assert policy_params["balanced_warm_fraction"] == 0.40
    assert policy_params["warm_minimum_free_mb"] == 1024

    # Verify physical facts and derived provenance boundaries
    assert "physical_facts" in policy["parameters"]
    assert (
        policy["parameters"]["physical_facts"]["filesystem_fact_origin"]
        == "FILESYSTEM_FACT"
    )
    assert policy["parameters"]["physical_facts"]["kernel_fact_origin"] == "KERNEL_FACT"
    assert policy["parameters"]["result_origin"] == "DERIVED"

    # Verify origin and semantics
    assert policy["origin"] == "FALLBACK_POLICY"
    assert policy["semantics"] == "POLICY_TARGET"


def test_autotune_status_not_found():
    resp = client.get("/api/v1/settings/autotune/status/non-existent-id")
    assert resp.status_code == 404


def test_autotune_cancel():
    resp = client.post(
        "/api/v1/settings/autotune/run",
        json={"profile": "aggressive", "bench_bytes": 16 * 1024 * 1024},
    )
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]

    cancel_resp = client.post(f"/api/v1/settings/autotune/cancel/{run_id}")
    assert cancel_resp.status_code == 200

    status_resp = client.get(f"/api/v1/settings/autotune/status/{run_id}")
    assert status_resp.status_code == 200
    assert status_resp.json()["status"] == "cancelled"


def test_autotune_apply_workflow():
    # Start and wait for completion
    resp = client.post(
        "/api/v1/settings/autotune/run",
        json={"profile": "balanced", "bench_bytes": 1024 * 1024},
    )
    run_id = resp.json()["run_id"]

    # Poll status until complete
    timeout = 10.0
    start = time.time()
    while time.time() - start < timeout:
        stat = client.get(f"/api/v1/settings/autotune/status/{run_id}").json()
        if stat["status"] == "completed":
            break
        time.sleep(0.05)

    assert stat["status"] == "completed"
    assert stat["recommendation"] is not None

    # Apply without active handles
    apply_resp = client.post(
        "/api/v1/settings/autotune/apply",
        json={"run_id": run_id, "force": False},
    )
    assert apply_resp.status_code == 200
    assert "applied successfully" in apply_resp.json()["message"]

    assert settings_manager.settings.filesystem.autotune_mode == "balanced"


def test_autotune_apply_blocked_by_open_vfs_handles():
    resp = client.post(
        "/api/v1/settings/autotune/run",
        json={"profile": "conservative", "bench_bytes": 1024 * 1024},
    )
    run_id = resp.json()["run_id"]

    # Wait for completion
    start = time.time()
    while time.time() - start < 10.0:
        stat = client.get(f"/api/v1/settings/autotune/status/{run_id}").json()
        if stat["status"] == "completed":
            break
        time.sleep(0.05)

    from program.program import Program

    mock_vfs = MagicMock()
    mock_vfs.has_open_media_handles.return_value = True

    mock_services = MagicMock()
    mock_services.filesystem.riven_vfs = mock_vfs

    mock_program = MagicMock()
    mock_program.services = mock_services

    orig_program = di._services.get(Program)
    di[Program] = mock_program
    try:
        # Without force, should be 409 Conflict
        blocked_resp = client.post(
            "/api/v1/settings/autotune/apply",
            json={"run_id": run_id, "force": False},
        )
        assert blocked_resp.status_code == 409
        assert "Active VFS media handles detected" in blocked_resp.json()["detail"]

        # With force=True, should succeed
        forced_resp = client.post(
            "/api/v1/settings/autotune/apply",
            json={"run_id": run_id, "force": True},
        )
        assert forced_resp.status_code == 200
        assert "applied successfully" in forced_resp.json()["message"]
    finally:
        if orig_program is not None:
            di[Program] = orig_program
        else:
            di._services.pop(Program, None)
