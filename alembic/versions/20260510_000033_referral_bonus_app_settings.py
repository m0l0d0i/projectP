"""referral_inviter_bonus / referral_invited_bonus в app_settings (FEA-A6)

Revision ID: 20260510_000033
Revises: 20260510_000032
Create Date: 2026-05-10 00:00:05.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '20260510_000033'
down_revision = '20260510_000032'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'app_settings',
        sa.Column(
            'referral_inviter_bonus',
            sa.Numeric(10, 2),
            nullable=False,
            server_default=sa.text('50.00'),
        ),
    )
    op.add_column(
        'app_settings',
        sa.Column(
            'referral_invited_bonus',
            sa.Numeric(10, 2),
            nullable=False,
            server_default=sa.text('50.00'),
        ),
    )
    op.create_check_constraint(
        'ck_app_settings_referral_inviter_bonus_non_negative',
        'app_settings',
        'referral_inviter_bonus >= 0',
    )
    op.create_check_constraint(
        'ck_app_settings_referral_invited_bonus_non_negative',
        'app_settings',
        'referral_invited_bonus >= 0',
    )
    op.execute(
        sa.text(
            "ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'referral_settings_updated'"
        ).execution_options(autocommit=True)
    )


def downgrade() -> None:
    op.drop_constraint(
        'ck_app_settings_referral_invited_bonus_non_negative',
        'app_settings',
        type_='check',
    )
    op.drop_constraint(
        'ck_app_settings_referral_inviter_bonus_non_negative',
        'app_settings',
        type_='check',
    )
    op.drop_column('app_settings', 'referral_invited_bonus')
    op.drop_column('app_settings', 'referral_inviter_bonus')
