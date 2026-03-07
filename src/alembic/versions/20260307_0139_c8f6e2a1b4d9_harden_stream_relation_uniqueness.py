"""Harden stream relation uniqueness and cleanup duplicates

Revision ID: c8f6e2a1b4d9
Revises: 52a1b9c3d4e5
Create Date: 2026-03-07 01:39:00.000000

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8f6e2a1b4d9"
down_revision: Union[str, None] = "52a1b9c3d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Remove duplicates before adding unique indexes.
    # Keep the lowest row id for each duplicate tuple.
    op.execute("""
        DELETE FROM "StreamRelation" sr
        USING "StreamRelation" sr_keep
        WHERE sr.parent_id = sr_keep.parent_id
          AND sr.child_id = sr_keep.child_id
          AND sr.id > sr_keep.id;
        """)

    op.execute("""
        DELETE FROM "StreamBlacklistRelation" sbr
        USING "StreamBlacklistRelation" sbr_keep
        WHERE sbr.media_item_id = sbr_keep.media_item_id
          AND sbr.stream_id = sbr_keep.stream_id
          AND sbr.id > sbr_keep.id;
        """)

    op.create_index(
        "uq_streamrelation_parent_id_child_id",
        "StreamRelation",
        ["parent_id", "child_id"],
        unique=True,
    )
    op.create_index(
        "uq_streamblacklistrelation_media_item_id_stream_id",
        "StreamBlacklistRelation",
        ["media_item_id", "stream_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_streamblacklistrelation_media_item_id_stream_id",
        table_name="StreamBlacklistRelation",
    )
    op.drop_index(
        "uq_streamrelation_parent_id_child_id",
        table_name="StreamRelation",
    )
