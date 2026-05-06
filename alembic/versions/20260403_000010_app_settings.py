"""add app settings singleton and extend audit action enum

Revision ID: 20260403_000010
Revises: 20260403_000009
Create Date: 2026-04-03 00:30:00
"""
from __future__ import annotations

import json
from decimal import Decimal

from alembic import op
import sqlalchemy as sa

from app.config import get_settings

revision = '20260403_000010'
down_revision = '20260403_000009'
branch_labels = None
depends_on = None


def _bootstrap_payload() -> dict[str, object]:
    settings = get_settings()

    trial_duration_days = max(
        1,
        int(getattr(settings, 'trial_duration_days', 0) or 0)
        or max(1, int(getattr(settings, 'trial_duration_hours', 24)) // 24),
    )
    trial_traffic_gb = max(0, int(getattr(settings, 'trial_traffic_gb', 5)))
    trial_device_count = max(1, int(getattr(settings, 'trial_device_count', 1)))

    anti_spam_enabled = bool(getattr(settings, 'anti_spam_enabled', True))
    anti_spam_message_limit = max(1, int(getattr(settings, 'anti_spam_message_limit', 8)))
    anti_spam_message_window_seconds = max(1, int(getattr(settings, 'anti_spam_message_window_seconds', 12)))
    anti_spam_callback_limit = max(1, int(getattr(settings, 'anti_spam_callback_limit', 12)))
    anti_spam_callback_window_seconds = max(1, int(getattr(settings, 'anti_spam_callback_window_seconds', 8)))
    anti_spam_block_seconds = max(1, int(getattr(settings, 'anti_spam_block_seconds', 10)))

    min_interval = Decimal(str(getattr(settings, 'anti_spam_min_interval_seconds', 1.0)))
    anti_spam_min_interval_seconds = str(min_interval.quantize(Decimal('0.001')))

    admin_ids = [int(value) for value in (getattr(settings, 'admin_ids', None) or [])]
    support_ids = [int(value) for value in (getattr(settings, 'support_ids', None) or [])]
    startup_notify_ids = [int(value) for value in (getattr(settings, 'startup_notify_ids', None) or [])]
    if not startup_notify_ids:
        startup_notify_ids = list(admin_ids)

    support_chat_id = getattr(settings, 'support_chat_id', None)

    return {
        'trial_duration_days': trial_duration_days,
        'trial_traffic_gb': trial_traffic_gb,
        'trial_device_count': trial_device_count,
        'anti_spam_enabled': anti_spam_enabled,
        'anti_spam_message_limit': anti_spam_message_limit,
        'anti_spam_message_window_seconds': anti_spam_message_window_seconds,
        'anti_spam_callback_limit': anti_spam_callback_limit,
        'anti_spam_callback_window_seconds': anti_spam_callback_window_seconds,
        'anti_spam_block_seconds': anti_spam_block_seconds,
        'anti_spam_min_interval_seconds': anti_spam_min_interval_seconds,
        'rules_service_url': getattr(settings, 'rules_service_url', None),
        'rules_of_use_url': getattr(settings, 'rules_of_use_url', None),
        'rules_privacy_url': getattr(settings, 'rules_privacy_url', None),
        'admin_ids': json.dumps(admin_ids),
        'support_ids': json.dumps(support_ids),
        'support_chat_id': support_chat_id,
        'startup_notify_ids': json.dumps(startup_notify_ids),
        'show_subscription_copy_button': bool(getattr(settings, 'show_subscription_copy_button', True)),
        'show_subscription_page_button': bool(getattr(settings, 'show_subscription_page_button', True)),
    }


def upgrade() -> None:
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'pricing_updated'")
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'trial_settings_updated'")
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'antispam_settings_updated'")
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'rules_links_updated'")
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'people_settings_updated'")
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'ui_settings_updated'")
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'support_chat_tested'")
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'balance_adjusted'")
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'promo_created'")
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'broadcast_created'")

    op.create_table(
        'app_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('trial_duration_days', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('trial_traffic_gb', sa.Integer(), nullable=False, server_default='5'),
        sa.Column('trial_device_count', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('anti_spam_enabled', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('anti_spam_message_limit', sa.Integer(), nullable=False, server_default='8'),
        sa.Column('anti_spam_message_window_seconds', sa.Integer(), nullable=False, server_default='12'),
        sa.Column('anti_spam_callback_limit', sa.Integer(), nullable=False, server_default='12'),
        sa.Column('anti_spam_callback_window_seconds', sa.Integer(), nullable=False, server_default='8'),
        sa.Column('anti_spam_block_seconds', sa.Integer(), nullable=False, server_default='10'),
        sa.Column('anti_spam_min_interval_seconds', sa.Numeric(8, 3), nullable=False, server_default='1.000'),
        sa.Column('rules_service_url', sa.String(length=1024), nullable=True),
        sa.Column('rules_of_use_url', sa.String(length=1024), nullable=True),
        sa.Column('rules_privacy_url', sa.String(length=1024), nullable=True),
        sa.Column('admin_ids', sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column('support_ids', sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column('support_chat_id', sa.BigInteger(), nullable=True),
        sa.Column('startup_notify_ids', sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column('support_chat_test_last_status', sa.String(length=32), nullable=False, server_default='never'),
        sa.Column('support_chat_test_last_error', sa.Text(), nullable=True),
        sa.Column('show_subscription_copy_button', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('show_subscription_page_button', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.CheckConstraint('id = 1', name='ck_app_settings_singleton_id'),
        sa.CheckConstraint('trial_duration_days >= 1', name='ck_app_settings_trial_duration_days_positive'),
        sa.CheckConstraint('trial_traffic_gb >= 0', name='ck_app_settings_trial_traffic_gb_non_negative'),
        sa.CheckConstraint('trial_device_count >= 1', name='ck_app_settings_trial_device_count_positive'),
        sa.CheckConstraint('anti_spam_message_limit >= 1', name='ck_app_settings_antispam_message_limit_positive'),
        sa.CheckConstraint(
            'anti_spam_message_window_seconds >= 1',
            name='ck_app_settings_antispam_message_window_positive',
        ),
        sa.CheckConstraint('anti_spam_callback_limit >= 1', name='ck_app_settings_antispam_callback_limit_positive'),
        sa.CheckConstraint(
            'anti_spam_callback_window_seconds >= 1',
            name='ck_app_settings_antispam_callback_window_positive',
        ),
        sa.CheckConstraint('anti_spam_block_seconds >= 1', name='ck_app_settings_antispam_block_seconds_positive'),
        sa.CheckConstraint(
            'anti_spam_min_interval_seconds >= 0',
            name='ck_app_settings_antispam_min_interval_non_negative',
        ),
        sa.PrimaryKeyConstraint('id'),
    )

    payload = _bootstrap_payload()
    
    # ИСПОЛЬЗУЕМ op.get_bind().execute() ДЛЯ ПЕРЕДАЧИ СЛОВАРЯ ПАРАМЕТРОВ
    op.get_bind().execute(
        sa.text(
            """
            INSERT INTO app_settings (
                id,
                trial_duration_days,
                trial_traffic_gb,
                trial_device_count,
                anti_spam_enabled,
                anti_spam_message_limit,
                anti_spam_message_window_seconds,
                anti_spam_callback_limit,
                anti_spam_callback_window_seconds,
                anti_spam_block_seconds,
                anti_spam_min_interval_seconds,
                rules_service_url,
                rules_of_use_url,
                rules_privacy_url,
                admin_ids,
                support_ids,
                support_chat_id,
                startup_notify_ids,
                support_chat_test_last_status,
                support_chat_test_last_error,
                show_subscription_copy_button,
                show_subscription_page_button,
                created_at,
                updated_at
            )
            VALUES (
                1,
                :trial_duration_days,
                :trial_traffic_gb,
                :trial_device_count,
                :anti_spam_enabled,
                :anti_spam_message_limit,
                :anti_spam_message_window_seconds,
                :anti_spam_callback_limit,
                :anti_spam_callback_window_seconds,
                :anti_spam_block_seconds,
                CAST(:anti_spam_min_interval_seconds AS NUMERIC(8, 3)),
                :rules_service_url,
                :rules_of_use_url,
                :rules_privacy_url,
                CAST(:admin_ids AS JSON),
                CAST(:support_ids AS JSON),
                :support_chat_id,
                CAST(:startup_notify_ids AS JSON),
                'never',
                NULL,
                :show_subscription_copy_button,
                :show_subscription_page_button,
                now(),
                now()
            )
            ON CONFLICT (id) DO NOTHING
            """
        ),
        payload,
    )


def downgrade() -> None:
    op.drop_table('app_settings')
    # enum values in audit_action are intentionally not removed on downgrade