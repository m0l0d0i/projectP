"""add outbox_messages for transactional Telegram delivery (OPS-4)

Revision ID: 20260507_000024
Revises: 20260507_000023
Create Date: 2026-05-07 01:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '20260507_000024'
down_revision = '20260507_000023'
branch_labels = None
depends_on = None


TABLE_NAME = 'outbox_messages'
STATUS_ENUM_NAME = 'outbox_status'
KIND_ENUM_NAME = 'outbox_kind'


def upgrade() -> None:
    status_enum = sa.Enum(
        'pending', 'processing', 'sent', 'failed', 'dead',
        name=STATUS_ENUM_NAME,
    )
    kind_enum = sa.Enum(
        'tg_message',
        name=KIND_ENUM_NAME,
    )
    bind = op.get_bind()
    status_enum.create(bind, checkfirst=True)
    kind_enum.create(bind, checkfirst=True)

    op.create_table(
        TABLE_NAME,
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column(
            'kind',
            sa.Enum('tg_message', name=KIND_ENUM_NAME, create_type=False),
            nullable=False,
        ),
        sa.Column('target_chat_id', sa.BigInteger(), nullable=True, index=True),
        sa.Column(
            'payload_json',
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column(
            'status',
            sa.Enum(
                'pending', 'processing', 'sent', 'failed', 'dead',
                name=STATUS_ENUM_NAME, create_type=False,
            ),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('max_attempts', sa.Integer(), nullable=False, server_default=sa.text('10')),
        sa.Column(
            'next_attempt_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('CURRENT_TIMESTAMP'),
        ),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('correlation_key', sa.String(length=128), nullable=True),
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
        sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint('attempts >= 0', name='ck_outbox_attempts_non_negative'),
        sa.CheckConstraint('max_attempts > 0', name='ck_outbox_max_attempts_positive'),
    )

    # Hot-path index: worker SELECT WHERE status=pending AND next_attempt_at <= now ORDER BY next_attempt_at, id
    op.create_index(
        'ix_outbox_messages_due',
        TABLE_NAME,
        ['status', 'next_attempt_at', 'id'],
    )

    # Idempotency / dedup partial unique
    op.create_index(
        'uq_outbox_messages_correlation_key',
        TABLE_NAME,
        ['correlation_key'],
        unique=True,
        postgresql_where=sa.text('correlation_key IS NOT NULL'),
    )


def downgrade() -> None:
    op.drop_index('uq_outbox_messages_correlation_key', table_name=TABLE_NAME)
    op.drop_index('ix_outbox_messages_due', table_name=TABLE_NAME)
    op.drop_table(TABLE_NAME)
    sa.Enum(name=KIND_ENUM_NAME).drop(op.get_bind(), checkfirst=True)
    sa.Enum(name=STATUS_ENUM_NAME).drop(op.get_bind(), checkfirst=True)
