"""add broadcast delivery audit and bot blocked flags

Revision ID: 20260329_000004
Revises: 20260329_000003
Create Date: 2026-03-29 00:00:04
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '20260329_000004'
down_revision = '20260329_000003'
branch_labels = None
depends_on = None

broadcast_delivery_status = sa.Enum(
    'sent',
    'failed',
    'skipped_blocked',
    'bot_blocked',
    name='broadcast_delivery_status',
    create_type=False,
)


def _quote_enum_values(*values: str) -> str:
    return ', '.join("'" + value.replace("'", "''") + "'" for value in values)


def _create_enum_type(name: str, *values: str) -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            CREATE TYPE {name} AS ENUM ({_quote_enum_values(*values)});
        EXCEPTION
            WHEN duplicate_object THEN null;
        END
        $$;
        """
    )


def _drop_enum_type(name: str) -> None:
    op.execute(f'DROP TYPE IF EXISTS {name};')


def upgrade() -> None:

    op.execute(
        """
        ALTER TABLE users
        ADD COLUMN IF NOT EXISTS bot_blocked BOOLEAN NOT NULL DEFAULT false
        """
    )
    op.execute(
        """
        ALTER TABLE users
        ADD COLUMN IF NOT EXISTS bot_blocked_at TIMESTAMPTZ NULL
        """
    )
    op.execute(
        """
        ALTER TABLE users
        ADD COLUMN IF NOT EXISTS bot_blocked_reason VARCHAR(255) NULL
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_users_bot_blocked ON users (bot_blocked)"
    )

    op.create_table(
        'broadcast_job_deliveries',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('job_id', sa.Integer(), sa.ForeignKey('broadcast_jobs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('user_tg_id', sa.BigInteger(), nullable=False),
        sa.Column('status', broadcast_delivery_status, nullable=False, server_default='failed'),
        sa.Column('attempt_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('telegram_message_id', sa.BigInteger(), nullable=True),
        sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('job_id', 'user_id', name='uq_broadcast_job_delivery_job_user'),
    )
    op.create_index('ix_broadcast_job_deliveries_job_id', 'broadcast_job_deliveries', ['job_id'])
    op.create_index('ix_broadcast_job_deliveries_user_id', 'broadcast_job_deliveries', ['user_id'])
    op.create_index('ix_broadcast_job_deliveries_user_tg_id', 'broadcast_job_deliveries', ['user_tg_id'])
    op.create_index('ix_broadcast_job_deliveries_status', 'broadcast_job_deliveries', ['status'])


def downgrade() -> None:
    op.drop_index('ix_broadcast_job_deliveries_status', table_name='broadcast_job_deliveries')
    op.drop_index('ix_broadcast_job_deliveries_user_tg_id', table_name='broadcast_job_deliveries')
    op.drop_index('ix_broadcast_job_deliveries_user_id', table_name='broadcast_job_deliveries')
    op.drop_index('ix_broadcast_job_deliveries_job_id', table_name='broadcast_job_deliveries')
    op.drop_table('broadcast_job_deliveries')

    op.execute('DROP INDEX IF EXISTS ix_users_bot_blocked')
    op.execute('ALTER TABLE users DROP COLUMN IF EXISTS bot_blocked_reason')
    op.execute('ALTER TABLE users DROP COLUMN IF EXISTS bot_blocked_at')
    op.execute('ALTER TABLE users DROP COLUMN IF EXISTS bot_blocked')

    _drop_enum_type('broadcast_delivery_status')