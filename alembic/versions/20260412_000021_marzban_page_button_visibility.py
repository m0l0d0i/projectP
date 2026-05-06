"""add marzban subscription page button visibility flags

Revision ID: 20260412_000021
Revises: 20260411_000020
Create Date: 2026-04-12 18:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '20260412_000021'
down_revision = '20260411_000020'
branch_labels = None
depends_on = None


TABLE_NAME = 'marzban_page_settings'


def upgrade() -> None:
    op.add_column(
        TABLE_NAME,
        sa.Column(
            'show_primary_connect_button',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('true'),
        ),
    )
    op.add_column(
        TABLE_NAME,
        sa.Column(
            'show_one_click_block',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('true'),
        ),
    )
    op.add_column(
        TABLE_NAME,
        sa.Column(
            'show_hiddify_button',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('true'),
        ),
    )
    op.add_column(
        TABLE_NAME,
        sa.Column(
            'show_v2raytun_button',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('true'),
        ),
    )
    op.add_column(
        TABLE_NAME,
        sa.Column(
            'show_happ_button',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('true'),
        ),
    )
    op.add_column(
        TABLE_NAME,
        sa.Column(
            'show_qr_button',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('true'),
        ),
    )


def downgrade() -> None:
    op.drop_column(TABLE_NAME, 'show_qr_button')
    op.drop_column(TABLE_NAME, 'show_happ_button')
    op.drop_column(TABLE_NAME, 'show_v2raytun_button')
    op.drop_column(TABLE_NAME, 'show_hiddify_button')
    op.drop_column(TABLE_NAME, 'show_one_click_block')
    op.drop_column(TABLE_NAME, 'show_primary_connect_button')