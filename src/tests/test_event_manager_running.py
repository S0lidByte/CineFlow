from concurrent.futures import Future
from unittest.mock import MagicMock, patch

from program.managers.event_manager import EventManager, FutureWithEvent
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


def test_process_future_queues_next_event_after_clearing_running():
    """Scraped items must hand off to Downloader; self-dedupe must not block that."""

    event_manager = EventManager()
    completed = Event(emitted_by="Scraping", item_id=9321)
    event_manager.add_event_to_running(completed)

    future = Future()
    future.set_result(9321)
    future_with_event = FutureWithEvent(
        future=future,
        event=completed,
        cancellation_event=MagicMock(is_set=MagicMock(return_value=False)),
    )
    event_manager._futures.append(future_with_event)
    service = MagicMock()
    service.__class__.__name__ = "Scraping"

    with (
        patch.object(
            event_manager,
            "add_event_to_queue",
            side_effect=lambda event, log_message=True: (
                event_manager._queued_events.append(event)
            ),
        ),
        patch("program.managers.event_manager.sse_manager.publish_event"),
        patch(
            "program.managers.event_manager.db_functions.get_item_ids",
            return_value=(9321, []),
        ),
        patch("program.managers.event_manager.db_session"),
    ):
        event_manager._process_future(future_with_event, service)

    assert event_manager._id_in_queue(9321), (
        "next-stage event must be queued after scrape completes "
        "(must not self-dedupe against the just-finished running job)"
    )
    assert not event_manager._id_in_running_events(9321)
