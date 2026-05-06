"""broadcast jobs rich payload and lifecycle rework

Revision ID: 20260410_000018
Revises: 20260410_000017
Create Date: 2026-04-10 00:00:18
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '20260410_000018'
down_revision = '20260410_000017'
branch_labels = None
depends_on = None


_OLD_ENUM_NAME = 'broadcast_job_status'
_NEW_ENUM_NAME = 'broadcast_job_status_v2'
_OLD_ENUM_VALUES = (
    'pending',
    'running',
    'completed',
    'failed',
    'cancelled',
)
_NEW_ENUM_VALUES = (
    'draft',
    'scheduled',
    'running',
    'completed',
    'failed',
    'cancelled',
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


def _replace_broadcast_job_status_enum(*, upgrade: bool) -> None:
    source_name = _OLD_ENUM_NAME if upgrade else _NEW_ENUM_NAME
    target_name = _NEW_ENUM_NAME if upgrade else _OLD_ENUM_NAME
    target_values = _NEW_ENUM_VALUES if upgrade else _OLD_ENUM_VALUES

    _create_enum_type(target_name, *target_values)

    if upgrade:
        mapping_sql = """
            CASE status::text
                WHEN 'pending' THEN 'scheduled'
                ELSE status::text
            END
        """
        target_default = "'scheduled'"
    else:
        mapping_sql = """
            CASE status::text
                WHEN 'draft' THEN 'pending'
                WHEN 'scheduled' THEN 'pending'
                ELSE status::text
            END
        """
        target_default = "'pending'"

    op.execute(
        f"""
        ALTER TABLE broadcast_jobs
        ALTER COLUMN status DROP DEFAULT,
        ALTER COLUMN status TYPE {target_name}
        USING ({mapping_sql})::{target_name}
        """
    )
    op.execute(
        f"""
        ALTER TABLE broadcast_jobs
        ALTER COLUMN status SET DEFAULT {target_default}::{target_name}
        """
    )

    _drop_enum_type(source_name)
    op.execute(f'ALTER TYPE {target_name} RENAME TO {_OLD_ENUM_NAME};')



def upgrade() -> None:
    op.add_column(
        'broadcast_jobs',
        sa.Column(
            'payload_json',
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
    )
    op.add_column(
        'broadcast_jobs',
        sa.Column('photo_file_id', sa.Text(), nullable=True),
    )
    op.add_column(
        'broadcast_jobs',
        sa.Column('photo_file_unique_id', sa.Text(), nullable=True),
    )
    op.add_column(
        'broadcast_jobs',
        sa.Column('media_type', sa.String(length=32), nullable=True),
    )
    op.add_column(
        'broadcast_jobs',
        sa.Column(
            'keyboard_json',
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
    )
    op.add_column(
        'broadcast_jobs',
        sa.Column('cancel_requested_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'broadcast_jobs',
        sa.Column('cancelled_by_tg_id', sa.BigInteger(), nullable=True),
    )

    op.alter_column('broadcast_jobs', 'text', existing_type=sa.Text(), nullable=True)

    op.execute(
        """
        UPDATE broadcast_jobs
        SET payload_json = '{}'::json,
            keyboard_json = '[]'::json
        WHERE payload_json IS NULL OR keyboard_json IS NULL
        """
    )

    _replace_broadcast_job_status_enum(upgrade=True)

    op.create_check_constraint(
        'ck_broadcast_jobs_has_content',
        'broadcast_jobs',
        "((text IS NOT NULL AND char_length(trim(text)) > 0) OR (photo_file_id IS NOT NULL AND char_length(trim(photo_file_id)) > 0))",
    )

    op.create_index(
        'ix_broadcast_jobs_cancel_requested_at',
        'broadcast_jobs',
        ['cancel_requested_at'],
        unique=False,
    )
    op.create_index(
        'ix_broadcast_jobs_cancelled_by_tg_id',
        'broadcast_jobs',
        ['cancelled_by_tg_id'],
        unique=False,
    )
    op.create_index(
        'ix_broadcast_jobs_status_run_at_id',
        'broadcast_jobs',
        ['status', 'run_at', 'id'],
        unique=False,
    )
    op.create_index(
        'ix_broadcast_jobs_created_by_status_id',
        'broadcast_jobs',
        ['created_by_tg_id', 'status', 'id'],
        unique=False,
    )
    op.create_index(
        'ix_broadcast_jobs_cancel_requested_at_id',
        'broadcast_jobs',
        ['cancel_requested_at', 'id'],
        unique=False,
    )



def downgrade() -> None:
    op.drop_index('ix_broadcast_jobs_cancel_requested_at_id', table_name='broadcast_jobs')
    op.drop_index('ix_broadcast_jobs_created_by_status_id', table_name='broadcast_jobs')
    op.drop_index('ix_broadcast_jobs_status_run_at_id', table_name='broadcast_jobs')
    op.drop_index('ix_broadcast_jobs_cancelled_by_tg_id', table_name='broadcast_jobs')
    op.drop_index('ix_broadcast_jobs_cancel_requested_at', table_name='broadcast_jobs')

    op.drop_constraint('ck_broadcast_jobs_has_content', 'broadcast_jobs', type_='check')

    _replace_broadcast_job_status_enum(upgrade=False)

    op.alter_column('broadcast_jobs', 'text', existing_type=sa.Text(), nullable=False)

    op.drop_column('broadcast_jobs', 'cancelled_by_tg_id')
    op.drop_column('broadcast_jobs', 'cancel_requested_at')
    op.drop_column('broadcast_jobs', 'keyboard_json')
    op.drop_column('broadcast_jobs', 'media_type')
    op.drop_column('broadcast_jobs', 'photo_file_unique_id')
    op.drop_column('broadcast_jobs', 'photo_file_id')
    op.drop_column('broadcast_jobs', 'payload_json')
