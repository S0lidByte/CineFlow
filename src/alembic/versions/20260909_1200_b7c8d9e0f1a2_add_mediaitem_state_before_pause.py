"""add MediaItem state before pause

Revision ID: b7c8d9e0f1a2
Revises: a1b2c3d4e5f6
Create Date: 2026-09-09 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7c8d9e0f1a2"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


states_enum = sa.Enum(
    "Unknown",
    "Unreleased",
    "Ongoing",
    "Requested",
    "Indexed",
    "Scraped",
    "Downloaded",
    "Symlinked",
    "Completed",
    "PartiallyCompleted",
    "Failed",
    "Paused",
    name="states",
    create_type=False,
)


def upgrade() -> None:
    op.add_column(
        "MediaItem",
        sa.Column("state_before_pause", states_enum, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("MediaItem", "state_before_pause")
