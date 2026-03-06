from sqlalchemy import MetaData, orm


class Base(orm.DeclarativeBase):
    """Base class for all database models"""


def get_base_metadata() -> MetaData:
    """Get the Base metadata for Alembic migrations"""

    # Import models to register them with Base.metadata

    return Base.metadata
