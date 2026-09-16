"""add timeline and status_created_at indexes to OperationLedger

Revision ID: f6a1b2c3d4e5
Revises: e5f6a1b2c3d4
Create Date: 2026-09-16 14:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f6a1b2c3d4e5"
down_revision: Union[str, None] = "e5f6a1b2c3d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_OperationLedger_created_at_desc",
        "OperationLedger",
        [sa.text("created_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_OperationLedger_status_created_at",
        "OperationLedger",
        ["status", sa.text("created_at DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_OperationLedger_status_created_at", table_name="OperationLedger")
    op.drop_index("ix_OperationLedger_created_at_desc", table_name="OperationLedger")
