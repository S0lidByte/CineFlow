"""add OperationLedger table for durable execution outbox

Revision ID: d4e5f6a1b2c3
Revises: b7c8d9e0f1a2
Create Date: 2026-09-11 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4e5f6a1b2c3"
down_revision: Union[str, None] = "b7c8d9e0f1a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "OperationLedger",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("correlation_id", sa.String(length=128), nullable=False),
        sa.Column("media_item_id", sa.Integer(), nullable=True),
        sa.Column("operation_type", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("idempotency_key", sa.String(length=256), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_classification", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("ix_OperationLedger_media_item_id", "OperationLedger", ["media_item_id"], unique=False)
    op.create_index("ix_OperationLedger_status", "OperationLedger", ["status"], unique=False)
    op.create_index("ix_OperationLedger_scheduled_at", "OperationLedger", ["scheduled_at"], unique=False)
    op.create_index("ix_OperationLedger_lease_expires_at", "OperationLedger", ["lease_expires_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_OperationLedger_lease_expires_at", table_name="OperationLedger")
    op.drop_index("ix_OperationLedger_scheduled_at", table_name="OperationLedger")
    op.drop_index("ix_OperationLedger_status", table_name="OperationLedger")
    op.drop_index("ix_OperationLedger_media_item_id", table_name="OperationLedger")
    op.drop_table("OperationLedger")
