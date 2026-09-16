"""Comprehensive migration and schema integrity tests for OperationLedger indexes (OUTBOX-011)."""

from __future__ import annotations

import os
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from alembic import command
from program.contracts.operation_ledger import OperationLedger


@pytest.fixture
def alembic_config():
    """Create an Alembic Config pointed at the repository migration scripts."""
    backend_src = Path(__file__).resolve().parent.parent
    ini_path = backend_src / "alembic.ini"
    cfg = Config(str(ini_path))
    cfg.set_main_option("script_location", str(backend_src / "alembic"))
    return cfg


@pytest.fixture
def temp_db_file():
    """Create a temporary SQLite database file for migration-cycle tests."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _prepare_previous_revision(cfg: Config, db_url: str):
    """Build the exact model schema at e5f6a1b2c3d4 and stamp that revision."""
    engine = create_engine(db_url)
    OperationLedger.__table__.create(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP INDEX ix_OperationLedger_status_created_at"))
        connection.execute(text("DROP INDEX ix_OperationLedger_created_at_desc"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.stamp(cfg, "e5f6a1b2c3d4")
    return engine


def _index_names(engine: sa.Engine) -> set[str]:
    return {str(index["name"]) for index in inspect(engine).get_indexes("OperationLedger")}


def _column_names(engine: sa.Engine) -> set[str]:
    return {str(column["name"]) for column in inspect(engine).get_columns("OperationLedger")}


def _sqlite_index_sql(engine: sa.Engine, name: str) -> str:
    with engine.connect() as connection:
        definition = connection.scalar(
            text("SELECT sql FROM sqlite_master WHERE type = 'index' AND name = :name"),
            {"name": name},
        )
    assert isinstance(definition, str)
    return " ".join(definition.split())


class TestOperationLedgerMigrationIntegrity:
    def test_repair_migration_restores_missing_claim_token_schema(self, alembic_config, temp_db_file):
        """Repair a falsely stamped prior head without dropping existing ledger data."""
        db_url = f"sqlite:///{temp_db_file}"
        engine = _prepare_previous_revision(alembic_config, db_url)

        with engine.begin() as connection:
            connection.execute(text("DROP INDEX ix_OperationLedger_claim_token"))
            connection.execute(text("ALTER TABLE OperationLedger DROP COLUMN claim_token"))

        command.stamp(alembic_config, "f6a1b2c3d4e5")
        command.upgrade(alembic_config, "head")

        assert "claim_token" in _column_names(engine)
        assert "ix_OperationLedger_claim_token" in _index_names(engine)

    def test_upgrade_and_exact_index_definitions(self, alembic_config, temp_db_file):
        """Upgrade the prior head and verify all four required indexes exactly once."""
        db_url = f"sqlite:///{temp_db_file}"
        engine = _prepare_previous_revision(alembic_config, db_url)

        command.upgrade(alembic_config, "head")

        names = _index_names(engine)
        assert "ix_OperationLedger_claim" in names
        assert "ix_OperationLedger_lease" in names
        assert "ix_OperationLedger_created_at_desc" in names
        assert "ix_OperationLedger_status_created_at" in names

        with engine.connect() as connection:
            duplicates = connection.execute(
                text(
                    "SELECT name, COUNT(*) FROM sqlite_master "
                    "WHERE type = 'index' AND name IN "
                    "('ix_OperationLedger_claim', 'ix_OperationLedger_lease', "
                    "'ix_OperationLedger_created_at_desc', "
                    "'ix_OperationLedger_status_created_at') GROUP BY name"
                )
            ).all()
        assert {name: count for name, count in duplicates} == {
            "ix_OperationLedger_claim": 1,
            "ix_OperationLedger_lease": 1,
            "ix_OperationLedger_created_at_desc": 1,
            "ix_OperationLedger_status_created_at": 1,
        }

        assert _sqlite_index_sql(engine, "ix_OperationLedger_created_at_desc").endswith(
            "(created_at DESC)"
        )
        assert _sqlite_index_sql(engine, "ix_OperationLedger_status_created_at").endswith(
            "(status, created_at DESC)"
        )

    def test_downgrade_and_reupgrade_cycle(self, alembic_config, temp_db_file):
        """Verify clean rollback to e5f6a1b2c3d4 and idempotent re-upgrade to f6a1b2c3d4e5."""
        db_url = f"sqlite:///{temp_db_file}"
        engine = _prepare_previous_revision(alembic_config, db_url)

        # 1. Upgrade to head
        command.upgrade(alembic_config, "head")

        initial_indexes = _index_names(engine)
        assert "ix_OperationLedger_created_at_desc" in initial_indexes
        assert "ix_OperationLedger_status_created_at" in initial_indexes

        # 2. Downgrade 1 revision back to e5f6a1b2c3d4
        command.downgrade(alembic_config, "e5f6a1b2c3d4")

        downgraded_indexes = _index_names(engine)
        assert "ix_OperationLedger_created_at_desc" not in downgraded_indexes
        assert "ix_OperationLedger_status_created_at" not in downgraded_indexes
        # Prior indexes must still be present
        assert "ix_OperationLedger_claim" in downgraded_indexes
        assert "ix_OperationLedger_lease" in downgraded_indexes

        # 3. Re-upgrade back to head
        command.upgrade(alembic_config, "f6a1b2c3d4e5")

        reupgraded_indexes = _index_names(engine)
        assert "ix_OperationLedger_created_at_desc" in reupgraded_indexes
        assert "ix_OperationLedger_status_created_at" in reupgraded_indexes
        assert "ix_OperationLedger_claim" in reupgraded_indexes
        assert "ix_OperationLedger_lease" in reupgraded_indexes

    def test_populated_database_migration_and_query_plans(self, alembic_config, temp_db_file):
        """Verify data preservation across upgrade/downgrade and timeline index query execution."""
        db_url = f"sqlite:///{temp_db_file}"
        engine = _prepare_previous_revision(alembic_config, db_url)
        inserted_ids = []

        # Seed data at previous revision
        with Session(engine) as session:
            now = datetime.now(UTC)
            for i in range(15):
                rec_id = str(uuid.uuid4())
                inserted_ids.append(rec_id)
                status = "completed" if i % 2 == 0 else "pending"
                record = OperationLedger(
                    id=rec_id,
                    correlation_id=str(uuid.uuid4()),
                    operation_type="scrape" if i % 3 == 0 else "download",
                    status=status,
                    created_at=now - timedelta(minutes=i),
                    scheduled_at=now - timedelta(minutes=i),
                    payload={"index": i, "secret": "sensitive_data_123"},
                )
                session.add(record)
            session.commit()

        # 2. Upgrade to f6a1b2c3d4e5 with populated rows
        command.upgrade(alembic_config, "f6a1b2c3d4e5")

        # 3. Verify all records survived with zero corruption
        with Session(engine) as session:
            count = session.scalar(select(sa.func.count(OperationLedger.id)))
            assert count == 15

            # Test timeline query (ordered by created_at DESC)
            timeline_query = select(OperationLedger).order_by(OperationLedger.created_at.desc())
            records = session.scalars(timeline_query).all()
            assert len(records) == 15
            # Verify descending order
            for i in range(len(records) - 1):
                assert records[i].created_at >= records[i + 1].created_at

            # Test filtered timeline query (status + created_at DESC)
            filtered_query = (
                select(OperationLedger)
                .where(OperationLedger.status == "completed")
                .order_by(OperationLedger.created_at.desc())
            )
            completed_records = session.scalars(filtered_query).all()
            assert len(completed_records) == 8
            for rec in completed_records:
                assert rec.status == "completed"

        # 4. Verify EXPLAIN QUERY PLAN uses indexes on SQLite
        with engine.connect() as conn:
            plan_timeline = conn.execute(
                text("EXPLAIN QUERY PLAN SELECT id FROM OperationLedger ORDER BY created_at DESC")
            ).fetchall()
            plan_timeline_str = " ".join(str(row) for row in plan_timeline)
            # Should mention SCAN or SEARCH using ix_OperationLedger_created_at_desc
            assert "ix_OperationLedger_created_at_desc" in plan_timeline_str

            plan_status_timeline = conn.execute(
                text("EXPLAIN QUERY PLAN SELECT id FROM OperationLedger WHERE status = 'pending' ORDER BY created_at DESC")
            ).fetchall()
            plan_status_timeline_str = " ".join(str(row) for row in plan_status_timeline)
            # Should mention ix_OperationLedger_status_created_at
            assert "ix_OperationLedger_status_created_at" in plan_status_timeline_str

        # 5. Downgrade and ensure data is still 100% intact
        command.downgrade(alembic_config, "e5f6a1b2c3d4")
        with Session(engine) as session:
            count = session.scalar(select(sa.func.count(OperationLedger.id)))
            assert count == 15

        # 6. Re-upgrade and verify final state
        command.upgrade(alembic_config, "head")
        with Session(engine) as session:
            count = session.scalar(select(sa.func.count(OperationLedger.id)))
            assert count == 15
