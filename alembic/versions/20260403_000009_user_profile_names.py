"""add user profile name columns

Revision ID: 20260403_000009
Revises: 20260402_000008
Create Date: 2026-04-03 00:10:00
"""
from __future__ import annotations

from alembic import op

revision = '20260403_000009'
down_revision = '20260402_000008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE users
        ADD COLUMN IF NOT EXISTS first_name VARCHAR(128) NULL
        """
    )
    op.execute(
        """
        ALTER TABLE users
        ADD COLUMN IF NOT EXISTS last_name VARCHAR(128) NULL
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS last_name")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS first_name")