from concurrent.futures import Future
from unittest.mock import MagicMock, patch

from program.managers.event_manager import EventManager
from program.types import Event


def test_submit_job_marks_item_as_running_before_worker_completes():
    event_manager = EventManager()
    event = Event(emitted_by="Downloader", item_id=9316)
    service = MagicMock()
    service.get_key.return_value = "downloader"
    program = MagicMock()
    program.services = {"downloader": MagicMock()}
    executor = MagicMock()
    executor.submit.return_value = Future()

    with (
        patch.object(event_manager, "_find_or_create_executor", return_value=executor),
        patch("program.managers.event_manager.sse_manager.publish_event"),
    ):
        event_manager.submit_job(service, program, event)

    assert event_manager._id_in_running_events(9316)
