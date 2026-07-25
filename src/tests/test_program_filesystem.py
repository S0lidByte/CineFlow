from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

from program.program import Program
from program.settings import settings_manager


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
