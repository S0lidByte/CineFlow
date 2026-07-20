"""Unit tests for Postgres startup readiness (recovery mode / missing DB)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from psycopg2 import OperationalError as Psycopg2OperationalError
from sqlalchemy.exc import OperationalError

from program.db.db import (
    create_database_if_not_exists,
    is_database_already_exists_error,
    is_database_missing_error,
    is_transient_database_error,
    wait_for_database,
)
from program.program import Program


def _sa_operational(message: str) -> OperationalError:
    """Build a SQLAlchemy OperationalError wrapping a psycopg2-style message."""

    return OperationalError(
        statement="SELECT 1",
        params=None,
        orig=Psycopg2OperationalError(message),
    )


class TestDatabaseErrorClassification:
    def test_recovery_mode_is_transient(self):
        exc = _sa_operational(
            'connection to server at "riven-db" (172.21.0.8), port 5432 failed: '
            "FATAL:  the database system is in recovery mode"
        )
        assert is_transient_database_error(exc)
        assert not is_database_missing_error(exc)

    def test_starting_up_is_transient(self):
        exc = _sa_operational("FATAL:  the database system is starting up")
        assert is_transient_database_error(exc)

    def test_connection_refused_is_transient(self):
        exc = _sa_operational(
            'connection to server at "riven-db", port 5432 failed: Connection refused'
        )
        assert is_transient_database_error(exc)

    def test_hostname_dns_failure_is_transient(self):
        exc = _sa_operational(
            'could not translate host name "riven-db" to address: Name does not resolve'
        )
        assert is_transient_database_error(exc)
        assert not is_database_missing_error(exc)

    def test_missing_database_is_not_transient(self):
        exc = _sa_operational('FATAL:  database "riven" does not exist')
        assert is_database_missing_error(exc)
        assert not is_transient_database_error(exc)

    def test_already_exists_detection(self):
        exc = Exception('database "riven" already exists')
        assert is_database_already_exists_error(exc)


class TestWaitForDatabase:
    @patch("program.db.db.time.sleep")
    @patch("program.db.db.db")
    @patch("program.db.db.probe_database")
    def test_retries_recovery_mode_then_succeeds(
        self, mock_probe, mock_db, mock_sleep
    ):
        recovery = _sa_operational(
            "FATAL:  the database system is in recovery mode"
        )
        mock_probe.side_effect = [recovery, recovery, None]

        wait_for_database(
            timeout_seconds=30.0,
            initial_delay=0.01,
            max_delay=0.05,
        )

        assert mock_probe.call_count == 3
        assert mock_sleep.call_count == 2
        assert mock_db.engine.dispose.call_count == 2

    @patch("program.db.db.time.sleep")
    @patch("program.db.db.db")
    @patch("program.db.db.probe_database")
    def test_raises_immediately_when_database_missing(
        self, mock_probe, mock_db, mock_sleep
    ):
        missing = _sa_operational('FATAL:  database "riven" does not exist')
        mock_probe.side_effect = missing

        with pytest.raises(OperationalError):
            wait_for_database(timeout_seconds=10.0)

        mock_probe.assert_called_once()
        mock_sleep.assert_not_called()
        mock_db.engine.dispose.assert_not_called()

    @patch("program.db.db.time.sleep")
    @patch("program.db.db.db")
    @patch("program.db.db.probe_database")
    def test_times_out_on_persistent_recovery(
        self, mock_probe, mock_db, mock_sleep
    ):
        recovery = _sa_operational(
            "FATAL:  the database system is in recovery mode"
        )
        mock_probe.side_effect = recovery

        with patch("program.db.db.time.monotonic", side_effect=[0.0, 0.0, 100.0]):
            with pytest.raises(OperationalError):
                wait_for_database(
                    timeout_seconds=1.0,
                    initial_delay=0.01,
                    max_delay=0.01,
                )


class TestCreateDatabaseIfNotExists:
    @patch("program.db.db.SQLAlchemy")
    @patch("program.db.db.db_host", "postgresql+psycopg2://postgres:postgres@riven-db/riven")
    def test_treats_already_exists_as_success(self, mock_sqlalchemy):
        engine = MagicMock()
        connection = MagicMock()
        mock_sqlalchemy.return_value.engine = engine
        engine.connect.return_value.__enter__.return_value = connection
        connection.execution_options.return_value.execute.side_effect = Exception(
            'database "riven" already exists'
        )

        assert create_database_if_not_exists() is True


class TestProgramEnsureDatabaseReady:
    @patch("program.program.wait_for_database")
    def test_success_path(self, mock_wait):
        program = Program.__new__(Program)
        assert program._ensure_database_ready() is True
        mock_wait.assert_called_once_with()

    @patch("program.program.create_database_if_not_exists", return_value=True)
    @patch("program.program.wait_for_database")
    def test_creates_when_missing_then_waits(
        self, mock_wait, mock_create
    ):
        missing = _sa_operational('FATAL:  database "riven" does not exist')
        mock_wait.side_effect = [missing, None]

        program = Program.__new__(Program)
        assert program._ensure_database_ready() is True

        mock_create.assert_called_once()
        assert mock_wait.call_count == 2
        mock_wait.assert_any_call(timeout_seconds=30.0)

    @patch("program.program.create_database_if_not_exists")
    @patch("program.program.wait_for_database")
    def test_does_not_create_on_recovery_timeout(
        self, mock_wait, mock_create
    ):
        recovery = _sa_operational(
            "FATAL:  the database system is in recovery mode"
        )
        mock_wait.side_effect = recovery

        program = Program.__new__(Program)
        assert program._ensure_database_ready() is False

        mock_create.assert_not_called()
