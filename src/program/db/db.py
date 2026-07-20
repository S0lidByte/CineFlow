import time
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from loguru import logger
from sqla_wrapper import Session, SQLAlchemy
from sqlalchemy import text

from alembic import command
from program.utils import root_dir

from . import db, db_host, engine_options

# Prom: This is a good place to set the statement timeout for the database when debugging.
# @event.listens_for(Engine, "connect")
# def set_statement_timeout(dbapi_connection, connection_record):
#     cursor = dbapi_connection.cursor()
#     cursor.execute("SET statement_timeout = 300000")
#     cursor.close()

# Postgres / network states that usually clear if we wait (crash recovery, boot, brief outages).
_TRANSIENT_DB_ERROR_MARKERS = (
    "in recovery mode",
    "is starting up",
    "connection refused",
    "could not connect to server",
    "server closed the connection unexpectedly",
    "connection timed out",
    "timeout expired",
    "temporarily unavailable",
    "too many connections",
    "remaining connection slots are reserved",
)

DEFAULT_DB_WAIT_TIMEOUT_SECONDS = 120.0
DEFAULT_DB_WAIT_INITIAL_DELAY_SECONDS = 1.0
DEFAULT_DB_WAIT_MAX_DELAY_SECONDS = 8.0


@contextmanager
def db_session() -> Generator[Session, Any, None]:
    with db.Session() as session:
        yield session


def _error_text(exc: BaseException) -> str:
    return str(exc).lower()


def is_transient_database_error(exc: BaseException) -> bool:
    """Return True when the failure is likely temporary (recovery, boot, refused)."""

    msg = _error_text(exc)
    return any(marker in msg for marker in _TRANSIENT_DB_ERROR_MARKERS)


def is_database_missing_error(exc: BaseException) -> bool:
    """Return True when the server is reachable but the target database does not exist."""

    msg = _error_text(exc)
    return "database" in msg and "does not exist" in msg


def is_database_already_exists_error(exc: BaseException) -> bool:
    """Return True when CREATE DATABASE raced with another creator."""

    msg = _error_text(exc)
    return "already exists" in msg


def probe_database() -> None:
    """Execute a simple query against the configured database. Raises on failure."""

    with db_session() as session:
        session.execute(text("SELECT 1"))


def wait_for_database(
    *,
    timeout_seconds: float = DEFAULT_DB_WAIT_TIMEOUT_SECONDS,
    initial_delay: float = DEFAULT_DB_WAIT_INITIAL_DELAY_SECONDS,
    max_delay: float = DEFAULT_DB_WAIT_MAX_DELAY_SECONDS,
) -> None:
    """Block until the configured database accepts queries.

    Retries with exponential backoff on transient errors (recovery mode, connection
    refused, starting up). Re-raises immediately when the database is missing so the
    caller can attempt CREATE DATABASE. Re-raises other permanent errors immediately.
    """

    deadline = time.monotonic() + timeout_seconds
    delay = initial_delay
    attempt = 0

    while True:
        attempt += 1
        try:
            probe_database()
            if attempt > 1:
                logger.success("Database is ready")
            return
        except Exception as exc:
            if is_database_missing_error(exc):
                raise

            if not is_transient_database_error(exc):
                raise

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.error(
                    f"Database still unavailable after {timeout_seconds:.0f}s: {exc}"
                )
                raise

            sleep_for = min(delay, remaining, max_delay)
            logger.warning(
                f"Database not ready yet ({exc}); retrying in {sleep_for:.1f}s "
                f"(attempt {attempt})"
            )
            try:
                db.engine.dispose()
            except Exception:
                pass
            time.sleep(sleep_for)
            delay = min(delay * 2, max_delay)


