"""Add composite index for items endpoint

Revision ID: 52a1b9c3d4e5
Revises: b1345f835923
Create Date: 2026-02-26 23:24:39.123456

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "52a1b9c3d4e5"
down_revision: Union[str, None] = "b1345f835923"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Use concurrent index creation to avoid locking the table during deployment
    op.execute("COMMIT")  # Required for CREATE INDEX CONCURRENTLY
    op.create_index(
        "ix_mediaitem_state_type_date",
        "MediaItem",
        ["last_state", "type", "requested_at"],
        unique=False,
        postgresql_concurrently=True,
    )


def downgrade() -> None:
    op.drop_index("ix_mediaitem_state_type_date", table_name="MediaItem")
