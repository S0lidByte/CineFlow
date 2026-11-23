from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from program.program import Program
from program.settings import settings_manager
from program.settings.models import AppModel
from routers.secure.settings import router as settings_router


@pytest.fixture(autouse=True)
def _restore_changed_top_keys():
    previous = settings_manager.last_changed_top_keys
    try:
        yield
    finally:
        settings_manager.last_changed_top_keys = previous


def _mock_service(*, enabled: bool = True, initialized: bool = True) -> MagicMock:
    service = MagicMock()
    service.enabled = enabled
    service.initialized = initialized
    service.is_content_service = False
    return service


def _run_initialize(*, program: Program, downloader, filesystem) -> None:
    with ExitStack() as stack:
        for target, value in (
            ("program.program.Overseerr", _mock_service()),
            ("program.program.PlexWatchlist", _mock_service()),
            ("program.program.Listrr", _mock_service()),
            ("program.program.Mdblist", _mock_service()),
            ("program.program.TraktContent", _mock_service()),
            ("program.program.IndexerService", _mock_service()),
            ("program.program.Scraping", _mock_service()),
            ("program.program.Updater", _mock_service()),
            ("program.program.Downloader", downloader),
            ("program.program.FilesystemService", filesystem),
            ("program.program.PostProcessing", _mock_service()),
            ("program.program.NotificationService", _mock_service()),
        ):
            stack.enter_context(patch(target, return_value=value))
        program.initialize_services()


def test_initialize_services_closes_previous_filesystem_service():
    program = Program()
    previous_filesystem = MagicMock()
    program.services = MagicMock(filesystem=previous_filesystem)

    downloader = _mock_service()
    filesystem = _mock_service()

    # Unknown change set → remount (conservative).
    settings_manager.last_changed_top_keys = None

    _run_initialize(program=program, downloader=downloader, filesystem=filesystem)

    previous_filesystem.close.assert_called_once_with()
    assert program.services is not None
    assert program.services.filesystem is filesystem


def test_initialize_services_skips_vfs_close_for_content_only_changes():
    program = Program()
    previous_filesystem = MagicMock()
    previous_downloader = MagicMock()
    program.services = MagicMock(
        filesystem=previous_filesystem,
        downloader=previous_downloader,
    )

    downloader = _mock_service()
    filesystem = _mock_service()

    settings_manager.last_changed_top_keys = frozenset({"content"})

    _run_initialize(program=program, downloader=downloader, filesystem=filesystem)

    previous_filesystem.close.assert_not_called()
    assert program.services.filesystem is previous_filesystem
    assert program.services.downloader is previous_downloader


def test_initialize_services_preserves_graph_for_stream_only_changes():
    program = Program()
    previous_services = MagicMock()
    program.services = previous_services
    settings_manager.last_changed_top_keys = frozenset({"stream"})

    program.initialize_services()

    assert program.services is previous_services
    previous_services.filesystem.close.assert_not_called()


def test_initialize_services_preserves_graph_for_noop_update():
    program = Program()
    previous_services = MagicMock()
    program.services = previous_services
    settings_manager.last_changed_top_keys = frozenset()

    program.initialize_services()

    assert program.services is previous_services
    previous_services.filesystem.close.assert_not_called()


def test_initialize_services_reconfigures_logging_without_rebuilding_services():
    program = Program()
    previous_services = MagicMock()
    program.services = previous_services
    settings_manager.last_changed_top_keys = frozenset({"logging", "log_level"})

    with patch("program.program.setup_logger") as setup_logger:
        program.initialize_services()

    setup_logger.assert_called_once_with(settings_manager.settings.log_level)
    assert program.services is previous_services
    previous_services.filesystem.close.assert_not_called()


def test_initialize_services_reconfigures_logging_during_mixed_update():
    program = Program()
    previous_filesystem = MagicMock()
    previous_downloader = MagicMock()
    program.services = MagicMock(
        filesystem=previous_filesystem,
        downloader=previous_downloader,
    )
    settings_manager.last_changed_top_keys = frozenset({"content", "log_level"})

    with patch("program.program.setup_logger") as setup_logger:
        _run_initialize(
            program=program,
            downloader=_mock_service(),
            filesystem=_mock_service(),
        )

    setup_logger.assert_called_once_with(settings_manager.settings.log_level)
    previous_filesystem.close.assert_not_called()
    assert program.services.filesystem is previous_filesystem
    assert program.services.downloader is previous_downloader


def test_initialize_apis_skips_rebuild_for_stream_only_changes():
    program = Program()
    program.initialized = True
    settings_manager.last_changed_top_keys = frozenset({"stream"})

    with patch("program.program.bootstrap_apis") as bootstrap:
        program.initialize_apis()

    bootstrap.assert_not_called()


