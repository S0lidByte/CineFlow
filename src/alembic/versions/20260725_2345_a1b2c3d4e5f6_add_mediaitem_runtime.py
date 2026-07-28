"""Add MediaItem.runtime for bitrate floors

Revision ID: a1b2c3d4e5f6
Revises: c8f6e2a1b4d9
Create Date: 2026-07-25 23:45:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "c8f6e2a1b4d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "MediaItem",
        sa.Column("runtime", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("MediaItem", "runtime")
