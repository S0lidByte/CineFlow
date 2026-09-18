"""repair missing OperationLedger claim token schema

Revision ID: a7b2c3d4e5f6
Revises: f6a1b2c3d4e5
Create Date: 2026-09-16 15:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7b2c3d4e5f6"
down_revision: Union[str, None] = "f6a1b2c3d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE_NAME = "OperationLedger"
_COLUMN_NAME = "claim_token"
_INDEX_NAME = "ix_OperationLedger_claim_token"


def _ledger_columns() -> set[str]:
    bind = op.get_bind()
    return {column["name"] for column in sa.inspect(bind).get_columns(_TABLE_NAME)}


def _ledger_indexes() -> set[str]:
    bind = op.get_bind()
    return {index["name"] for index in sa.inspect(bind).get_indexes(_TABLE_NAME)}


def upgrade() -> None:
    """Repair databases stamped at the prior head without the fencing token."""
    if _COLUMN_NAME not in _ledger_columns():
        op.add_column(
            _TABLE_NAME, sa.Column(_COLUMN_NAME, sa.String(length=36), nullable=True)
        )

    if _INDEX_NAME not in _ledger_indexes():
        op.create_index(_INDEX_NAME, _TABLE_NAME, [_COLUMN_NAME], unique=False)


def downgrade() -> None:
    """Repair migration downgrade is a no-op because claim_token is owned by e5f6a1b2c3d4."""