def create_database_if_not_exists():
    """Create the database if it doesn't exist."""

    db_name = db_host.split("/")[-1]
    db_base_host = "/".join(db_host.split("/")[:-1])

    try:
        temp_db = SQLAlchemy(db_base_host, engine_options=engine_options)

        with temp_db.engine.connect() as connection:
            connection.execution_options(isolation_level="AUTOCOMMIT").execute(
                text(f"CREATE DATABASE {db_name}")
            )

        return True
    except Exception as e:
        if is_database_already_exists_error(e):
            logger.log("DATABASE", f"Database {db_name} already exists")
            return True
        logger.error(f"Failed to create database {db_name}: {e}")
        return False


def vacuum_and_analyze_index_maintenance() -> None:
    try:
        with db.engine.connect() as connection:
            connection = connection.execution_options(isolation_level="AUTOCOMMIT")
            connection.execute(text("VACUUM;"))
            connection.execute(text("ANALYZE;"))

        logger.log("DATABASE", "VACUUM and ANALYZE completed successfully.")
    except Exception as e:
        logger.error(f"Error during VACUUM and ANALYZE: {e}")


def reset_database():
    """Reset the database by dropping and recreating the public schema."""

    logger.warning("Resetting database - all data will be lost!")

    try:
        with db.engine.connect() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
            conn.execute(text("GRANT ALL ON SCHEMA public TO public"))
            conn.commit()

        logger.success("Database reset complete")

        return True
    except Exception as e:
        logger.error(f"Failed to reset database: {e}")
        return False


def run_migrations(database_url: str | None = None):
    """Run any pending migrations on startup.

    If a pre-v1 database is detected (revision not in current migration chain),
    automatically reset the database and create the v1 schema from scratch.

    Special case: Latest dev branch (7e5b5cf430ff) has identical schema to v1_base,
    so we can migrate it directly without data loss.
    """

    try:
        alembic_cfg = Config(root_dir / "src" / "alembic.ini")

        if database_url:
            alembic_cfg.set_main_option("sqlalchemy.url", database_url)

        # Get script directory to check migration chain
        script = ScriptDirectory.from_config(alembic_cfg)

        # Check current database revision
        with db.engine.connect() as conn:
            context = MigrationContext.configure(conn)
            current_rev = context.get_current_revision()

        # Get all revisions in the current migration chain (from base to head)
        # This includes v1_base and any future migrations built on top of it
        head_rev = script.get_current_head()
        current_chain = set[str]()

        if head_rev:
            # Walk down from head to base, collecting all revisions
            for rev in script.walk_revisions(base="base", head=head_rev):
                current_chain.add(rev.revision)

        # Special case: Latest dev branch has identical schema to v1_base
        # Migrate it directly without resetting to preserve user data
        latest_dev_revision = "7e5b5cf430ff"
        v1_base_revision = "4f327e05c40f"

        if current_rev == latest_dev_revision:
            logger.info(f"Detected latest dev branch (revision: {current_rev})")
            logger.info("Migrating to v1 without data loss (schema is identical)")

            # Update alembic_version to v1_base directly
            with db.engine.connect() as conn:
                conn.execute(
                    text(
                        f"UPDATE alembic_version SET version_num = '{v1_base_revision}'"
                    )
                )
                conn.commit()

            logger.success("Migrated from dev branch to v1_base")

            # Continue with normal upgrade
            command.upgrade(alembic_cfg, "head")

            logger.success("Database migrations completed successfully")

            return

        # If database has a revision that's NOT in the current chain, it's pre-v1
        # This handles old v0/dev branches while allowing new v1.x migrations
        if current_rev is not None and current_rev not in current_chain:
            logger.warning(f"Detected pre-v1 database (revision: {current_rev})")
            logger.warning(
                "Upgrading to v1 requires database reset (data cannot be migrated)"
            )
            logger.warning(
                "This affects all pre-v1 databases including v0 releases and dev branches"
            )

            if not reset_database():
                raise Exception("Failed to reset database for v1 upgrade")

            logger.info("Creating v1 schema from scratch...")

        # Run migrations to head (v1 schema)
        command.upgrade(alembic_cfg, "head")
        logger.success("Database migrations completed successfully")

    except Exception as e:
        logger.error(f"Migration failed: {e}")
        raise
