"""initial schema

Revision ID: 20260328_000001
Revises:
Create Date: 2026-03-28 00:00:01
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '20260328_000001'
down_revision = None
branch_labels = None
depends_on = None


invoice_purpose = sa.Enum('tariff', 'topup', 'balance_topup', name='invoice_purpose', create_type=False)
invoice_status = sa.Enum('pending', 'paid', 'consumed', 'cancelled', name='invoice_status', create_type=False)
transaction_type = sa.Enum('income', 'outcome', name='transaction_type', create_type=False)
referral_source = sa.Enum('link', 'code', name='referral_source', create_type=False)
support_ticket_status = sa.Enum('open', 'closed', name='support_ticket_status', create_type=False)
support_sender_type = sa.Enum('user', 'admin', name='support_sender_type', create_type=False)
audit_action = sa.Enum(
    'invoice_cancelled',
    'invoice_paid',
    'ticket_closed',
    'promo_redeemed',
    'referral_activated',
    'admin_action',
    name='audit_action',
    create_type=False,
)
audit_actor_type = sa.Enum('system', 'user', 'admin', name='audit_actor_type', create_type=False)


def _quote_enum_values(*values: str) -> str:
    return ', '.join("'" + value.replace("'", "''") + "'" for value in values)


def _create_enum_type(name: str, *values: str) -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            CREATE TYPE {name} AS ENUM ({_quote_enum_values(*values)});
        EXCEPTION
            WHEN duplicate_object THEN null;
        END
        $$;
        """
    )


def _drop_enum_type(name: str) -> None:
    op.execute(f'DROP TYPE IF EXISTS {name};')


