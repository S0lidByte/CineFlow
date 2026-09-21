"""Unit tests for scheduled retry_library batching (Phase 0/1)."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from program.db.db_functions import retry_library
from program.scheduling.scheduler import ProgramScheduler


class TestRetryLibraryBatch:
    @patch("program.scheduling.scheduler.enqueue_operation")
    @patch("program.scheduling.scheduler.get_active_outbox_item_ids")
    @patch("program.scheduling.scheduler.db_functions.retry_library")
    @patch("program.scheduling.scheduler.settings_manager")
    @patch("program.scheduling.scheduler.db_session")
    def test_scheduler_passes_batch_limit_and_excludes_active(
        self,
        mock_db_session,
        mock_settings,
        mock_retry_library,
        mock_get_active_outbox,
        mock_enqueue_op,
    ):
        mock_session = MagicMock()

        @contextmanager
        def _mock_session_cm():
            yield mock_session

        mock_db_session.side_effect = _mock_session_cm
        mock_settings.settings.retry_library_batch_size = 25
        mock_settings.settings.retry_interval = 600
        mock_get_active_outbox.return_value = {200}
        mock_retry_library.return_value = [10, 11, 12]

        mock_op = MagicMock()
        mock_op.status = "pending"
        mock_op.attempt_count = 0
        mock_enqueue_op.return_value = mock_op

        program = MagicMock()
        program.em.get_active_item_ids.return_value = {99, 100}
        program.em.queue_depth.return_value = 2

        scheduler = ProgramScheduler(program)
        scheduler._retry_library()

        mock_retry_library.assert_called_once_with(
            session=mock_session,
            limit=25,
            exclude_ids={99, 100, 200},
        )
        assert mock_enqueue_op.call_count == 3
        enqueued_item_ids = [
            call.kwargs["media_item_id"] for call in mock_enqueue_op.call_args_list
        ]
        assert enqueued_item_ids == [10, 11, 12]
        mock_session.commit.assert_called_once()
        program.outbox_dispatcher.notify.assert_called_once()

    @patch("program.scheduling.scheduler.notify_outbox_dispatcher")
    @patch("program.scheduling.scheduler.enqueue_operation")
    @patch("program.scheduling.scheduler.get_active_outbox_item_ids")
    @patch("program.scheduling.scheduler.db_functions.retry_library")
    @patch("program.scheduling.scheduler.settings_manager")
    @patch("program.scheduling.scheduler.db_session")
    def test_scheduler_handles_empty_batch(
        self,
        mock_db_session,
        mock_settings,
        mock_retry_library,
        mock_get_active_outbox,
        mock_enqueue_op,
        mock_notify_dispatcher,
    ):
        mock_session = MagicMock()

        @contextmanager
        def _mock_session_cm():
            yield mock_session

        mock_db_session.side_effect = _mock_session_cm
        mock_settings.settings.retry_library_batch_size = 50
        mock_settings.settings.retry_interval = 600
        mock_get_active_outbox.return_value = set()
        mock_retry_library.return_value = []

        program = MagicMock()
        program.em.get_active_item_ids.return_value = set()
        program.em.queue_depth.return_value = 0

        scheduler = ProgramScheduler(program)
        scheduler._retry_library()

        mock_enqueue_op.assert_not_called()
        mock_notify_dispatcher.assert_not_called()


class TestEventManagerActiveIds:
    def test_get_active_item_ids_and_queue_depth(self):
        from threading import Lock

        from program.managers.event_manager import EventManager
        from program.types import Event

        em = EventManager.__new__(EventManager)
        em.mutex = Lock()
        em._queued_events = [
            Event(emitted_by="A", item_id=1),
            Event(emitted_by="B", item_id=2),
        ]
        em._running_events = [Event(emitted_by="C", item_id=3)]

        assert em.queue_depth() == 2
        assert em.get_active_item_ids() == {1, 2, 3}


class TestRetryLibraryQuery:
    def test_rejects_invalid_limit_without_db(self):
        with pytest.raises(ValueError, match="limit must be >= 1"):
            retry_library(limit=0)

    @patch("program.db.db_functions._maybe_session")
    def test_applies_limit_and_exclude_ids(self, mock_maybe_session):
        session = MagicMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = [7, 8]
        session.execute.return_value = result

        @contextmanager
        def _cm(_session=None):
            yield session, False

        mock_maybe_session.side_effect = _cm

        ids = retry_library(limit=2, exclude_ids={3, 4})
        assert ids == [7, 8]

        compiled = session.execute.call_args[0][0].compile()
        compiled_sql = str(compiled).upper()
        assert "LIMIT" in compiled_sql
        assert "NOT IN" in compiled_sql or "NOTIN" in compiled_sql.replace(" ", "")
        assert 2 in compiled.params.values()
        assert any(
            set(value) == {3, 4}
            for value in compiled.params.values()
            if isinstance(value, (list, tuple, set))
        )
