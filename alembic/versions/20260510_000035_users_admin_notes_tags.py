"""users.admin_notes + users.tags + audit_action user-CRM (FEA-ADMIN-USER-CRM #1)

Revision ID: 20260510_000035
Revises: 20260510_000034
Create Date: 2026-05-10 00:00:07.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '20260510_000035'
down_revision = '20260510_000034'
branch_labels = None
depends_on = None


_NEW_AUDIT_VALUES = (
    'user_notes_updated',
    'user_tag_added',
    'user_tag_removed',
    'user_blocked',
    'user_unblocked',
    'user_trial_reset',
    'user_admin_dm_sent',
    'user_force_subscription_disabled',
)


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == 'postgresql'

    op.add_column(
        'users',
        sa.Column('admin_notes', sa.Text(), nullable=True),
    )
    op.add_column(
        'users',
        sa.Column(
            'tags',
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
    )

    if is_pg:
        for value in _NEW_AUDIT_VALUES:
            op.execute(
                sa.text(
                    f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{value}'"
                ).execution_options(autocommit=True)
            )


def downgrade() -> None:
    op.drop_column('users', 'tags')
    op.drop_column('users', 'admin_notes')
    # PG не поддерживает удаление enum value — namespace value останется.
