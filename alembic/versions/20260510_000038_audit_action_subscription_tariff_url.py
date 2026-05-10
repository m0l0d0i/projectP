"""audit_action += subscription_tariff_changed/subscription_url_reissued
(FEA-ADMIN-SUB-CRM #3)

Revision ID: 20260510_000038
Revises: 20260510_000037
Create Date: 2026-05-10 00:00:10.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '20260510_000038'
down_revision = '20260510_000037'
branch_labels = None
depends_on = None


_NEW_AUDIT_VALUES = (
    'subscription_tariff_changed',
    'subscription_url_reissued',
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
    pass
