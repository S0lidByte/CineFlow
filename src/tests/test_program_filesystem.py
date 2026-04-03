from unittest.mock import MagicMock, patch

from program.program import Program


def _mock_service(*, enabled: bool = True, initialized: bool = True) -> MagicMock:
    service = MagicMock()
    service.enabled = enabled
    service.initialized = initialized
    service.is_content_service = False
    return service


def test_initialize_services_closes_previous_filesystem_service():
    program = Program()
    previous_filesystem = MagicMock()
    program.services = MagicMock(filesystem=previous_filesystem)

    downloader = _mock_service()
    filesystem = _mock_service()

    with (
        patch("program.program.Overseerr", return_value=_mock_service()),
        patch("program.program.PlexWatchlist", return_value=_mock_service()),
        patch("program.program.Listrr", return_value=_mock_service()),
        patch("program.program.Mdblist", return_value=_mock_service()),
        patch("program.program.TraktContent", return_value=_mock_service()),
        patch("program.program.IndexerService", return_value=_mock_service()),
        patch("program.program.Scraping", return_value=_mock_service()),
        patch("program.program.Updater", return_value=_mock_service()),
        patch("program.program.Downloader", return_value=downloader),
        patch("program.program.FilesystemService", return_value=filesystem),
        patch("program.program.PostProcessing", return_value=_mock_service()),
        patch("program.program.NotificationService", return_value=_mock_service()),
    ):
        program.initialize_services()

    previous_filesystem.close.assert_called_once_with()
    assert program.services is not None
    assert program.services.filesystem is filesystem
