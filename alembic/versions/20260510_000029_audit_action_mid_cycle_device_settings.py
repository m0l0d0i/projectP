"""audit_action += mid_cycle_device_settings_updated (FEA-A9 admin)

Revision ID: 20260510_000029
Revises: 20260510_000028
Create Date: 2026-05-10 00:00:01.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '20260510_000029'
down_revision = '20260510_000028'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute(
            sa.text(
                "ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'mid_cycle_device_settings_updated'"
            ).execution_options(autocommit=True)
        )


def downgrade() -> None:
    # PG не поддерживает удаление enum value; downgrade — no-op,
    # как и для других миграций audit_action в этом проекте.
    pass
