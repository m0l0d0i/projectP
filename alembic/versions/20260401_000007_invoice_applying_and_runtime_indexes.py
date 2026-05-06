"""add invoice applying status and runtime indexes

Revision ID: 20260401_000007
Revises: 20260330_000006
Create Date: 2026-04-01 18:55:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '20260401_000007'
down_revision = '20260330_000006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE invoice_status ADD VALUE IF NOT EXISTS 'applying'")

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
        "CREATE INDEX IF NOT EXISTS ix_subscriptions_is_active_id ON subscriptions (is_active, id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_users_bot_blocked_is_blocked_id ON users (bot_blocked, is_blocked, id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_support_messages_ticket_created_id ON support_messages (ticket_id, created_at, id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_support_messages_ticket_created_id")
    op.execute("DROP INDEX IF EXISTS ix_users_bot_blocked_is_blocked_id")
    op.execute("DROP INDEX IF EXISTS ix_subscriptions_is_active_id")

    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS bot_blocked_reason")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS bot_blocked_at")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS bot_blocked")

    # enum value applying safely not removed on downgrade