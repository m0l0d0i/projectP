"""TariffPlan visibility/окна/сегменты + User.unlocked_tariff_ids +
PromoCode.unlocks_tariff_id + audit (FEA-ADMIN-TARIFF-PLUS #1)

Revision ID: 20260510_000039
Revises: 20260510_000038
Create Date: 2026-05-10 00:00:11.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '20260510_000039'
down_revision = '20260510_000038'
branch_labels = None
depends_on = None


_VISIBILITY_ENUM_NAME = 'tariff_visibility'
_VISIBILITY_VALUES = ('public', 'code_only', 'segment_only', 'private_link')

_NEW_AUDIT_VALUES = (
    'tariff_visibility_updated',
    'tariff_unlock_granted',
)


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == 'postgresql'

    if is_pg:
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                f"IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = '{_VISIBILITY_ENUM_NAME}') THEN "
                f"CREATE TYPE {_VISIBILITY_ENUM_NAME} AS ENUM "
                f"({', '.join(repr(v) for v in _VISIBILITY_VALUES)}); "
                "END IF; END $$;"
            )
        )
        for value in _NEW_AUDIT_VALUES:
            op.execute(
                sa.text(
                    f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{value}'"
                ).execution_options(autocommit=True)
            )

    visibility_type = sa.Enum(
        *_VISIBILITY_VALUES, name=_VISIBILITY_ENUM_NAME, create_type=False
    )

    # tariff_plans
    op.add_column(
        'tariff_plans',
        sa.Column(
            'visibility',
            visibility_type,
            nullable=False,
            server_default=sa.text("'public'"),
        ),
    )
    op.add_column('tariff_plans', sa.Column('available_from', sa.DateTime(timezone=True), nullable=True))
    op.add_column('tariff_plans', sa.Column('available_to', sa.DateTime(timezone=True), nullable=True))
    op.add_column('tariff_plans', sa.Column('segment_filter_json', sa.JSON(), nullable=True))
    op.add_column('tariff_plans', sa.Column('private_token', sa.String(length=48), nullable=True))
    op.add_column('tariff_plans', sa.Column('accent_color', sa.String(length=16), nullable=True))
    op.add_column(
        'tariff_plans',
        sa.Column(
            'is_recommended',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('false'),
        ),
    )
    op.add_column('tariff_plans', sa.Column('max_active_subscriptions', sa.Integer(), nullable=True))
    op.create_unique_constraint(
        'uq_tariff_plans_private_token',
        'tariff_plans',
        ['private_token'],
    )
    op.create_check_constraint(
        'ck_tariff_plans_max_active_subs_positive',
        'tariff_plans',
        'max_active_subscriptions IS NULL OR max_active_subscriptions >= 1',
    )
    op.create_check_constraint(
        'ck_tariff_plans_available_window_valid',
        'tariff_plans',
        'available_from IS NULL OR available_to IS NULL OR available_from <= available_to',
    )
    # users.unlocked_tariff_ids
    op.add_column(
        'users',
        sa.Column(
            'unlocked_tariff_ids',
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
    )
    # promo_codes.unlocks_tariff_id
    op.add_column('promo_codes', sa.Column('unlocks_tariff_id', sa.Integer(), nullable=True))
    op.create_foreign_key(
        'fk_promo_codes_unlocks_tariff_id',
        'promo_codes',
        'tariff_plans',
        ['unlocks_tariff_id'],
        ['id'],
        ondelete='SET NULL',
    )


def downgrade() -> None:
    op.drop_constraint('fk_promo_codes_unlocks_tariff_id', 'promo_codes', type_='foreignkey')
    op.drop_column('promo_codes', 'unlocks_tariff_id')

    op.drop_column('users', 'unlocked_tariff_ids')

    op.drop_constraint('ck_tariff_plans_available_window_valid', 'tariff_plans', type_='check')
    op.drop_constraint('ck_tariff_plans_max_active_subs_positive', 'tariff_plans', type_='check')
    op.drop_constraint('uq_tariff_plans_private_token', 'tariff_plans', type_='unique')
    op.drop_column('tariff_plans', 'max_active_subscriptions')
    op.drop_column('tariff_plans', 'is_recommended')
    op.drop_column('tariff_plans', 'accent_color')
    op.drop_column('tariff_plans', 'private_token')
    op.drop_column('tariff_plans', 'segment_filter_json')
    op.drop_column('tariff_plans', 'available_to')
    op.drop_column('tariff_plans', 'available_from')
    op.drop_column('tariff_plans', 'visibility')

    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute(sa.text(f'DROP TYPE IF EXISTS {_VISIBILITY_ENUM_NAME}'))
    # PG не удаляет enum value — namespace останется.
