"""users.anonymized_at + audit_action user_data_exported/user_erased
(CMP-1 GDPR export+erase)

Revision ID: 20260518_000042
Revises: 20260510_000041
Create Date: 2026-05-18 00:00:42.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '20260518_000042'
down_revision = '20260510_000041'
branch_labels = None
depends_on = None


_NEW_AUDIT_VALUES = (
    'user_data_exported',
    'user_erased',
)


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('anonymized_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        'ix_users_anonymized_at',
        'users',
        ['anonymized_at'],
    )

    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        for value in _NEW_AUDIT_VALUES:
            op.execute(
                sa.text(
                    f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{value}'"
                ).execution_options(autocommit=True)
            )


def downgrade() -> None:
    op.drop_index('ix_users_anonymized_at', table_name='users')
    op.drop_column('users', 'anonymized_at')
