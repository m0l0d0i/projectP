"""add broadcast jobs table

Revision ID: 20260329_000003
Revises: 20260329_000002
Create Date: 2026-03-29 00:00:03
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '20260329_000003'
down_revision = '20260329_000002'
branch_labels = None
depends_on = None

broadcast_job_status = sa.Enum(
    'pending',
    'running',
    'completed',
    'failed',
    'cancelled',
    name='broadcast_job_status',
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

    op.create_table(
        'broadcast_jobs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('created_by_tg_id', sa.BigInteger(), nullable=False),
        sa.Column('status', broadcast_job_status, nullable=False, server_default='pending'),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('total_users', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('processed_users', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('sent_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('failed_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_user_id', sa.Integer(), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_broadcast_jobs_created_by_tg_id', 'broadcast_jobs', ['created_by_tg_id'])
    op.create_index('ix_broadcast_jobs_status', 'broadcast_jobs', ['status'])


def downgrade() -> None:
    op.drop_index('ix_broadcast_jobs_status', table_name='broadcast_jobs')
    op.drop_index('ix_broadcast_jobs_created_by_tg_id', table_name='broadcast_jobs')
    op.drop_table('broadcast_jobs')

    _drop_enum_type('broadcast_job_status')