"""add subscription cycle and current-cycle extra traffic fields

Revision ID: 20260405_000011
Revises: 20260403_000010
Create Date: 2026-04-05 06:00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '20260405_000011'
down_revision = '20260403_000010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'subscriptions',
        sa.Column('traffic_cycle_start_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'subscriptions',
        sa.Column('traffic_cycle_end_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'subscriptions',
        sa.Column('traffic_cycle_base_bytes', sa.BigInteger(), nullable=True),
    )
    op.add_column(
        'subscriptions',
        sa.Column(
            'cycle_extra_traffic_bytes',
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text('0'),
        ),
    )
    op.add_column(
        'subscriptions',
        sa.Column('last_traffic_reset_at', sa.DateTime(timezone=True), nullable=True),
    )

    op.create_index(
        'ix_subscriptions_traffic_cycle_start_at',
        'subscriptions',
        ['traffic_cycle_start_at'],
    )
    op.create_index(
        'ix_subscriptions_traffic_cycle_end_at',
        'subscriptions',
        ['traffic_cycle_end_at'],
    )

    op.execute(
        sa.text(
            """
            UPDATE subscriptions
            SET
                traffic_cycle_end_at = COALESCE(
                    next_traffic_reset_at,
                    CASE
                        WHEN expire_date IS NOT NULL AND expire_date > created_at THEN expire_date
                        ELSE created_at + INTERVAL '30 days'
                    END
                ),
                traffic_cycle_start_at = CASE
                    WHEN next_traffic_reset_at IS NOT NULL THEN next_traffic_reset_at - INTERVAL '1 month'
                    ELSE created_at
                END,
                traffic_cycle_base_bytes = CASE
                    WHEN monthly_traffic_bytes IS NOT NULL THEN monthly_traffic_bytes
                    WHEN data_limit_bytes IS NULL OR data_limit_bytes = 0 THEN NULL
                    ELSE data_limit_bytes
                END,
                cycle_extra_traffic_bytes = CASE
                    WHEN monthly_traffic_bytes IS NULL OR monthly_traffic_bytes <= 0 THEN 0
                    WHEN data_limit_bytes IS NULL OR data_limit_bytes <= monthly_traffic_bytes THEN 0
                    ELSE data_limit_bytes - monthly_traffic_bytes
                END,
                last_traffic_reset_at = CASE
                    WHEN next_traffic_reset_at IS NOT NULL THEN next_traffic_reset_at - INTERVAL '1 month'
                    ELSE created_at
                END
            """
        )
    )

    op.execute(
        sa.text(
            """
            UPDATE subscriptions
            SET traffic_cycle_end_at = traffic_cycle_start_at + INTERVAL '30 days'
            WHERE traffic_cycle_start_at IS NOT NULL
              AND traffic_cycle_end_at IS NOT NULL
              AND traffic_cycle_end_at <= traffic_cycle_start_at
            """
        )
    )

    op.create_check_constraint(
        'ck_subscriptions_monthly_traffic_non_negative',
        'subscriptions',
        'monthly_traffic_bytes IS NULL OR monthly_traffic_bytes >= 0',
    )
    op.create_check_constraint(
        'ck_subscriptions_cycle_base_non_negative',
        'subscriptions',
        'traffic_cycle_base_bytes IS NULL OR traffic_cycle_base_bytes >= 0',
    )
    op.create_check_constraint(
        'ck_subscriptions_cycle_extra_traffic_non_negative',
        'subscriptions',
        'cycle_extra_traffic_bytes >= 0',
    )
    op.create_check_constraint(
        'ck_subscriptions_traffic_cycle_bounds_valid',
        'subscriptions',
        """
        (
            traffic_cycle_start_at IS NULL
            AND traffic_cycle_end_at IS NULL
        )
        OR (
            traffic_cycle_start_at IS NOT NULL
            AND traffic_cycle_end_at IS NOT NULL
            AND traffic_cycle_end_at > traffic_cycle_start_at
        )
        """,
    )


def downgrade() -> None:
    op.drop_constraint('ck_subscriptions_traffic_cycle_bounds_valid', 'subscriptions', type_='check')
    op.drop_constraint('ck_subscriptions_cycle_extra_traffic_non_negative', 'subscriptions', type_='check')
    op.drop_constraint('ck_subscriptions_cycle_base_non_negative', 'subscriptions', type_='check')
    op.drop_constraint('ck_subscriptions_monthly_traffic_non_negative', 'subscriptions', type_='check')

    op.drop_index('ix_subscriptions_traffic_cycle_end_at', table_name='subscriptions')
    op.drop_index('ix_subscriptions_traffic_cycle_start_at', table_name='subscriptions')

    op.drop_column('subscriptions', 'last_traffic_reset_at')
    op.drop_column('subscriptions', 'cycle_extra_traffic_bytes')
    op.drop_column('subscriptions', 'traffic_cycle_base_bytes')
    op.drop_column('subscriptions', 'traffic_cycle_end_at')
    op.drop_column('subscriptions', 'traffic_cycle_start_at')
