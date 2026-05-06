"""add run_at to broadcast jobs

Revision ID: 20260330_000006
Revises: 20260330_000005
Create Date: 2026-03-30 00:00:06
"""
from __future__ import annotations

from alembic import op

revision = '20260330_000006'
down_revision = '20260330_000005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE broadcast_jobs
        ADD COLUMN IF NOT EXISTS run_at TIMESTAMPTZ NULL
        """
    )
    op.execute(
        """
        UPDATE broadcast_jobs
        SET run_at = COALESCE(created_at, NOW())
        WHERE run_at IS NULL
        """
    )
    op.execute(
        """
        ALTER TABLE broadcast_jobs
        ALTER COLUMN run_at SET NOT NULL
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_broadcast_jobs_run_at ON broadcast_jobs (run_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_broadcast_jobs_run_at")
    op.execute("ALTER TABLE broadcast_jobs DROP COLUMN IF EXISTS run_at")