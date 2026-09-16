from sqlalchemy import MetaData, orm


class Base(orm.DeclarativeBase):
    """Base class for all database models"""


def get_base_metadata() -> MetaData:
    """Get the Base metadata for Alembic migrations"""

    # Import models to register them with Base.metadata
    from program.contracts.operation_ledger import OperationLedger
    from program.media.filesystem_entry import FilesystemEntry
    from program.media.item import MediaItem
    from program.media.stream import (
        Stream,
        StreamBlacklistRelation,
        StreamRelation,
    )
    from program.scheduling.models import ScheduledTask

    _ = (
        OperationLedger,
        FilesystemEntry,
        MediaItem,
        Stream,
        StreamBlacklistRelation,
        StreamRelation,
        ScheduledTask,
    )

    return Base.metadata
