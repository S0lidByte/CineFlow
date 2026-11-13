"""Utility for triggering graceful server and service restarts."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from typing import Any

from kink import di
from loguru import logger


def schedule_backend_restart(delay_seconds: float = 0.5) -> None:
    """
    Schedule a graceful backend restart after a short delay.

    The short delay allows any pending HTTP response (e.g. 200 OK to the client)
    to complete before the process terminates.

    In production/Docker container setups with `restart: unless-stopped`,
    terminating the process causes the container orchestrator to immediately
    re-launch the backend with freshly loaded settings from disk.

    In pytest/unit test environments, process termination is safely skipped.
    """

    async def _do_restart() -> None:
        await asyncio.sleep(delay_seconds)
        logger.info("Backend restart initiated via API. Stopping CineFlow program...")
        try:
            from program.program import Program

            if Program in di:
                program_inst: Any = di[Program]
                if hasattr(program_inst, "stop"):
                    program_inst.stop()
        except Exception as exc:
            logger.warning(f"Error while stopping program during restart: {exc}")

        # In testing/pytest environments, do not terminate the test process
        if "pytest" in sys.modules or os.environ.get("PYTEST_CURRENT_TEST"):
            logger.info(
                "Restart requested in pytest test environment; skipping process termination."
            )
            return

        try:
            os.kill(os.getpid(), signal.SIGTERM)
        except Exception:
            os._exit(0)

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_do_restart())
    except RuntimeError:
        pass