def test_initialize_apis_rebuilds_for_content_changes():
    program = Program()
    program.initialized = True
    settings_manager.last_changed_top_keys = frozenset({"content"})

    with patch("program.program.bootstrap_apis") as bootstrap:
        program.initialize_apis()

    bootstrap.assert_called_once_with()


def test_initialize_services_remounts_vfs_when_filesystem_changes():
    program = Program()
    previous_filesystem = MagicMock()
    program.services = MagicMock(
        filesystem=previous_filesystem,
        downloader=MagicMock(),
    )

    downloader = _mock_service()
    filesystem = _mock_service()

    settings_manager.last_changed_top_keys = frozenset({"filesystem"})

    _run_initialize(program=program, downloader=downloader, filesystem=filesystem)

    previous_filesystem.close.assert_called_once_with()
    assert program.services.filesystem is filesystem


def test_initialize_services_remounts_vfs_when_downloaders_change():
    program = Program()
    previous_filesystem = MagicMock()
    program.services = MagicMock(
        filesystem=previous_filesystem,
        downloader=MagicMock(),
    )

    downloader = _mock_service()
    filesystem = _mock_service()

    settings_manager.last_changed_top_keys = frozenset({"downloaders", "content"})

    _run_initialize(program=program, downloader=downloader, filesystem=filesystem)

    previous_filesystem.close.assert_called_once_with()
    assert program.services.filesystem is filesystem


@pytest.mark.parametrize("tab", ["ops", "filesystem"])
def test_settings_api_applies_frontend_tab_update_to_service_lifecycle(
    tab: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Exercise the real settings API and diff dispatch without a live mount."""
    fresh = AppModel()
    monkeypatch.setenv("CINEFLOW_FORCE_ENV", "false")
    monkeypatch.setattr(settings_manager, "settings", fresh)
    monkeypatch.setattr(settings_manager, "settings_file", tmp_path / "settings.json")

    program = Program()
    previous_filesystem = MagicMock()
    program.services = MagicMock(filesystem=previous_filesystem, downloader=MagicMock())
    replacement_filesystem = _mock_service()
    changes = []

    def observe():
        changes.append(settings_manager.last_changed_top_keys)
        _run_initialize(
            program=program,
            downloader=_mock_service(),
            filesystem=replacement_filesystem,
        )

    monkeypatch.setattr(settings_manager, "observers", [observe])
    if tab == "ops":
        # This models the initial POST; the frontend separately requests a restart.
        sections = ("post_processing", "logging", "stream")
        payload = fresh.model_dump(mode="json", include=set(sections))
        payload["stream"]["parallel_prefetch_workers"] = 2
        key, value = "stream.parallel_prefetch_workers", 2
        expected_changes = frozenset({"stream"})
    else:
        sections = ("filesystem",)
        payload = fresh.model_dump(mode="json", include=set(sections))
        payload["filesystem"]["hot_cache_watermark_high_pct"] = 82.0
        payload["filesystem"]["hot_cache_watermark_low_pct"] = 65.0
        key, value = "filesystem.hot_cache_watermark_high_pct", 82.0
        expected_changes = frozenset({"filesystem"})

    app = FastAPI()
    app.include_router(settings_router, prefix="/api/v1")
    with TestClient(app) as client:
        saved = client.post(f"/api/v1/settings/set/{','.join(sections)}", json=payload)
        assert saved.status_code == 200, saved.text
        readback = client.get(f"/api/v1/settings/get/{key}")
        assert readback.status_code == 200, readback.text
        assert readback.json()[key] == value

    assert changes == [expected_changes]
    if tab == "ops":
        previous_filesystem.close.assert_not_called()
        assert program.services.filesystem is previous_filesystem
    else:
        previous_filesystem.close.assert_called_once_with()
        assert program.services.filesystem is replacement_filesystem


def test_startup_waits_for_initial_filesystem_sync():
    program = Program()
    filesystem = MagicMock()
    filesystem.wait_until_ready.return_value = True
    program.services = MagicMock(filesystem=filesystem)

    assert program._wait_for_initial_filesystem_sync() is True
    filesystem.wait_until_ready.assert_called_once_with(timeout=None)


def test_startup_rejects_filesystem_sync_failure():
    program = Program()
    filesystem = MagicMock()
    filesystem.wait_until_ready.return_value = False
    program.services = MagicMock(filesystem=filesystem)

    assert program._wait_for_initial_filesystem_sync() is False
    filesystem.wait_until_ready.assert_called_once_with(timeout=None)
