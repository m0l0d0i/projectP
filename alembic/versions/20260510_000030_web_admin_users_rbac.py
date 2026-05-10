"""web_admin_users (RBAC) + actor_username on audit_logs (FEA-C39 #1)

Revision ID: 20260510_000030
Revises: 20260510_000029
Create Date: 2026-05-10 00:00:02.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '20260510_000030'
down_revision = '20260510_000029'
branch_labels = None
depends_on = None


_ROLE_ENUM_NAME = 'web_admin_role'
_ROLE_VALUES = ('superadmin', 'finance', 'support', 'readonly')


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == 'postgresql'

    if is_pg:
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                f"IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = '{_ROLE_ENUM_NAME}') THEN "
                f"CREATE TYPE {_ROLE_ENUM_NAME} AS ENUM "
                f"({', '.join(repr(v) for v in _ROLE_VALUES)}); "
                "END IF; END $$;"
            )
        )

    role_type = sa.Enum(*_ROLE_VALUES, name=_ROLE_ENUM_NAME, create_type=False)

    op.create_table(
        'web_admin_users',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('username', sa.String(length=64), nullable=False),
        sa.Column('password_hash', sa.Text(), nullable=False),
        sa.Column('role', role_type, nullable=False),
        sa.Column(
            'is_active',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('true'),
        ),
        sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
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
            "char_length(trim(username)) > 0",
            name='ck_web_admin_users_username_not_blank',
        ),
        sa.CheckConstraint(
            "char_length(password_hash) > 0",
            name='ck_web_admin_users_password_hash_not_blank',
        ),
    )
    # Кейс-инсенситивный уникальный индекс на username — чтобы Admin/admin/ADMIN
    # не могли существовать одновременно.
    if is_pg:
        op.execute(
            sa.text(
                'CREATE UNIQUE INDEX uq_web_admin_users_username_lower '
                'ON web_admin_users (lower(username))'
            )
        )
    else:
        op.create_index(
            'uq_web_admin_users_username_lower',
            'web_admin_users',
            ['username'],
            unique=True,
        )

    # actor_username добавляем в audit_logs — у web-админов может не быть
    # tg_id, и actor_type=admin без attribution был бы дырявым логом.
    op.add_column(
        'audit_logs',
        sa.Column('actor_username', sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('audit_logs', 'actor_username')

    bind = op.get_bind()
    is_pg = bind.dialect.name == 'postgresql'
    if is_pg:
        op.execute(sa.text('DROP INDEX IF EXISTS uq_web_admin_users_username_lower'))
    else:
        op.drop_index('uq_web_admin_users_username_lower', table_name='web_admin_users')
    op.drop_table('web_admin_users')

    if is_pg:
        op.execute(sa.text(f'DROP TYPE IF EXISTS {_ROLE_ENUM_NAME}'))
