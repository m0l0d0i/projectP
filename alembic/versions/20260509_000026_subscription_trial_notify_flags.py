"""add notified_trial_* flags на subscriptions (FEA-NOTIF: trial-jobs)

Revision ID: 20260509_000026
Revises: 20260509_000025
Create Date: 2026-05-09 00:00:01.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '20260509_000026'
down_revision = '20260509_000025'
branch_labels = None
depends_on = None


_FLAGS = (
    'notified_trial_mid',
    'notified_trial_last_day',
    'notified_trial_post_expire',
)


def upgrade() -> None:
    for column_name in _FLAGS:
        op.add_column(
            'subscriptions',
            sa.Column(
                column_name,
                sa.Boolean(),
                nullable=False,
                server_default=sa.text('false'),
            ),
        )


def downgrade() -> None:
    for column_name in reversed(_FLAGS):
        op.drop_column('subscriptions', column_name)
