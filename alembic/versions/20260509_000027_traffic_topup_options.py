"""add traffic_topup_options table with default seed (FEA-A8)

Revision ID: 20260509_000027
Revises: 20260509_000026
Create Date: 2026-05-09 00:00:02.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '20260509_000027'
down_revision = '20260509_000026'
branch_labels = None
depends_on = None


TABLE_NAME = 'traffic_topup_options'


# Seed: коды topup50/topup100 сохранены 1:1 c хардкодом в
# `low_traffic_alert_keyboard` (callbacks из smart-push'ей FEA-NOTIF
# перестанут работать, если их переименовать). topup200 — новый вариант
# по более выгодной цене за ГБ (FEA-A8 «3-й вариант пакета»).
_DEFAULT_OPTIONS: list[dict] = [
    {
        'code': 'topup50',
        'title': '+50 ГБ',
        'extra_traffic_gb': 50,
        'amount': 62,
        'is_enabled': True,
        'sort_order': 10,
        'badge_label': None,
    },
    {
        'code': 'topup100',
        'title': '+100 ГБ',
        'extra_traffic_gb': 100,
        'amount': 125,
        'is_enabled': True,
        'sort_order': 20,
        'badge_label': None,
    },
    {
        'code': 'topup200',
        'title': '+200 ГБ',
        'extra_traffic_gb': 200,
        'amount': 230,
        'is_enabled': True,
        'sort_order': 30,
        'badge_label': None,
    },
]


def upgrade() -> None:
    op.create_table(
        TABLE_NAME,
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('code', sa.String(length=32), nullable=False),
        sa.Column('title', sa.String(length=64), nullable=False),
        sa.Column('extra_traffic_gb', sa.Integer(), nullable=False),
        sa.Column('amount', sa.Numeric(10, 2), nullable=False),
        sa.Column(
            'is_enabled',
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            'sort_order',
            sa.Integer(),
            nullable=False,
            server_default=sa.text('100'),
        ),
        sa.Column('badge_label', sa.String(length=64), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('CURRENT_TIMESTAMP'),
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('CURRENT_TIMESTAMP'),
        ),
        sa.CheckConstraint(
            'extra_traffic_gb > 0',
            name='ck_traffic_topup_options_extra_positive',
        ),
        sa.CheckConstraint(
            'amount >= 0',
            name='ck_traffic_topup_options_amount_non_negative',
        ),
        sa.UniqueConstraint('code', name='uq_traffic_topup_options_code'),
    )

    op.create_index(
        'ix_traffic_topup_options_enabled_sort',
        TABLE_NAME,
        ['is_enabled', 'sort_order'],
    )

    options_table = sa.table(
        TABLE_NAME,
        sa.column('code', sa.String()),
        sa.column('title', sa.String()),
        sa.column('extra_traffic_gb', sa.Integer()),
        sa.column('amount', sa.Numeric()),
        sa.column('is_enabled', sa.Boolean()),
        sa.column('sort_order', sa.Integer()),
        sa.column('badge_label', sa.String()),
    )
    op.bulk_insert(options_table, _DEFAULT_OPTIONS)


def downgrade() -> None:
    op.drop_index('ix_traffic_topup_options_enabled_sort', table_name=TABLE_NAME)
    op.drop_table(TABLE_NAME)
