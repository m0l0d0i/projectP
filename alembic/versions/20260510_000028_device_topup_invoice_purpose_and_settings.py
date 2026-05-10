"""device_topup invoice purpose + mid-cycle device price settings (FEA-A9)

Revision ID: 20260510_000028
Revises: 20260509_000027
Create Date: 2026-05-10 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '20260510_000028'
down_revision = '20260509_000027'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Расширяем enum invoice_purpose новым значением.
    # ALTER TYPE ... ADD VALUE нельзя выполнять внутри транзакции.
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute(
            sa.text("ALTER TYPE invoice_purpose ADD VALUE IF NOT EXISTS 'device_topup'")
            .execution_options(autocommit=True)
        )

    # 2. Добавляем 3 поля в app_settings: on/off, режим расчёта, фикс-цена.
    # mid_cycle_device_topup_enabled = TRUE — фича доступна по умолчанию.
    # mid_cycle_device_price_mode IN ('prorated','fixed') — prorated по дням
    # до конца цикла (умножая на device_step_price из тарифа/правил),
    # либо fixed — берётся mid_cycle_device_fixed_price.
    op.add_column(
        'app_settings',
        sa.Column(
            'mid_cycle_device_topup_enabled',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('true'),
        ),
    )
    op.add_column(
        'app_settings',
        sa.Column(
            'mid_cycle_device_price_mode',
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'prorated'"),
        ),
    )
    op.add_column(
        'app_settings',
        sa.Column(
            'mid_cycle_device_fixed_price',
            sa.Numeric(10, 2),
            nullable=False,
            server_default=sa.text('99.00'),
        ),
    )
    op.create_check_constraint(
        'ck_app_settings_mid_cycle_device_price_mode',
        'app_settings',
        "mid_cycle_device_price_mode IN ('prorated', 'fixed')",
    )
    op.create_check_constraint(
        'ck_app_settings_mid_cycle_device_fixed_price_non_negative',
        'app_settings',
        'mid_cycle_device_fixed_price >= 0',
    )


def downgrade() -> None:
    op.drop_constraint(
        'ck_app_settings_mid_cycle_device_fixed_price_non_negative',
        'app_settings',
        type_='check',
    )
    op.drop_constraint(
        'ck_app_settings_mid_cycle_device_price_mode',
        'app_settings',
        type_='check',
    )
    op.drop_column('app_settings', 'mid_cycle_device_fixed_price')
    op.drop_column('app_settings', 'mid_cycle_device_price_mode')
    op.drop_column('app_settings', 'mid_cycle_device_topup_enabled')
    # Значение enum invoice_purpose='device_topup' намеренно не удаляем —
    # PG не поддерживает удаление enum value, а пересоздание ENUM на проде
    # с существующими invoices требует full rewrite. Downgrade irrecoverable
    # для уже созданных device_topup-инвойсов.
