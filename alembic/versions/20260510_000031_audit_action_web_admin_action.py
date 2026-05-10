"""audit_action += web_admin_action (FEA-C39 #2 enforcement)

Revision ID: 20260510_000031
Revises: 20260510_000030
Create Date: 2026-05-10 00:00:03.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '20260510_000031'
down_revision = '20260510_000030'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute(
            sa.text(
                "ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'web_admin_action'"
            ).execution_options(autocommit=True)
        )


def downgrade() -> None:
    # PG не поддерживает удаление enum value; downgrade — no-op.
    pass
