"""admin_dm_messages для DM-композера и timeline (FEA-ADMIN-USER-CRM #3)

Revision ID: 20260510_000036
Revises: 20260510_000035
Create Date: 2026-05-10 00:00:08.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '20260510_000036'
down_revision = '20260510_000035'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'admin_dm_messages',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('admin_id', sa.Integer(), nullable=True),
        sa.Column('admin_username', sa.String(length=64), nullable=True),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column(
            'status',
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'queued'"),
        ),
        sa.Column('outbox_message_id', sa.Integer(), nullable=True),
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
            "char_length(trim(text)) > 0",
            name='ck_admin_dm_messages_text_not_blank',
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'sent', 'failed')",
            name='ck_admin_dm_messages_status_valid',
        ),
        sa.ForeignKeyConstraint(
            ['user_id'],
            ['users.id'],
            ondelete='CASCADE',
            name='fk_admin_dm_messages_user_id',
        ),
        sa.ForeignKeyConstraint(
            ['admin_id'],
            ['web_admin_users.id'],
            ondelete='SET NULL',
            name='fk_admin_dm_messages_admin_id',
        ),
        sa.ForeignKeyConstraint(
            ['outbox_message_id'],
            ['outbox_messages.id'],
            ondelete='SET NULL',
            name='fk_admin_dm_messages_outbox_message_id',
        ),
    )
    op.create_index(
        'ix_admin_dm_messages_user_created',
        'admin_dm_messages',
        ['user_id', sa.text('created_at DESC')],
    )


def downgrade() -> None:
    op.drop_index('ix_admin_dm_messages_user_created', table_name='admin_dm_messages')
    op.drop_table('admin_dm_messages')
