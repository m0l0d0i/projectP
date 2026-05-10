"""audit_action +=  subscription_extended/traffic_reset/disabled/enabled
(FEA-ADMIN-SUB-CRM #2)

Revision ID: 20260510_000037
Revises: 20260510_000036
Create Date: 2026-05-10 00:00:09.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '20260510_000037'
down_revision = '20260510_000036'
branch_labels = None
depends_on = None


_NEW_AUDIT_VALUES = (
    'subscription_extended',
    'subscription_traffic_reset',
    'subscription_disabled',
    'subscription_enabled',
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    for value in _NEW_AUDIT_VALUES:
        op.execute(
            sa.text(
                f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{value}'"
            ).execution_options(autocommit=True)
        )


def downgrade() -> None:
    # PG не поддерживает удаление enum value — namespace останется.
    pass
