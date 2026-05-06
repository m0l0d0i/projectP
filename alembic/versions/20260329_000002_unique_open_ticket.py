"""add partial unique index for one open support ticket per user

Revision ID: 20260329_000002
Revises: 20260328_000001
Create Date: 2026-03-29 00:00:02
"""
from __future__ import annotations

from alembic import op

revision = '20260329_000002'
down_revision = '20260328_000001'
branch_labels = None
depends_on = None

INDEX_NAME = 'uq_support_tickets_one_open_per_user'


def upgrade() -> None:
    op.execute(
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS {INDEX_NAME}
        ON support_tickets (user_id)
        WHERE status = 'open'
        """
    )


def downgrade() -> None:
    op.execute(f'DROP INDEX IF EXISTS {INDEX_NAME}')