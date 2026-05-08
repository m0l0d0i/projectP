"""add ix_audit_logs_created_at_id for fast list_recent

Revision ID: 20260507_000023
Revises: 20260507_000022
Create Date: 2026-05-07 00:30:00.000000
"""

from __future__ import annotations

from alembic import op


revision = '20260507_000023'
down_revision = '20260507_000022'
branch_labels = None
depends_on = None


INDEX_NAME = 'ix_audit_logs_created_at_id'
TABLE_NAME = 'audit_logs'


def upgrade() -> None:
    op.execute(
        f'CREATE INDEX IF NOT EXISTS {INDEX_NAME} '
        f'ON {TABLE_NAME} (created_at, id)'
    )


def downgrade() -> None:
    op.execute(f'DROP INDEX IF EXISTS {INDEX_NAME}')
