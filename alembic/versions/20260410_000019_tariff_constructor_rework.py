"""tariff constructor rework and invoice snapshots

Revision ID: 20260410_000019
Revises: 20260410_000018
Create Date: 2026-04-10 00:00:19
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '20260410_000019'
down_revision = '20260410_000018'
branch_labels = None
depends_on = None


TARIFF_PRICING_MODE = postgresql.ENUM(
    'fixed',
    'constructor',
    name='tariff_pricing_mode',
)
TARIFF_TRAFFIC_MODE = postgresql.ENUM(
    'fixed',
    'constructor',
    'unlimited',
    name='tariff_traffic_mode',
)
TARIFF_DEVICE_MODE = postgresql.ENUM(
    'fixed',
    'constructor',
    'unlimited',
    name='tariff_device_mode',
)


def _pricing_scalar_sql(column_name: str, fallback_sql: str) -> str:
    return (
        f"COALESCE((SELECT {column_name} FROM pricing_rules WHERE id = 1), {fallback_sql})"
    )


def upgrade() -> None:
    bind = op.get_bind()
    TARIFF_PRICING_MODE.create(bind, checkfirst=True)
    TARIFF_TRAFFIC_MODE.create(bind, checkfirst=True)
    TARIFF_DEVICE_MODE.create(bind, checkfirst=True)

    op.add_column('subscriptions', sa.Column('current_tariff_id', sa.Integer(), nullable=True))
    op.create_foreign_key(
        'fk_subscriptions_current_tariff_id_tariff_plans',
        'subscriptions',
        'tariff_plans',
        ['current_tariff_id'],
        ['id'],
        ondelete='SET NULL',
    )
    op.create_index('ix_subscriptions_current_tariff_id', 'subscriptions', ['current_tariff_id'], unique=False)

    op.add_column('invoices', sa.Column('tariff_plan_id', sa.Integer(), nullable=True))
    op.add_column(
        'invoices',
        sa.Column(
            'tariff_snapshot_json',
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
    )
    op.create_foreign_key(
        'fk_invoices_tariff_plan_id_tariff_plans',
        'invoices',
        'tariff_plans',
        ['tariff_plan_id'],
        ['id'],
        ondelete='SET NULL',
    )
    op.create_index('ix_invoices_tariff_plan_id', 'invoices', ['tariff_plan_id'], unique=False)

    op.add_column('tariff_plans', sa.Column('description', sa.Text(), nullable=True))
    op.add_column('tariff_plans', sa.Column('badge_text', sa.String(length=64), nullable=True))
    op.add_column(
        'tariff_plans',
        sa.Column('is_highlighted', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    )
    op.add_column(
        'tariff_plans',
        sa.Column('is_public', sa.Boolean(), nullable=False, server_default=sa.text('true')),
    )
    op.add_column(
        'tariff_plans',
        sa.Column('is_archived', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    )
    op.add_column('tariff_plans', sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        'tariff_plans',
        sa.Column(
            'pricing_mode',
            TARIFF_PRICING_MODE,
            nullable=False,
            server_default=sa.text("'fixed'::tariff_pricing_mode"),
        ),
    )
    op.add_column(
        'tariff_plans',
        sa.Column(
            'traffic_mode',
            TARIFF_TRAFFIC_MODE,
            nullable=False,
            server_default=sa.text("'fixed'::tariff_traffic_mode"),
        ),
    )
    op.add_column(
        'tariff_plans',
        sa.Column(
            'device_mode',
            TARIFF_DEVICE_MODE,
            nullable=False,
            server_default=sa.text("'fixed'::tariff_device_mode"),
        ),
    )
    op.add_column(
        'tariff_plans',
        sa.Column(
            'base_monthly_price',
            sa.Numeric(12, 2),
            nullable=False,
            server_default=sa.text('0.00'),
        ),
    )
    op.add_column('tariff_plans', sa.Column('base_traffic_gb', sa.Integer(), nullable=True))
    op.add_column('tariff_plans', sa.Column('fixed_traffic_gb', sa.Integer(), nullable=True))
    op.add_column('tariff_plans', sa.Column('min_traffic_gb', sa.Integer(), nullable=True))
    op.add_column('tariff_plans', sa.Column('max_traffic_gb', sa.Integer(), nullable=True))
    op.add_column('tariff_plans', sa.Column('traffic_step_gb', sa.Integer(), nullable=True))
    op.add_column(
        'tariff_plans',
        sa.Column(
            'traffic_step_price',
            sa.Numeric(12, 2),
            nullable=False,
            server_default=sa.text('0.00'),
        ),
    )
    op.add_column('tariff_plans', sa.Column('base_device_count', sa.Integer(), nullable=True))
    op.add_column('tariff_plans', sa.Column('fixed_device_count', sa.Integer(), nullable=True))
    op.add_column('tariff_plans', sa.Column('min_device_count', sa.Integer(), nullable=True))
    op.add_column('tariff_plans', sa.Column('max_device_count', sa.Integer(), nullable=True))
    op.add_column('tariff_plans', sa.Column('device_step', sa.Integer(), nullable=True))
    op.add_column(
        'tariff_plans',
        sa.Column(
            'device_step_price',
            sa.Numeric(12, 2),
            nullable=False,
            server_default=sa.text('0.00'),
        ),
    )
    op.add_column(
        'tariff_plans',
        sa.Column('allow_unlimited_devices', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    )
    op.add_column(
        'tariff_plans',
        sa.Column(
            'unlimited_devices_surcharge',
            sa.Numeric(12, 2),
            nullable=False,
            server_default=sa.text('0.00'),
        ),
    )

    op.create_table(
        'tariff_period_options',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('tariff_plan_id', sa.Integer(), sa.ForeignKey('tariff_plans.id', ondelete='CASCADE'), nullable=False),
        sa.Column('months', sa.Integer(), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default=sa.text('100')),
        sa.Column('is_enabled', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('tariff_plan_id', 'months', name='uq_tariff_period_options_plan_months'),
        sa.CheckConstraint('months >= 1', name='ck_tariff_period_options_months_positive'),
        sa.CheckConstraint('sort_order >= 0', name='ck_tariff_period_options_sort_order_non_negative'),
    )
    op.create_index('ix_tariff_period_options_tariff_plan_id', 'tariff_period_options', ['tariff_plan_id'], unique=False)
    op.create_index('ix_tariff_period_options_is_enabled', 'tariff_period_options', ['is_enabled'], unique=False)
    op.create_index(
        'ix_tariff_period_options_plan_sort',
        'tariff_period_options',
        ['tariff_plan_id', 'sort_order', 'months', 'id'],
        unique=False,
    )
    op.create_index(
        'ix_tariff_period_options_enabled_sort',
        'tariff_period_options',
        ['is_enabled', 'sort_order', 'months', 'id'],
        unique=False,
    )

    op.create_index('ix_tariff_plans_is_public', 'tariff_plans', ['is_public'], unique=False)
    op.create_index('ix_tariff_plans_is_archived', 'tariff_plans', ['is_archived'], unique=False)
    op.create_index('ix_tariff_plans_archived_at', 'tariff_plans', ['archived_at'], unique=False)
    op.create_index('ix_tariff_plans_pricing_mode', 'tariff_plans', ['pricing_mode'], unique=False)
    op.create_index('ix_tariff_plans_traffic_mode', 'tariff_plans', ['traffic_mode'], unique=False)
    op.create_index('ix_tariff_plans_device_mode', 'tariff_plans', ['device_mode'], unique=False)
    op.create_index(
        'ix_tariff_plan_public_active_sort',
        'tariff_plans',
        ['is_public', 'is_active', 'sort_order', 'id'],
        unique=False,
    )
    op.create_index(
        'ix_tariff_plan_archived_sort',
        'tariff_plans',
        ['is_archived', 'sort_order', 'id'],
        unique=False,
    )
    op.create_index(
        'ix_tariff_plan_pricing_mode',
        'tariff_plans',
        ['pricing_mode', 'id'],
        unique=False,
    )

    op.create_check_constraint('ck_tariff_plan_code_not_blank', 'tariff_plans', "char_length(trim(code)) > 0")
    op.create_check_constraint('ck_tariff_plan_title_not_blank', 'tariff_plans', "char_length(trim(title)) > 0")
    op.create_check_constraint('ck_tariff_plan_sort_order_non_negative', 'tariff_plans', 'sort_order >= 0')
    op.create_check_constraint(
        'ck_tariff_plan_base_monthly_price_non_negative',
        'tariff_plans',
        'base_monthly_price >= 0',
    )
    op.create_check_constraint(
        'ck_tariff_plan_traffic_step_price_non_negative',
        'tariff_plans',
        'traffic_step_price >= 0',
    )
    op.create_check_constraint(
        'ck_tariff_plan_device_step_price_non_negative',
        'tariff_plans',
        'device_step_price >= 0',
    )
    op.create_check_constraint(
        'ck_tariff_plan_unlimited_devices_surcharge_non_negative',
        'tariff_plans',
        'unlimited_devices_surcharge >= 0',
    )
    op.create_check_constraint(
        'ck_tariff_plan_fixed_traffic_non_negative',
        'tariff_plans',
        'fixed_traffic_gb IS NULL OR fixed_traffic_gb >= 0',
    )
    op.create_check_constraint(
        'ck_tariff_plan_min_traffic_non_negative',
        'tariff_plans',
        'min_traffic_gb IS NULL OR min_traffic_gb >= 0',
    )
    op.create_check_constraint(
        'ck_tariff_plan_max_traffic_non_negative',
        'tariff_plans',
        'max_traffic_gb IS NULL OR max_traffic_gb >= 0',
    )
    op.create_check_constraint(
        'ck_tariff_plan_traffic_step_positive',
        'tariff_plans',
        'traffic_step_gb IS NULL OR traffic_step_gb >= 1',
    )
    op.create_check_constraint(
        'ck_tariff_plan_base_traffic_non_negative',
        'tariff_plans',
        'base_traffic_gb IS NULL OR base_traffic_gb >= 0',
    )
    op.create_check_constraint(
        'ck_tariff_plan_fixed_device_positive',
        'tariff_plans',
        'fixed_device_count IS NULL OR fixed_device_count >= 1',
    )
    op.create_check_constraint(
        'ck_tariff_plan_min_device_positive',
        'tariff_plans',
        'min_device_count IS NULL OR min_device_count >= 1',
    )
    op.create_check_constraint(
        'ck_tariff_plan_max_device_positive',
        'tariff_plans',
        'max_device_count IS NULL OR max_device_count >= 1',
    )
    op.create_check_constraint(
        'ck_tariff_plan_device_step_positive',
        'tariff_plans',
        'device_step IS NULL OR device_step >= 1',
    )
    op.create_check_constraint(
        'ck_tariff_plan_base_device_positive',
        'tariff_plans',
        'base_device_count IS NULL OR base_device_count >= 1',
    )
    op.create_check_constraint(
        'ck_tariff_plan_traffic_bounds_valid',
        'tariff_plans',
        'min_traffic_gb IS NULL OR max_traffic_gb IS NULL OR min_traffic_gb <= max_traffic_gb',
    )
    op.create_check_constraint(
        'ck_tariff_plan_device_bounds_valid',
        'tariff_plans',
        'min_device_count IS NULL OR max_device_count IS NULL OR min_device_count <= max_device_count',
    )
    op.create_check_constraint(
        'ck_tariff_plan_archived_not_active',
        'tariff_plans',
        'NOT (is_archived AND is_active)',
    )

    op.execute(
        f"""
        UPDATE tariff_plans
        SET is_public = true,
            is_archived = false,
            is_highlighted = false,
            pricing_mode = 'fixed'::tariff_pricing_mode,
            traffic_mode = CASE
                WHEN monthly_traffic_gb IS NULL THEN 'unlimited'::tariff_traffic_mode
                ELSE 'fixed'::tariff_traffic_mode
            END,
            device_mode = 'fixed'::tariff_device_mode,
            base_monthly_price = COALESCE(price_single, 0),
            base_traffic_gb = CASE
                WHEN monthly_traffic_gb IS NULL THEN NULL
                ELSE monthly_traffic_gb
            END,
            fixed_traffic_gb = monthly_traffic_gb,
            min_traffic_gb = monthly_traffic_gb,
            max_traffic_gb = monthly_traffic_gb,
            traffic_step_gb = CASE
                WHEN monthly_traffic_gb IS NULL THEN NULL
                ELSE {_pricing_scalar_sql('traffic_step_gb', '50')}
            END,
            traffic_step_price = {_pricing_scalar_sql('traffic_step_price', '0.00')},
            base_device_count = COALESCE(NULLIF(online_limit_single, 0), 1),
            fixed_device_count = COALESCE(NULLIF(online_limit_single, 0), 1),
            min_device_count = COALESCE(NULLIF(online_limit_single, 0), 1),
            max_device_count = COALESCE(NULLIF(online_limit_single, 0), 1),
            device_step = 1,
            device_step_price = {_pricing_scalar_sql('device_step_price', '0.00')},
            allow_unlimited_devices = CASE
                WHEN online_limit_unlimited IS NULL THEN true
                WHEN online_limit_unlimited > COALESCE(NULLIF(online_limit_single, 0), 1) THEN true
                ELSE false
            END,
            unlimited_devices_surcharge = GREATEST(COALESCE(price_unlimited, 0) - COALESCE(price_single, 0), 0)
        """
    )

    op.execute(
        """
        INSERT INTO tariff_period_options (
            tariff_plan_id,
            months,
            sort_order,
            is_enabled,
            created_at,
            updated_at
        )
        SELECT
            tp.id,
            gs.months,
            gs.months,
            true,
            now(),
            now()
        FROM tariff_plans tp
        CROSS JOIN LATERAL generate_series(
            1,
            GREATEST(COALESCE((SELECT max_months FROM pricing_rules WHERE id = 1), 12), 1)
        ) AS gs(months)
        """
    )

    op.execute(
        """
        UPDATE subscriptions s
        SET current_tariff_id = tp.id
        FROM tariff_plans tp
        WHERE s.current_tariff_id IS NULL
          AND s.current_tariff_code IS NOT NULL
          AND tp.code = s.current_tariff_code
        """
    )

    op.execute(
        """
        UPDATE invoices i
        SET tariff_plan_id = tp.id,
            tariff_snapshot_json = (
                COALESCE(i.payload_json::jsonb, '{}'::jsonb)
                || jsonb_build_object(
                    'tariff_plan_id', tp.id,
                    'tariff_code', tp.code,
                    'tariff_title', tp.title,
                    'pricing_mode', tp.pricing_mode::text,
                    'traffic_mode', tp.traffic_mode::text,
                    'device_mode', tp.device_mode::text,
                    'base_monthly_price', tp.base_monthly_price,
                    'base_traffic_gb', tp.base_traffic_gb,
                    'fixed_traffic_gb', tp.fixed_traffic_gb,
                    'base_device_count', tp.base_device_count,
                    'fixed_device_count', tp.fixed_device_count,
                    'allow_unlimited_devices', tp.allow_unlimited_devices,
                    'unlimited_devices_surcharge', tp.unlimited_devices_surcharge,
                    'legacy_monthly_traffic_gb', tp.monthly_traffic_gb,
                    'legacy_price_single', tp.price_single,
                    'legacy_price_unlimited', tp.price_unlimited,
                    'legacy_online_limit_single', tp.online_limit_single,
                    'legacy_online_limit_unlimited', tp.online_limit_unlimited
                )
            )::json
        FROM tariff_plans tp
        WHERE i.purpose = 'tariff'
          AND tp.code = COALESCE(i.payload_json ->> 'package_code', i.payload_json ->> 'tariff_code')
        """
    )

    op.execute(
        """
        UPDATE invoices
        SET tariff_snapshot_json = COALESCE(payload_json::jsonb, '{}'::jsonb)::json
        WHERE purpose = 'tariff'
          AND (tariff_snapshot_json IS NULL OR tariff_snapshot_json::jsonb = '{}'::jsonb)
        """
    )


def downgrade() -> None:
    op.execute("UPDATE invoices SET tariff_snapshot_json::jsonb = '{}'::jsonb WHERE tariff_snapshot_json IS NULL")

    op.drop_index('ix_tariff_plan_pricing_mode', table_name='tariff_plans')
    op.drop_index('ix_tariff_plan_archived_sort', table_name='tariff_plans')
    op.drop_index('ix_tariff_plan_public_active_sort', table_name='tariff_plans')
    op.drop_index('ix_tariff_plans_device_mode', table_name='tariff_plans')
    op.drop_index('ix_tariff_plans_traffic_mode', table_name='tariff_plans')
    op.drop_index('ix_tariff_plans_pricing_mode', table_name='tariff_plans')
    op.drop_index('ix_tariff_plans_archived_at', table_name='tariff_plans')
    op.drop_index('ix_tariff_plans_is_archived', table_name='tariff_plans')
    op.drop_index('ix_tariff_plans_is_public', table_name='tariff_plans')

    op.drop_constraint('ck_tariff_plan_archived_not_active', 'tariff_plans', type_='check')
    op.drop_constraint('ck_tariff_plan_device_bounds_valid', 'tariff_plans', type_='check')
    op.drop_constraint('ck_tariff_plan_traffic_bounds_valid', 'tariff_plans', type_='check')
    op.drop_constraint('ck_tariff_plan_base_device_positive', 'tariff_plans', type_='check')
    op.drop_constraint('ck_tariff_plan_device_step_positive', 'tariff_plans', type_='check')
    op.drop_constraint('ck_tariff_plan_max_device_positive', 'tariff_plans', type_='check')
    op.drop_constraint('ck_tariff_plan_min_device_positive', 'tariff_plans', type_='check')
    op.drop_constraint('ck_tariff_plan_fixed_device_positive', 'tariff_plans', type_='check')
    op.drop_constraint('ck_tariff_plan_base_traffic_non_negative', 'tariff_plans', type_='check')
    op.drop_constraint('ck_tariff_plan_traffic_step_positive', 'tariff_plans', type_='check')
    op.drop_constraint('ck_tariff_plan_max_traffic_non_negative', 'tariff_plans', type_='check')
    op.drop_constraint('ck_tariff_plan_min_traffic_non_negative', 'tariff_plans', type_='check')
    op.drop_constraint('ck_tariff_plan_fixed_traffic_non_negative', 'tariff_plans', type_='check')
    op.drop_constraint('ck_tariff_plan_unlimited_devices_surcharge_non_negative', 'tariff_plans', type_='check')
    op.drop_constraint('ck_tariff_plan_device_step_price_non_negative', 'tariff_plans', type_='check')
    op.drop_constraint('ck_tariff_plan_traffic_step_price_non_negative', 'tariff_plans', type_='check')
    op.drop_constraint('ck_tariff_plan_base_monthly_price_non_negative', 'tariff_plans', type_='check')
    op.drop_constraint('ck_tariff_plan_sort_order_non_negative', 'tariff_plans', type_='check')
    op.drop_constraint('ck_tariff_plan_title_not_blank', 'tariff_plans', type_='check')
    op.drop_constraint('ck_tariff_plan_code_not_blank', 'tariff_plans', type_='check')

    op.drop_index('ix_tariff_period_options_enabled_sort', table_name='tariff_period_options')
    op.drop_index('ix_tariff_period_options_plan_sort', table_name='tariff_period_options')
    op.drop_index('ix_tariff_period_options_is_enabled', table_name='tariff_period_options')
    op.drop_index('ix_tariff_period_options_tariff_plan_id', table_name='tariff_period_options')
    op.drop_table('tariff_period_options')

    op.drop_index('ix_invoices_tariff_plan_id', table_name='invoices')
    op.drop_constraint('fk_invoices_tariff_plan_id_tariff_plans', 'invoices', type_='foreignkey')
    op.drop_column('invoices', 'tariff_snapshot_json')
    op.drop_column('invoices', 'tariff_plan_id')

    op.drop_index('ix_subscriptions_current_tariff_id', table_name='subscriptions')
    op.drop_constraint('fk_subscriptions_current_tariff_id_tariff_plans', 'subscriptions', type_='foreignkey')
    op.drop_column('subscriptions', 'current_tariff_id')

    op.drop_column('tariff_plans', 'unlimited_devices_surcharge')
    op.drop_column('tariff_plans', 'allow_unlimited_devices')
    op.drop_column('tariff_plans', 'device_step_price')
    op.drop_column('tariff_plans', 'device_step')
    op.drop_column('tariff_plans', 'max_device_count')
    op.drop_column('tariff_plans', 'min_device_count')
    op.drop_column('tariff_plans', 'fixed_device_count')
    op.drop_column('tariff_plans', 'base_device_count')
    op.drop_column('tariff_plans', 'traffic_step_price')
    op.drop_column('tariff_plans', 'traffic_step_gb')
    op.drop_column('tariff_plans', 'max_traffic_gb')
    op.drop_column('tariff_plans', 'min_traffic_gb')
    op.drop_column('tariff_plans', 'fixed_traffic_gb')
    op.drop_column('tariff_plans', 'base_traffic_gb')
    op.drop_column('tariff_plans', 'base_monthly_price')
    op.drop_column('tariff_plans', 'device_mode')
    op.drop_column('tariff_plans', 'traffic_mode')
    op.drop_column('tariff_plans', 'pricing_mode')
    op.drop_column('tariff_plans', 'archived_at')
    op.drop_column('tariff_plans', 'is_archived')
    op.drop_column('tariff_plans', 'is_public')
    op.drop_column('tariff_plans', 'is_highlighted')
    op.drop_column('tariff_plans', 'badge_text')
    op.drop_column('tariff_plans', 'description')

    bind = op.get_bind()
    TARIFF_DEVICE_MODE.drop(bind, checkfirst=True)
    TARIFF_TRAFFIC_MODE.drop(bind, checkfirst=True)
    TARIFF_PRICING_MODE.drop(bind, checkfirst=True)
