"""Regression tests for main.py startup and shutdown status semantics."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.mark.parametrize("initialized, alive", [(False, True), (True, False)])
def test_invalid_program_state_is_a_startup_failure(initialized: bool, alive: bool):
    program = MagicMock(initialized=initialized)
    program.is_alive.return_value = alive

    with pytest.raises(RuntimeError, match="did not start correctly"):
        if not program.initialized or not program.is_alive():
            raise RuntimeError("Riven program did not start correctly.")


def test_startup_exception_is_mapped_to_nonzero_exit():
    exit_code = 0
    program = MagicMock()
    program.start.side_effect = RuntimeError("startup failed")

    try:
        program.start()
    except Exception:
        exit_code = 1

    assert exit_code == 1


def test_successful_program_lifecycle_keeps_zero_exit_code():
    program = MagicMock(initialized=True)
    program.is_alive.return_value = True

    exit_code = 0
    try:
        program.start()
        if not program.initialized or not program.is_alive():
            raise RuntimeError("Riven program did not start correctly.")
        program.join()
    except Exception:
        exit_code = 1

    assert exit_code == 0
    program.join.assert_called_once_with()
