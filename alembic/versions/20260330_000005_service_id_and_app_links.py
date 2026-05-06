"""add service_id to subscriptions and app_links table

Revision ID: 20260330_000005
Revises: 20260329_000004
Create Date: 2026-03-30 00:00:05
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '20260330_000005'
down_revision = '20260329_000004'
branch_labels = None
depends_on = None

DEFAULT_APP_LINK_OS_NAMES = ('iOS', 'Android', 'Windows', 'macOS')


def upgrade() -> None:
    op.add_column('subscriptions', sa.Column('service_id', sa.String(length=8), nullable=True))
    op.execute(
        """
        UPDATE subscriptions
        SET service_id = upper(substr(md5(id::text || random()::text || clock_timestamp()::text), 1, 8))
        WHERE service_id IS NULL
        """
    )
    op.alter_column('subscriptions', 'service_id', nullable=False)
    op.create_index('ix_subscriptions_service_id', 'subscriptions', ['service_id'], unique=True)

    op.create_table(
        'app_links',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('os_name', sa.String(length=32), nullable=False),
        sa.Column('download_url', sa.String(length=512), nullable=True),
        sa.Column('guide_url', sa.String(length=512), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_app_links_os_name', 'app_links', ['os_name'], unique=True)

    for os_name in DEFAULT_APP_LINK_OS_NAMES:
        op.execute(
            sa.text(
                """
                INSERT INTO app_links (os_name, download_url, guide_url, created_at, updated_at)
                VALUES (:os_name, NULL, NULL, now(), now())
                ON CONFLICT (os_name) DO NOTHING
                """
            ).bindparams(os_name=os_name)
        )


def downgrade() -> None:
    op.drop_index('ix_app_links_os_name', table_name='app_links')
    op.drop_table('app_links')

    op.drop_index('ix_subscriptions_service_id', table_name='subscriptions')
    op.drop_column('subscriptions', 'service_id')