"""Phase 2 hygiene: event-loop offload and scheduler coalesce."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from pydantic import HttpUrl

from program.scheduling.scheduler import ProgramScheduler


def test_upload_logs_offloads_sync_http_to_thread():
    from routers.secure import default as default_router

    fake_url = HttpUrl("https://paste.c-net.org/example")

    with patch.object(
        default_router.asyncio,
        "to_thread",
        new_callable=AsyncMock,
        return_value=fake_url,
    ) as to_thread_mock:
        response = asyncio.run(default_router.upload_logs())

    assert response.success is True
    assert response.url == fake_url
    to_thread_mock.assert_awaited_once_with(default_router._upload_logs_to_paste)

def test_content_service_jobs_coalesce_missed_runs():
    program = MagicMock()

    class FakeContentService(MagicMock):
        pass

    service = FakeContentService()
    service.settings = MagicMock()
    service.settings.use_webhook = False
    service.settings.update_interval = 120
    program.services.content_services = [service]
    program.em.submit_job = MagicMock()

    scheduler = ProgramScheduler(program)
    scheduler.scheduler = MagicMock()

    scheduler._schedule_services()

    scheduler.scheduler.add_job.assert_called_once()
    kwargs = scheduler.scheduler.add_job.call_args.kwargs
    assert kwargs["coalesce"] is True
    assert kwargs["max_instances"] == 1
    assert kwargs["id"] == "FakeContentService_update"
