"""add invoice idempotency_key with partial unique index

Revision ID: 20260507_000022
Revises: 20260412_000021
Create Date: 2026-05-07 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '20260507_000022'
down_revision = '20260412_000021'
branch_labels = None
depends_on = None


TABLE_NAME = 'invoices'
COLUMN_NAME = 'idempotency_key'
INDEX_NAME = 'uq_invoices_idempotency_key'


def upgrade() -> None:
    op.add_column(
        TABLE_NAME,
        sa.Column(COLUMN_NAME, sa.String(length=64), nullable=True),
    )
    op.create_index(
        INDEX_NAME,
        TABLE_NAME,
        [COLUMN_NAME],
        unique=True,
        postgresql_where=sa.text(f'{COLUMN_NAME} IS NOT NULL'),
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name=TABLE_NAME)
    op.drop_column(TABLE_NAME, COLUMN_NAME)