def upgrade() -> None:
    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('tg_id', sa.BigInteger(), nullable=False),
        sa.Column('username', sa.String(length=64), nullable=True),
        sa.Column('balance', sa.Numeric(12, 2), nullable=False, server_default='0.00'),
        sa.Column('referral_code', sa.String(length=32), nullable=False),
        sa.Column('trial_issued_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('first_paid_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('is_blocked', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('blocked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('blocked_reason', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint('balance >= 0', name='ck_users_balance_non_negative'),
        sa.UniqueConstraint('tg_id', name='uq_users_tg_id'),
        sa.UniqueConstraint('referral_code', name='uq_users_referral_code'),
    )
    op.create_index('ix_users_tg_id', 'users', ['tg_id'])
    op.create_index('ix_users_referral_code', 'users', ['referral_code'])
    op.create_index('ix_users_is_blocked', 'users', ['is_blocked'])

    op.create_table(
        'pricing_rules',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('base_price', sa.Numeric(12, 2), nullable=False, server_default='100.00'),
        sa.Column('base_traffic_gb', sa.Integer(), nullable=False, server_default='250'),
        sa.Column('traffic_step_gb', sa.Integer(), nullable=False, server_default='50'),
        sa.Column('traffic_step_price', sa.Numeric(12, 2), nullable=False, server_default='40.00'),
        sa.Column('device_step_price', sa.Numeric(12, 2), nullable=False, server_default='20.00'),
        sa.Column('unlimited_devices_price', sa.Numeric(12, 2), nullable=False, server_default='100.00'),
        sa.Column('unlimited_combo_price', sa.Numeric(12, 2), nullable=False, server_default='1000.00'),
        sa.Column('max_discount_percent', sa.Numeric(5, 2), nullable=False, server_default='25.00'),
        sa.Column('max_months', sa.Integer(), nullable=False, server_default='12'),
        sa.Column('min_topup_amount', sa.Numeric(12, 2), nullable=False, server_default='50.00'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        'subscriptions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('marzban_username', sa.String(length=64), nullable=False),
        sa.Column('current_tariff_code', sa.String(length=64), nullable=True),
        sa.Column('expire_date', sa.DateTime(timezone=True), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('data_limit_bytes', sa.BigInteger(), nullable=True),
        sa.Column('used_traffic_bytes', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('monthly_traffic_bytes', sa.BigInteger(), nullable=True),
        sa.Column('next_traffic_reset_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('used_device_mode', sa.String(length=32), nullable=True),
        sa.Column('used_device_count', sa.Integer(), nullable=True),
        sa.Column('online_limit', sa.Integer(), nullable=True),
        sa.Column('subscription_url', sa.Text(), nullable=True),
        sa.Column('is_trial', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('notified_3d', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('notified_1d', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('notified_exhausted', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('notified_low_traffic', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('notified_expired', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint('used_traffic_bytes >= 0', name='ck_subscriptions_used_traffic_non_negative'),
        sa.CheckConstraint('online_limit IS NULL OR online_limit >= 1', name='ck_subscriptions_online_limit_positive'),
        sa.UniqueConstraint('marzban_username', name='uq_subscriptions_marzban_username'),
    )
    for idx, cols in {
        'ix_subscriptions_user_id': ['user_id'],
        'ix_subscriptions_marzban_username': ['marzban_username'],
        'ix_subscriptions_expire_date': ['expire_date'],
        'ix_subscriptions_is_active': ['is_active'],
        'ix_subscriptions_next_traffic_reset_at': ['next_traffic_reset_at'],
    }.items():
        op.create_index(idx, 'subscriptions', cols)

    op.create_table(
        'tariff_plans',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('code', sa.String(length=64), nullable=False),
        sa.Column('title', sa.String(length=128), nullable=False),
        sa.Column('monthly_traffic_gb', sa.Integer(), nullable=True),
        sa.Column('price_single', sa.Numeric(12, 2), nullable=False),
        sa.Column('price_unlimited', sa.Numeric(12, 2), nullable=False),
        sa.Column('online_limit_single', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('online_limit_unlimited', sa.Integer(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='100'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint('price_single >= 0', name='ck_tariff_price_single_non_negative'),
        sa.CheckConstraint('price_unlimited >= 0', name='ck_tariff_price_unlimited_non_negative'),
        sa.UniqueConstraint('code', name='uq_tariff_plan_code'),
    )
    op.create_index('ix_tariff_plans_code', 'tariff_plans', ['code'])
    op.create_index('ix_tariff_plans_is_active', 'tariff_plans', ['is_active'])

    op.create_table(
        'promo_codes',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('code', sa.String(length=64), nullable=False),
        sa.Column('bonus_amount', sa.Numeric(12, 2), nullable=False),
        sa.Column('max_uses', sa.Integer(), nullable=True),
        sa.Column('used_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('created_by_tg_id', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint('bonus_amount >= 0', name='ck_promo_bonus_non_negative'),
        sa.CheckConstraint('max_uses IS NULL OR max_uses >= 1', name='ck_promo_max_uses_positive'),
        sa.UniqueConstraint('code', name='uq_promo_code'),
    )
    op.create_index('ix_promo_codes_code', 'promo_codes', ['code'])
    op.create_index('ix_promo_codes_is_active', 'promo_codes', ['is_active'])

    op.create_table(
        'promo_redemptions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('promo_id', sa.Integer(), sa.ForeignKey('promo_codes.id', ondelete='CASCADE'), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('promo_id', 'user_id', name='uq_promo_user_once'),
    )
    op.create_index('ix_promo_redemptions_promo_id', 'promo_redemptions', ['promo_id'])
    op.create_index('ix_promo_redemptions_user_id', 'promo_redemptions', ['user_id'])

    op.create_table(
        'invoices',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('purpose', invoice_purpose, nullable=False),
        sa.Column('status', invoice_status, nullable=False),
        sa.Column('provider', sa.String(length=32), nullable=False),
        sa.Column('external_invoice_id', sa.String(length=128), nullable=True),
        sa.Column('amount', sa.Numeric(12, 2), nullable=False),
        sa.Column('balance_used', sa.Numeric(12, 2), nullable=False, server_default='0.00'),
        sa.Column('payable_amount', sa.Numeric(12, 2), nullable=False),
        sa.Column('currency', sa.String(length=8), nullable=False, server_default='RUB'),
        sa.Column('payment_url', sa.Text(), nullable=True),
        sa.Column('payload_json', sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint('amount >= 0', name='ck_invoices_amount_non_negative'),
        sa.CheckConstraint('balance_used >= 0', name='ck_invoices_balance_used_non_negative'),
        sa.CheckConstraint('payable_amount >= 0', name='ck_invoices_payable_non_negative'),
        sa.UniqueConstraint('provider', 'external_invoice_id', name='uq_invoices_provider_external'),
    )
    op.create_index('ix_invoices_user_id', 'invoices', ['user_id'])
    op.create_index('ix_invoices_status', 'invoices', ['status'])
    op.create_index('ix_invoices_external_invoice_id', 'invoices', ['external_invoice_id'])

    op.create_table(
        'transactions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('amount', sa.Numeric(12, 2), nullable=False),
        sa.Column('type', transaction_type, nullable=False),
        sa.Column('description', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint('amount >= 0', name='ck_transactions_amount_non_negative'),
    )
    op.create_index('ix_transactions_user_id', 'transactions', ['user_id'])

    op.create_table(
        'referrals',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('inviter_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('invited_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('source', referral_source, nullable=False),
        sa.Column('is_activated', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('activated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('invited_id', name='uq_referrals_invited_id'),
    )
    op.create_index('ix_referrals_inviter_id', 'referrals', ['inviter_id'])

    op.create_table(
        'support_tickets',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('status', support_ticket_status, nullable=False),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('close_reason', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_support_tickets_user_id', 'support_tickets', ['user_id'])
    op.create_index('ix_support_tickets_status', 'support_tickets', ['status'])

    op.create_table(
        'support_messages',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('ticket_id', sa.Integer(), sa.ForeignKey('support_tickets.id', ondelete='CASCADE'), nullable=False),
        sa.Column('sender_type', support_sender_type, nullable=False),
        sa.Column('sender_tg_id', sa.BigInteger(), nullable=False),
        sa.Column('text', sa.Text(), nullable=True),
        sa.Column('media_type', sa.String(length=16), nullable=True),
        sa.Column('media_file_id', sa.Text(), nullable=True),
        sa.Column('admin_chat_message_id', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_support_messages_ticket_id', 'support_messages', ['ticket_id'])
    op.create_index('ix_support_messages_sender_tg_id', 'support_messages', ['sender_tg_id'])
    op.create_index('ix_support_messages_admin_chat_message_id', 'support_messages', ['admin_chat_message_id'])

    op.create_table(
        'audit_logs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('action', audit_action, nullable=False),
        sa.Column('actor_type', audit_actor_type, nullable=False),
        sa.Column('actor_tg_id', sa.BigInteger(), nullable=True),
        sa.Column('entity_type', sa.String(length=64), nullable=False),
        sa.Column('entity_id', sa.String(length=64), nullable=False),
        sa.Column('details', sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    for idx, cols in {
        'ix_audit_logs_action': ['action'],
        'ix_audit_logs_actor_type': ['actor_type'],
        'ix_audit_logs_actor_tg_id': ['actor_tg_id'],
        'ix_audit_logs_entity_type': ['entity_type'],
        'ix_audit_logs_entity_id': ['entity_id'],
    }.items():
        op.create_index(idx, 'audit_logs', cols)


def downgrade() -> None:
    for idx in ['ix_audit_logs_entity_id', 'ix_audit_logs_entity_type', 'ix_audit_logs_actor_tg_id', 'ix_audit_logs_actor_type', 'ix_audit_logs_action']:
        op.drop_index(idx, table_name='audit_logs')
    op.drop_table('audit_logs')

    for idx in ['ix_support_messages_admin_chat_message_id', 'ix_support_messages_sender_tg_id', 'ix_support_messages_ticket_id']:
        op.drop_index(idx, table_name='support_messages')
    op.drop_table('support_messages')

    for idx in ['ix_support_tickets_status', 'ix_support_tickets_user_id']:
        op.drop_index(idx, table_name='support_tickets')
    op.drop_table('support_tickets')

    op.drop_index('ix_referrals_inviter_id', table_name='referrals')
    op.drop_table('referrals')

    op.drop_index('ix_transactions_user_id', table_name='transactions')
    op.drop_table('transactions')

    for idx in ['ix_invoices_external_invoice_id', 'ix_invoices_status', 'ix_invoices_user_id']:
        op.drop_index(idx, table_name='invoices')
    op.drop_table('invoices')

    for idx in ['ix_promo_redemptions_user_id', 'ix_promo_redemptions_promo_id']:
        op.drop_index(idx, table_name='promo_redemptions')
    op.drop_table('promo_redemptions')

    for idx in ['ix_promo_codes_is_active', 'ix_promo_codes_code']:
        op.drop_index(idx, table_name='promo_codes')
    op.drop_table('promo_codes')

    for idx in ['ix_tariff_plans_is_active', 'ix_tariff_plans_code']:
        op.drop_index(idx, table_name='tariff_plans')
    op.drop_table('tariff_plans')

    for idx in ['ix_subscriptions_next_traffic_reset_at', 'ix_subscriptions_is_active', 'ix_subscriptions_expire_date', 'ix_subscriptions_marzban_username', 'ix_subscriptions_user_id']:
        op.drop_index(idx, table_name='subscriptions')
    op.drop_table('subscriptions')

    op.drop_table('pricing_rules')

    for idx in ['ix_users_is_blocked', 'ix_users_referral_code', 'ix_users_tg_id']:
        op.drop_index(idx, table_name='users')
    op.drop_table('users')

    _drop_enum_type('audit_actor_type')
    _drop_enum_type('audit_action')
    _drop_enum_type('support_sender_type')
    _drop_enum_type('support_ticket_status')
    _drop_enum_type('referral_source')
    _drop_enum_type('transaction_type')
    _drop_enum_type('invoice_status')
    _drop_enum_type('invoice_purpose')