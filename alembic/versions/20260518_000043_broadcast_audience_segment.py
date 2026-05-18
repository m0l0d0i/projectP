"""broadcast_jobs.audience_segment (FEA-C33)

Revision ID: 20260518_000043
Revises: 20260518_000042
Create Date: 2026-05-18 00:00:43.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '20260518_000043'
down_revision = '20260518_000042'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'broadcast_jobs',
        sa.Column('audience_segment', sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('broadcast_jobs', 'audience_segment')
