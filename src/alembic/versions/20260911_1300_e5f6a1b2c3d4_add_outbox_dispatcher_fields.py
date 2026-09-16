"""add worker_id, claim_token, started_at, completed_at, and compound indexes to OperationLedger

Revision ID: e5f6a1b2c3d4
Revises: d4e5f6a1b2c3
Create Date: 2026-09-11 13:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5f6a1b2c3d4"
down_revision: Union[str, None] = "d4e5f6a1b2c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("OperationLedger", sa.Column("worker_id", sa.String(length=64), nullable=True))
    op.add_column("OperationLedger", sa.Column("claim_token", sa.String(length=36), nullable=True))
    op.add_column("OperationLedger", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("OperationLedger", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_OperationLedger_claim_token", "OperationLedger", ["claim_token"], unique=False)
    op.create_index("ix_OperationLedger_claim", "OperationLedger", ["status", "scheduled_at"], unique=False)
    op.create_index("ix_OperationLedger_lease", "OperationLedger", ["status", "lease_expires_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_OperationLedger_lease", table_name="OperationLedger")
    op.drop_index("ix_OperationLedger_claim", table_name="OperationLedger")
    op.drop_index("ix_OperationLedger_claim_token", table_name="OperationLedger")
    op.drop_column("OperationLedger", "completed_at")
    op.drop_column("OperationLedger", "started_at")
    op.drop_column("OperationLedger", "claim_token")
    op.drop_column("OperationLedger", "worker_id")

