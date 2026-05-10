from __future__ import annotations

import enum
import secrets
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text as sa_text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


class TransactionType(str, enum.Enum):
    income = 'income'
    outcome = 'outcome'


class InvoicePurpose(str, enum.Enum):
    tariff = 'tariff'
    topup = 'topup'
    balance_topup = 'balance_topup'
    device_topup = 'device_topup'


class InvoiceStatus(str, enum.Enum):
    pending = 'pending'
    paid = 'paid'
    applying = 'applying'
    consumed = 'consumed'
    cancelled = 'cancelled'


class ReferralSource(str, enum.Enum):
    link = 'link'
    code = 'code'


class SupportTicketStatus(str, enum.Enum):
    waiting_operator = 'waiting_operator'
    waiting_user = 'waiting_user'
    closed = 'closed'



class SupportSenderType(str, enum.Enum):
    user = 'user'
    admin = 'admin'


class BroadcastJobStatus(str, enum.Enum):
    draft = 'draft'
    scheduled = 'scheduled'
    pending = 'scheduled'
    running = 'running'
    completed = 'completed'
    failed = 'failed'
    cancelled = 'cancelled'


class BroadcastDeliveryStatus(str, enum.Enum):
    sent = 'sent'
    failed = 'failed'
    skipped_blocked = 'skipped_blocked'
    bot_blocked = 'bot_blocked'


class OutboxStatus(str, enum.Enum):
    pending = 'pending'
    processing = 'processing'
    sent = 'sent'
    failed = 'failed'
    dead = 'dead'


class OutboxKind(str, enum.Enum):
    tg_message = 'tg_message'


class TariffPricingMode(str, enum.Enum):
    fixed = 'fixed'
    constructor = 'constructor'


class TariffTrafficMode(str, enum.Enum):
    fixed = 'fixed'
    constructor = 'constructor'
    unlimited = 'unlimited'


class TariffDeviceMode(str, enum.Enum):
    fixed = 'fixed'
    constructor = 'constructor'
    unlimited = 'unlimited'


class NodeHealthStatus(str, enum.Enum):
    unknown = 'unknown'
    healthy = 'healthy'
    degraded = 'degraded'
    unhealthy = 'unhealthy'
    disabled = 'disabled'


class NodeSourceStatus(str, enum.Enum):
    unknown = 'unknown'
    active = 'active'
    disabled = 'disabled'


class NodeSyncState(str, enum.Enum):
    never_synced = 'never_synced'
    synced = 'synced'
    missing = 'missing'
    error = 'error'


class AuditAction(str, enum.Enum):
    invoice_cancelled = 'invoice_cancelled'
    invoice_paid = 'invoice_paid'
    ticket_closed = 'ticket_closed'
    promo_redeemed = 'promo_redeemed'
    referral_activated = 'referral_activated'
    admin_action = 'admin_action'

    pricing_updated = 'pricing_updated'
    trial_settings_updated = 'trial_settings_updated'
    antispam_settings_updated = 'antispam_settings_updated'
    rules_links_updated = 'rules_links_updated'
    people_settings_updated = 'people_settings_updated'
    ui_settings_updated = 'ui_settings_updated'
    support_chat_tested = 'support_chat_tested'
    balance_adjusted = 'balance_adjusted'
    promo_created = 'promo_created'
    broadcast_created = 'broadcast_created'
    node_registry_updated = 'node_registry_updated'
    routing_profile_updated = 'routing_profile_updated'
    notification_rule_toggled = 'notification_rule_toggled'
    notification_rule_updated = 'notification_rule_updated'
    notification_rule_test_sent = 'notification_rule_test_sent'
    traffic_topup_option_created = 'traffic_topup_option_created'
    traffic_topup_option_updated = 'traffic_topup_option_updated'
    traffic_topup_option_deleted = 'traffic_topup_option_deleted'
    traffic_topup_option_toggled = 'traffic_topup_option_toggled'
    mid_cycle_device_settings_updated = 'mid_cycle_device_settings_updated'
    web_admin_action = 'web_admin_action'


class AuditActorType(str, enum.Enum):
    system = 'system'
    user = 'user'
    admin = 'admin'


class WebAdminRole(str, enum.Enum):
    """RBAC-роли веб-админки (FEA-C39).

    superadmin: полный доступ, единственная роль которая управляет
        web_admin_users и settings без ограничений.
    finance: pricing/promocodes/invoices/balance — финансовые операции.
    support: tickets/users (read+ограниченные write)/broadcasts —
        ежедневная работа саппорта.
    readonly: только GET; не может выполнять mutation-роуты.
    """
    superadmin = 'superadmin'
    finance = 'finance'
    support = 'support'
    readonly = 'readonly'


def generate_referral_code() -> str:
    return secrets.token_hex(5)


class User(TimestampMixin, Base):
    __tablename__ = 'users'
    __table_args__ = (
        CheckConstraint('balance >= 0', name='ck_users_balance_non_negative'),
        Index('ix_users_bot_blocked_is_blocked_id', 'bot_blocked', 'is_blocked', 'id'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    balance: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        default=Decimal('0.00'),
        server_default=sa_text('0.00'),
        nullable=False,
    )
    referral_code: Mapped[str] = mapped_column(
        String(32),
        unique=True,
        index=True,
        nullable=False,
        default=generate_referral_code,
    )
    trial_issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_blocked: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=sa_text('false'),
        nullable=False,
        index=True,
    )
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    blocked_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    bot_blocked: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=sa_text('false'),
        nullable=False,
        index=True,
    )
    bot_blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    bot_blocked_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    subscriptions: Mapped[list['Subscription']] = relationship(back_populates='user', cascade='all, delete-orphan')
    transactions: Mapped[list['Transaction']] = relationship(back_populates='user')
    invited_referrals: Mapped[list['Referral']] = relationship(
        back_populates='inviter',
        foreign_keys='Referral.inviter_id',
    )
    inviter_referral: Mapped['Referral | None'] = relationship(
        back_populates='invited',
        foreign_keys='Referral.invited_id',
        uselist=False,
    )
    invoices: Mapped[list['Invoice']] = relationship(back_populates='user')
    support_tickets: Mapped[list['SupportTicket']] = relationship(back_populates='user')
    promo_redemptions: Mapped[list['PromoRedemption']] = relationship(back_populates='user')


class Subscription(TimestampMixin, Base):
    __tablename__ = 'subscriptions'
    __table_args__ = (
        CheckConstraint('used_traffic_bytes >= 0', name='ck_subscriptions_used_traffic_non_negative'),
        CheckConstraint(
            'monthly_traffic_bytes IS NULL OR monthly_traffic_bytes >= 0',
            name='ck_subscriptions_monthly_traffic_non_negative',
        ),
        CheckConstraint(
            'traffic_cycle_base_bytes IS NULL OR traffic_cycle_base_bytes >= 0',
            name='ck_subscriptions_cycle_base_non_negative',
        ),
        CheckConstraint(
            'cycle_extra_traffic_bytes >= 0',
            name='ck_subscriptions_cycle_extra_traffic_non_negative',
        ),
        CheckConstraint('online_limit IS NULL OR online_limit >= 1', name='ck_subscriptions_online_limit_positive'),
        CheckConstraint(
            "("
            'traffic_cycle_start_at IS NULL AND traffic_cycle_end_at IS NULL'
            ') OR ('
            'traffic_cycle_start_at IS NOT NULL AND '
            'traffic_cycle_end_at IS NOT NULL AND '
            'traffic_cycle_end_at > traffic_cycle_start_at'
            ')',
            name='ck_subscriptions_traffic_cycle_bounds_valid',
        ),
        Index('ix_subscriptions_is_active_id', 'is_active', 'id'),
        Index('ix_subscriptions_traffic_cycle_end_id', 'traffic_cycle_end_at', 'id'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    marzban_username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    service_id: Mapped[str] = mapped_column(String(8), unique=True, index=True, nullable=False)
    current_tariff_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_tariff_id: Mapped[int | None] = mapped_column(
        ForeignKey('tariff_plans.id', ondelete='SET NULL'),
        nullable=True,
        index=True,
    )
    expire_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=sa_text('false'),
        nullable=False,
        index=True,
    )
    data_limit_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    used_traffic_bytes: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        server_default=sa_text('0'),
        nullable=False,
    )
    monthly_traffic_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    traffic_cycle_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    traffic_cycle_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    traffic_cycle_base_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cycle_extra_traffic_bytes: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        server_default=sa_text('0'),
        nullable=False,
    )
    last_traffic_reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    next_traffic_reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    used_device_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    used_device_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    online_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    subscription_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_trial: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=sa_text('false'),
        nullable=False,
    )
    notified_3d: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_text('false'), nullable=False)
    notified_1d: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_text('false'), nullable=False)
    notified_exhausted: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_text('false'), nullable=False)
    notified_low_traffic: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_text('false'), nullable=False)
    notified_expired: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_text('false'), nullable=False)
    notified_trial_mid: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_text('false'), nullable=False)
    notified_trial_last_day: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_text('false'), nullable=False)
    notified_trial_post_expire: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_text('false'), nullable=False)

    user: Mapped['User'] = relationship(back_populates='subscriptions')
    tariff_plan: Mapped['TariffPlan | None'] = relationship(back_populates='subscriptions')

    @property
    def effective_cycle_base_bytes(self) -> int | None:
        if self.traffic_cycle_base_bytes is not None:
            return self.traffic_cycle_base_bytes
        if self.monthly_traffic_bytes is not None:
            return self.monthly_traffic_bytes
        if self.data_limit_bytes in (None, 0):
            return None
        fallback_value = self.data_limit_bytes - self.cycle_extra_traffic_bytes
        return max(fallback_value, 0)

    @property
    def effective_cycle_total_bytes(self) -> int | None:
        base_bytes = self.effective_cycle_base_bytes
        if base_bytes is None:
            return None
        return base_bytes + self.cycle_extra_traffic_bytes

    @property
    def current_cycle_remaining_bytes(self) -> int | None:
        total_bytes = self.effective_cycle_total_bytes
        if total_bytes is None:
            return None
        return max(total_bytes - self.used_traffic_bytes, 0)

    @property
    def has_cycle_extra_traffic(self) -> bool:
        return self.cycle_extra_traffic_bytes > 0

    @property
    def is_alive_local(self) -> bool:
        now = datetime.now(timezone.utc)
        not_expired = self.expire_date is None or self.expire_date > now
        under_limit = self.data_limit_bytes in (None, 0) or self.used_traffic_bytes < (self.data_limit_bytes or 0)
        return self.is_active and not_expired and under_limit


class TariffPlan(TimestampMixin, Base):
    __tablename__ = 'tariff_plans'
    __table_args__ = (
        UniqueConstraint('code', name='uq_tariff_plan_code'),
        CheckConstraint("char_length(trim(code)) > 0", name='ck_tariff_plan_code_not_blank'),
        CheckConstraint("char_length(trim(title)) > 0", name='ck_tariff_plan_title_not_blank'),
        CheckConstraint('sort_order >= 0', name='ck_tariff_plan_sort_order_non_negative'),
        CheckConstraint('base_monthly_price >= 0', name='ck_tariff_plan_base_monthly_price_non_negative'),
        CheckConstraint('traffic_step_price >= 0', name='ck_tariff_plan_traffic_step_price_non_negative'),
        CheckConstraint('device_step_price >= 0', name='ck_tariff_plan_device_step_price_non_negative'),
        CheckConstraint('unlimited_devices_surcharge >= 0', name='ck_tariff_plan_unlimited_devices_surcharge_non_negative'),
        CheckConstraint('price_single >= 0', name='ck_tariff_price_single_non_negative'),
        CheckConstraint('price_unlimited >= 0', name='ck_tariff_price_unlimited_non_negative'),
        CheckConstraint('fixed_traffic_gb IS NULL OR fixed_traffic_gb >= 0', name='ck_tariff_plan_fixed_traffic_non_negative'),
        CheckConstraint('min_traffic_gb IS NULL OR min_traffic_gb >= 0', name='ck_tariff_plan_min_traffic_non_negative'),
        CheckConstraint('max_traffic_gb IS NULL OR max_traffic_gb >= 0', name='ck_tariff_plan_max_traffic_non_negative'),
        CheckConstraint('traffic_step_gb IS NULL OR traffic_step_gb >= 1', name='ck_tariff_plan_traffic_step_positive'),
        CheckConstraint('base_traffic_gb IS NULL OR base_traffic_gb >= 0', name='ck_tariff_plan_base_traffic_non_negative'),
        CheckConstraint('fixed_device_count IS NULL OR fixed_device_count >= 1', name='ck_tariff_plan_fixed_device_positive'),
        CheckConstraint('min_device_count IS NULL OR min_device_count >= 1', name='ck_tariff_plan_min_device_positive'),
        CheckConstraint('max_device_count IS NULL OR max_device_count >= 1', name='ck_tariff_plan_max_device_positive'),
        CheckConstraint('device_step IS NULL OR device_step >= 1', name='ck_tariff_plan_device_step_positive'),
        CheckConstraint('base_device_count IS NULL OR base_device_count >= 1', name='ck_tariff_plan_base_device_positive'),
        CheckConstraint(
            'min_traffic_gb IS NULL OR max_traffic_gb IS NULL OR min_traffic_gb <= max_traffic_gb',
            name='ck_tariff_plan_traffic_bounds_valid',
        ),
        CheckConstraint(
            'min_device_count IS NULL OR max_device_count IS NULL OR min_device_count <= max_device_count',
            name='ck_tariff_plan_device_bounds_valid',
        ),
        CheckConstraint(
            'NOT (is_archived AND is_active)',
            name='ck_tariff_plan_archived_not_active',
        ),
        Index('ix_tariff_plan_public_active_sort', 'is_public', 'is_active', 'sort_order', 'id'),
        Index('ix_tariff_plan_archived_sort', 'is_archived', 'sort_order', 'id'),
        Index('ix_tariff_plan_pricing_mode', 'pricing_mode', 'id'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    badge_text: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_highlighted: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=sa_text('false'),
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=sa_text('true'), nullable=False, index=True)
    is_public: Mapped[bool] = mapped_column(Boolean, default=True, server_default=sa_text('true'), nullable=False, index=True)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_text('false'), nullable=False, index=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=100, server_default=sa_text('100'), nullable=False)

    pricing_mode: Mapped[TariffPricingMode] = mapped_column(
        Enum(TariffPricingMode, name='tariff_pricing_mode'),
        nullable=False,
        default=TariffPricingMode.fixed,
        server_default=sa_text("'fixed'"),
        index=True,
    )
    traffic_mode: Mapped[TariffTrafficMode] = mapped_column(
        Enum(TariffTrafficMode, name='tariff_traffic_mode'),
        nullable=False,
        default=TariffTrafficMode.fixed,
        server_default=sa_text("'fixed'"),
        index=True,
    )
    device_mode: Mapped[TariffDeviceMode] = mapped_column(
        Enum(TariffDeviceMode, name='tariff_device_mode'),
        nullable=False,
        default=TariffDeviceMode.fixed,
        server_default=sa_text("'fixed'"),
        index=True,
    )

    base_monthly_price: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
        default=Decimal('0.00'),
        server_default=sa_text('0.00'),
    )
    base_traffic_gb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fixed_traffic_gb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    min_traffic_gb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_traffic_gb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    traffic_step_gb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    traffic_step_price: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
        default=Decimal('0.00'),
        server_default=sa_text('0.00'),
    )

    base_device_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fixed_device_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    min_device_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_device_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    device_step: Mapped[int | None] = mapped_column(Integer, nullable=True)
    device_step_price: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
        default=Decimal('0.00'),
        server_default=sa_text('0.00'),
    )
    allow_unlimited_devices: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=sa_text('false'),
    )
    unlimited_devices_surcharge: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
        default=Decimal('0.00'),
        server_default=sa_text('0.00'),
    )

    # Legacy fields retained during constructor migration to keep runtime compatible
    monthly_traffic_gb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price_single: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal('0.00'), server_default=sa_text('0.00'))
    price_unlimited: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal('0.00'), server_default=sa_text('0.00'))
    online_limit_single: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=sa_text('1'),
    )
    online_limit_unlimited: Mapped[int | None] = mapped_column(Integer, nullable=True)

    period_options: Mapped[list['TariffPeriodOption']] = relationship(
        back_populates='tariff_plan',
        cascade='all, delete-orphan',
        order_by='TariffPeriodOption.sort_order.asc(), TariffPeriodOption.months.asc(), TariffPeriodOption.id.asc()',
    )
    subscriptions: Mapped[list['Subscription']] = relationship(back_populates='tariff_plan')
    invoices: Mapped[list['Invoice']] = relationship(back_populates='tariff_plan')

    @property
    def is_constructor(self) -> bool:
        return self.pricing_mode == TariffPricingMode.constructor

    @property
    def is_public_active(self) -> bool:
        return self.is_public and self.is_active and not self.is_archived

    @property
    def supports_unlimited_traffic(self) -> bool:
        return self.traffic_mode == TariffTrafficMode.unlimited

    @property
    def supports_unlimited_devices(self) -> bool:
        return self.device_mode == TariffDeviceMode.unlimited or self.allow_unlimited_devices


class TariffPeriodOption(TimestampMixin, Base):
    __tablename__ = 'tariff_period_options'
    __table_args__ = (
        UniqueConstraint('tariff_plan_id', 'months', name='uq_tariff_period_options_plan_months'),
        CheckConstraint('months >= 1', name='ck_tariff_period_options_months_positive'),
        CheckConstraint('sort_order >= 0', name='ck_tariff_period_options_sort_order_non_negative'),
        Index('ix_tariff_period_options_plan_sort', 'tariff_plan_id', 'sort_order', 'months', 'id'),
        Index('ix_tariff_period_options_enabled_sort', 'is_enabled', 'sort_order', 'months', 'id'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tariff_plan_id: Mapped[int] = mapped_column(
        ForeignKey('tariff_plans.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    months: Mapped[int] = mapped_column(Integer, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100, server_default=sa_text('100'))
    is_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=sa_text('true'),
        index=True,
    )

    tariff_plan: Mapped['TariffPlan'] = relationship(back_populates='period_options')


class PricingRule(TimestampMixin, Base):
    __tablename__ = 'pricing_rules'
    __table_args__ = (
        CheckConstraint('id = 1', name='ck_pricing_rules_singleton_id'),
        CheckConstraint('base_price >= 0', name='ck_pricing_base_price_non_negative'),
        CheckConstraint('traffic_step_price >= 0', name='ck_pricing_traffic_step_price_non_negative'),
        CheckConstraint('device_step_price >= 0', name='ck_pricing_device_step_price_non_negative'),
        CheckConstraint('unlimited_devices_price >= 0', name='ck_pricing_unlimited_devices_price_non_negative'),
        CheckConstraint('unlimited_combo_price >= 0', name='ck_pricing_unlimited_combo_price_non_negative'),
        CheckConstraint('max_discount_percent >= 0', name='ck_pricing_max_discount_non_negative'),
        CheckConstraint('min_topup_amount >= 0', name='ck_pricing_min_topup_non_negative'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1, server_default=sa_text('1'))
    base_price: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
        default=Decimal('100.00'),
        server_default=sa_text('100.00'),
    )
    base_traffic_gb: Mapped[int] = mapped_column(Integer, nullable=False, default=250, server_default=sa_text('250'))
    traffic_step_gb: Mapped[int] = mapped_column(Integer, nullable=False, default=50, server_default=sa_text('50'))
    traffic_step_price: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
        default=Decimal('40.00'),
        server_default=sa_text('40.00'),
    )
    device_step_price: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
        default=Decimal('20.00'),
        server_default=sa_text('20.00'),
    )
    unlimited_devices_price: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
        default=Decimal('100.00'),
        server_default=sa_text('100.00'),
    )
    unlimited_combo_price: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
        default=Decimal('1000.00'),
        server_default=sa_text('1000.00'),
    )
    max_discount_percent: Mapped[Decimal] = mapped_column(
        Numeric(5, 2),
        nullable=False,
        default=Decimal('25.00'),
        server_default=sa_text('25.00'),
    )
    max_months: Mapped[int] = mapped_column(Integer, nullable=False, default=12, server_default=sa_text('12'))
    min_topup_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
        default=Decimal('50.00'),
        server_default=sa_text('50.00'),
    )


class PromoCode(TimestampMixin, Base):
    __tablename__ = 'promo_codes'
    __table_args__ = (
        UniqueConstraint('code', name='uq_promo_code'),
        CheckConstraint('bonus_amount >= 0', name='ck_promo_bonus_non_negative'),
        CheckConstraint('max_uses IS NULL OR max_uses >= 1', name='ck_promo_max_uses_positive'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    bonus_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    max_uses: Mapped[int | None] = mapped_column(Integer, nullable=True)
    used_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sa_text('0'))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=sa_text('true'), nullable=False, index=True)
    created_by_tg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    redemptions: Mapped[list['PromoRedemption']] = relationship(
        back_populates='promo',
        cascade='all, delete-orphan',
    )


class PromoRedemption(TimestampMixin, Base):
    __tablename__ = 'promo_redemptions'
    __table_args__ = (
        UniqueConstraint('promo_id', 'user_id', name='uq_promo_user_once'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    promo_id: Mapped[int] = mapped_column(ForeignKey('promo_codes.id', ondelete='CASCADE'), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)

    promo: Mapped['PromoCode'] = relationship(back_populates='redemptions')
    user: Mapped['User'] = relationship(back_populates='promo_redemptions')


class AppLink(TimestampMixin, Base):
    __tablename__ = 'app_links'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    os_name: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    download_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    guide_url: Mapped[str | None] = mapped_column(String(512), nullable=True)


class NodeRegistry(TimestampMixin, Base):
    __tablename__ = 'node_registry'
    __table_args__ = (
        UniqueConstraint('code', name='uq_node_registry_code'),
        UniqueConstraint('source_node_id', name='uq_node_registry_source_node_id'),
        CheckConstraint('priority >= 0', name='ck_node_registry_priority_non_negative'),
        CheckConstraint('weight >= 0', name='ck_node_registry_weight_non_negative'),
        CheckConstraint('sort_order >= 0', name='ck_node_registry_sort_order_non_negative'),
        CheckConstraint(
            "(api_base_url IS NULL) OR (char_length(trim(api_base_url)) > 0)",
            name='ck_node_registry_api_base_url_not_blank',
        ),
        CheckConstraint(
            "(subscription_base_url IS NULL) OR (char_length(trim(subscription_base_url)) > 0)",
            name='ck_node_registry_subscription_base_url_not_blank',
        ),
        CheckConstraint(
            "(source_node_id IS NULL) OR (char_length(trim(source_node_id)) > 0)",
            name='ck_node_registry_source_node_id_not_blank',
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)

    source_node_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    source_status: Mapped[NodeSourceStatus] = mapped_column(
        Enum(NodeSourceStatus, name='node_source_status'),
        nullable=False,
        default=NodeSourceStatus.unknown,
        server_default=sa_text("'unknown'"),
        index=True,
    )
    sync_state: Mapped[NodeSyncState] = mapped_column(
        Enum(NodeSyncState, name='node_sync_state'),
        nullable=False,
        default=NodeSyncState.never_synced,
        server_default=sa_text("'never_synced'"),
        index=True,
    )
    source_payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        server_default=sa_text("'{}'::json"),
    )
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    subscription_base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    location_code: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    provider_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    transport_hint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    policy_tags: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default=sa_text("'[]'::json"),
    )
    capabilities_json: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        server_default=sa_text("'{}'::json"),
    )
    is_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=sa_text('true'),
        index=True,
    )
    is_default: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=sa_text('false'),
        index=True,
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100, server_default=sa_text('100'))
    weight: Mapped[int] = mapped_column(Integer, nullable=False, default=100, server_default=sa_text('100'))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100, server_default=sa_text('100'))
    health_status: Mapped[NodeHealthStatus] = mapped_column(
        Enum(NodeHealthStatus, name='node_health_status'),
        nullable=False,
        default=NodeHealthStatus.unknown,
        server_default=sa_text("'unknown'"),
        index=True,
    )
    last_healthcheck_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_health_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    @property
    def is_routable(self) -> bool:
        return self.is_enabled and self.health_status in {
            NodeHealthStatus.healthy,
            NodeHealthStatus.degraded,
        }


class RoutingProfile(TimestampMixin, Base):
    __tablename__ = 'routing_profiles'
    __table_args__ = (
        UniqueConstraint('code', name='uq_routing_profiles_code'),
        CheckConstraint('sort_order >= 0', name='ck_routing_profiles_sort_order_non_negative'),
        CheckConstraint(
            "(description IS NULL) OR (char_length(trim(description)) > 0)",
            name='ck_routing_profiles_description_not_blank',
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=sa_text('true'),
        index=True,
    )
    is_default: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=sa_text('false'),
        index=True,
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100, server_default=sa_text('100'))

    match_tags: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default=sa_text("'[]'::json"),
    )
    config_json: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        server_default=sa_text("'{}'::json"),
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class MarzbanPageSettings(TimestampMixin, Base):
    __tablename__ = 'marzban_page_settings'
    __table_args__ = (
        CheckConstraint('id = 1', name='ck_marzban_page_settings_singleton_id'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1, server_default=sa_text('1'))
    brand_name: Mapped[str] = mapped_column(String(128), nullable=False, default='😎 SwoiVPN')
    page_title: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default='😎 SwoiVPN — Страница подписки',
    )
    hero_title: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default='Добро пожаловать в 😎 SwoiVPN',
    )
    hero_text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default='Здесь вы можете быстро подключить VPN, посмотреть статус подписки и открыть инструкции для своей платформы.',
    )
    connect_button_text: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        default='Подключить в 1 клик',
    )
    connect_hint_text: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default='Откройте ссылку подписки в приложении или импортируйте её вручную, если приложение уже установлено.',
    )
    support_text: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default='Если приложение ещё не установлено — сначала откройте инструкцию для своей платформы, затем подключитесь по кнопке ниже.',
    )
    platforms_title: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default='Платформы подключения',
    )
    platforms_subtitle: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default='Выберите свою платформу, чтобы открыть приложение и инструкцию.',
    )
    show_usage_block: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=sa_text('true'),
    )
    show_subscription_copy_button: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=sa_text('true'),
    )
    show_platform_cards: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=sa_text('true'),
    )

    show_primary_connect_button: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=sa_text('true'),
    )
    show_one_click_block: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=sa_text('true'),
    )
    show_hiddify_button: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=sa_text('true'),
    )
    show_v2raytun_button: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=sa_text('true'),
    )
    show_happ_button: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=sa_text('true'),
    )
    show_qr_button: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=sa_text('true'),
    )


class AppSettings(TimestampMixin, Base):
    __tablename__ = 'app_settings'
    __table_args__ = (
        CheckConstraint('id = 1', name='ck_app_settings_singleton_id'),
        CheckConstraint('trial_duration_days >= 1', name='ck_app_settings_trial_duration_days_positive'),
        CheckConstraint('trial_traffic_gb >= 0', name='ck_app_settings_trial_traffic_gb_non_negative'),
        CheckConstraint('trial_device_count >= 1', name='ck_app_settings_trial_device_count_positive'),
        CheckConstraint('anti_spam_message_limit >= 1', name='ck_app_settings_antispam_message_limit_positive'),
        CheckConstraint(
            'anti_spam_message_window_seconds >= 1',
            name='ck_app_settings_antispam_message_window_positive',
        ),
        CheckConstraint('anti_spam_callback_limit >= 1', name='ck_app_settings_antispam_callback_limit_positive'),
        CheckConstraint(
            'anti_spam_callback_window_seconds >= 1',
            name='ck_app_settings_antispam_callback_window_positive',
        ),
        CheckConstraint('anti_spam_block_seconds >= 1', name='ck_app_settings_antispam_block_seconds_positive'),
        CheckConstraint(
            'anti_spam_min_interval_seconds >= 0',
            name='ck_app_settings_antispam_min_interval_non_negative',
        ),
        CheckConstraint(
            "mid_cycle_device_price_mode IN ('prorated', 'fixed')",
            name='ck_app_settings_mid_cycle_device_price_mode',
        ),
        CheckConstraint(
            'mid_cycle_device_fixed_price >= 0',
            name='ck_app_settings_mid_cycle_device_fixed_price_non_negative',
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1, server_default=sa_text('1'))
    trial_duration_days: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=sa_text('1'))
    trial_traffic_gb: Mapped[int] = mapped_column(Integer, nullable=False, default=5, server_default=sa_text('5'))
    trial_device_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=sa_text('1'))
    anti_spam_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=sa_text('true'))
    anti_spam_message_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=8, server_default=sa_text('8'))
    anti_spam_message_window_seconds: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=12,
        server_default=sa_text('12'),
    )
    anti_spam_callback_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=12, server_default=sa_text('12'))
    anti_spam_callback_window_seconds: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=8,
        server_default=sa_text('8'),
    )
    anti_spam_block_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=10, server_default=sa_text('10'))
    anti_spam_min_interval_seconds: Mapped[Decimal] = mapped_column(
        Numeric(8, 3),
        nullable=False,
        default=Decimal('1.000'),
        server_default=sa_text('1.000'),
    )
    rules_service_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    rules_of_use_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    rules_privacy_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    admin_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list, server_default=sa_text("'[]'::json"))
    support_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list, server_default=sa_text("'[]'::json"))
    support_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    startup_notify_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list, server_default=sa_text("'[]'::json"))
    support_chat_test_last_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default='never',
        server_default=sa_text("'never'"),
    )
    support_chat_test_last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    show_subscription_copy_button: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=sa_text('true'),
    )
    show_subscription_page_button: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=sa_text('true'),
    )
    mid_cycle_device_topup_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=sa_text('true'),
    )
    mid_cycle_device_price_mode: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default='prorated',
        server_default=sa_text("'prorated'"),
    )
    mid_cycle_device_fixed_price: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
        default=Decimal('99.00'),
        server_default=sa_text('99.00'),
    )


class Invoice(TimestampMixin, Base):
    __tablename__ = 'invoices'
    __table_args__ = (
        CheckConstraint('amount >= 0', name='ck_invoices_amount_non_negative'),
        CheckConstraint('balance_used >= 0', name='ck_invoices_balance_used_non_negative'),
        CheckConstraint('payable_amount >= 0', name='ck_invoices_payable_non_negative'),
        UniqueConstraint('provider', 'external_invoice_id', name='uq_invoices_provider_external'),
        Index(
            'uq_invoices_idempotency_key',
            'idempotency_key',
            unique=True,
            postgresql_where=sa_text('idempotency_key IS NOT NULL'),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    purpose: Mapped[InvoicePurpose] = mapped_column(Enum(InvoicePurpose, name='invoice_purpose'), nullable=False)
    status: Mapped[InvoiceStatus] = mapped_column(
        Enum(InvoiceStatus, name='invoice_status'),
        nullable=False,
        default=InvoiceStatus.pending,
        server_default=sa_text("'pending'"),
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default='mock', server_default=sa_text("'mock'"))
    external_invoice_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    balance_used: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
        default=Decimal('0.00'),
        server_default=sa_text('0.00'),
    )
    payable_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default='RUB', server_default=sa_text("'RUB'"))
    payment_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict, server_default=sa_text("'{}'::json"))
    tariff_plan_id: Mapped[int | None] = mapped_column(
        ForeignKey('tariff_plans.id', ondelete='SET NULL'),
        nullable=True,
        index=True,
    )
    tariff_snapshot_json: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        server_default=sa_text("'{}'::json"),
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped['User'] = relationship(back_populates='invoices')
    tariff_plan: Mapped['TariffPlan | None'] = relationship(back_populates='invoices')


class Transaction(TimestampMixin, Base):
    __tablename__ = 'transactions'
    __table_args__ = (
        CheckConstraint('amount >= 0', name='ck_transactions_amount_non_negative'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    type: Mapped[TransactionType] = mapped_column(Enum(TransactionType, name='transaction_type'), nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=False)

    user: Mapped['User'] = relationship(back_populates='transactions')


class Referral(TimestampMixin, Base):
    __tablename__ = 'referrals'
    __table_args__ = (
        UniqueConstraint('invited_id', name='uq_referrals_invited_id'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    inviter_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    invited_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    source: Mapped[ReferralSource] = mapped_column(
        Enum(ReferralSource, name='referral_source'),
        nullable=False,
        default=ReferralSource.link,
        server_default=sa_text("'link'"),
    )
    is_activated: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_text('false'), nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    inviter: Mapped['User'] = relationship(back_populates='invited_referrals', foreign_keys=[inviter_id])
    invited: Mapped['User'] = relationship(back_populates='inviter_referral', foreign_keys=[invited_id])


class SupportTicket(TimestampMixin, Base):
    __tablename__ = 'support_tickets'
    __table_args__ = (
        Index(
            'uq_support_tickets_one_active_per_user',
            'user_id',
            unique=True,
            postgresql_where=sa_text("status IN ('waiting_operator', 'waiting_user')"),
        ),
        Index('ix_support_tickets_user_status_id', 'user_id', 'status', 'id'),
        Index('ix_support_tickets_closed_at_id', 'closed_at', 'id'),
        Index('ix_support_tickets_last_actor_tg_id', 'last_actor_tg_id'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    status: Mapped[SupportTicketStatus] = mapped_column(
        Enum(SupportTicketStatus, name='support_ticket_status'),
        default=SupportTicketStatus.waiting_operator,
        server_default=sa_text("'waiting_operator'"),
        nullable=False,
        index=True,
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    close_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    closed_by_admin_tg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    last_actor_type: Mapped[SupportSenderType | None] = mapped_column(
        Enum(SupportSenderType, name='support_sender_type'),
        nullable=True,
    )
    last_actor_tg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    user: Mapped['User'] = relationship(back_populates='support_tickets')
    messages: Mapped[list['SupportMessage']] = relationship(
        back_populates='ticket',
        cascade='all, delete-orphan',
        order_by='SupportMessage.created_at.asc(), SupportMessage.id.asc()',
    )

    @property
    def is_active(self) -> bool:
        return self.status != SupportTicketStatus.closed

    @property
    def hashtag(self) -> str:
        return f'#ticket{self.id}'


class SupportMessage(TimestampMixin, Base):
    __tablename__ = 'support_messages'
    __table_args__ = (
        Index('ix_support_messages_ticket_created_id', 'ticket_id', 'created_at', 'id'),
        Index('ix_support_messages_admin_chat_message_id', 'admin_chat_message_id'),
        Index('ix_support_messages_sender_type_created_id', 'sender_type', 'created_at', 'id'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey('support_tickets.id', ondelete='CASCADE'), nullable=False, index=True)
    sender_type: Mapped[SupportSenderType] = mapped_column(
        Enum(SupportSenderType, name='support_sender_type'),
        nullable=False,
    )
    sender_tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    media_file_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_file_unique_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    media_mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    media_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    admin_chat_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)

    ticket: Mapped['SupportTicket'] = relationship(back_populates='messages')


class BroadcastJob(TimestampMixin, Base):
    __tablename__ = 'broadcast_jobs'
    __table_args__ = (
        CheckConstraint(
            '('
            '(text IS NOT NULL AND char_length(trim(text)) > 0)'
            ' OR '
            '(photo_file_id IS NOT NULL AND char_length(trim(photo_file_id)) > 0)'
            ')',
            name='ck_broadcast_jobs_has_content',
        ),
        Index('ix_broadcast_jobs_status_run_at_id', 'status', 'run_at', 'id'),
        Index('ix_broadcast_jobs_created_by_status_id', 'created_by_tg_id', 'status', 'id'),
        Index('ix_broadcast_jobs_cancel_requested_at_id', 'cancel_requested_at', 'id'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_by_tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        server_default=sa_text("'{}'::json"),
    )
    photo_file_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    photo_file_unique_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    keyboard_json: Mapped[list[list[dict[str, Any]]]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default=sa_text("'[]'::json"),
    )
    run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
        default=lambda: datetime.now(timezone.utc),
    )
    status: Mapped[BroadcastJobStatus] = mapped_column(
        Enum(BroadcastJobStatus, name='broadcast_job_status'),
        nullable=False,
        default=BroadcastJobStatus.scheduled,
        server_default=sa_text("'scheduled'"),
        index=True,
    )
    total_users: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sa_text('0'))
    processed_users: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sa_text('0'))
    sent_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sa_text('0'))
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sa_text('0'))
    last_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    cancelled_by_tg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    deliveries: Mapped[list['BroadcastJobDelivery']] = relationship(
        back_populates='job',
        cascade='all, delete-orphan',
    )

    @property
    def is_editable(self) -> bool:
        return self.status in {
            BroadcastJobStatus.draft,
            BroadcastJobStatus.scheduled,
        }

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            BroadcastJobStatus.completed,
            BroadcastJobStatus.failed,
            BroadcastJobStatus.cancelled,
        }

    @property
    def can_request_cancel(self) -> bool:
        return self.status in {
            BroadcastJobStatus.scheduled,
            BroadcastJobStatus.running,
        }

    @property
    def has_media(self) -> bool:
        return bool((self.photo_file_id or '').strip())

    @property
    def has_keyboard(self) -> bool:
        return bool(self.keyboard_json)

    @property
    def content_preview_text(self) -> str:
        return (self.text or '').strip()


class BroadcastJobDelivery(TimestampMixin, Base):
    __tablename__ = 'broadcast_job_deliveries'
    __table_args__ = (
        UniqueConstraint('job_id', 'user_id', name='uq_broadcast_job_delivery_job_user'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey('broadcast_jobs.id', ondelete='CASCADE'), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    user_tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    status: Mapped[BroadcastDeliveryStatus] = mapped_column(
        Enum(BroadcastDeliveryStatus, name='broadcast_delivery_status'),
        nullable=False,
        default=BroadcastDeliveryStatus.failed,
        server_default=sa_text("'failed'"),
        index=True,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sa_text('0'))
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    job: Mapped['BroadcastJob'] = relationship(back_populates='deliveries')
    user: Mapped['User'] = relationship()


class AuditLog(TimestampMixin, Base):
    __tablename__ = 'audit_logs'
    __table_args__ = (
        Index('ix_audit_logs_created_at_id', 'created_at', 'id'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action: Mapped[AuditAction] = mapped_column(Enum(AuditAction, name='audit_action'), nullable=False, index=True)
    actor_type: Mapped[AuditActorType] = mapped_column(
        Enum(AuditActorType, name='audit_actor_type'),
        nullable=False,
        index=True,
    )
    actor_tg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    actor_username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict, server_default=sa_text("'{}'::json"))


class OutboxMessage(TimestampMixin, Base):
    __tablename__ = 'outbox_messages'
    __table_args__ = (
        CheckConstraint('attempts >= 0', name='ck_outbox_attempts_non_negative'),
        CheckConstraint('max_attempts > 0', name='ck_outbox_max_attempts_positive'),
        Index('ix_outbox_messages_due', 'status', 'next_attempt_at', 'id'),
        Index(
            'uq_outbox_messages_correlation_key',
            'correlation_key',
            unique=True,
            postgresql_where=sa_text('correlation_key IS NOT NULL'),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[OutboxKind] = mapped_column(
        Enum(OutboxKind, name='outbox_kind'),
        nullable=False,
    )
    target_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=sa_text("'{}'::json")
    )
    status: Mapped[OutboxStatus] = mapped_column(
        Enum(OutboxStatus, name='outbox_status'),
        nullable=False,
        default=OutboxStatus.pending,
        server_default=sa_text("'pending'"),
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sa_text('0'))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=10, server_default=sa_text('10'))
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=sa_text('CURRENT_TIMESTAMP'),
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    correlation_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NotificationRule(TimestampMixin, Base):
    """Конфигурируемое правило push-уведомления (FEA-NOTIF).

    Каждый сценарий (expiring_3d/low_traffic_90/...) идентифицируется `code`.
    Админ может выключить правило (is_enabled=False) или переопределить текст
    и кнопки. При отсутствии правила в БД диспатчер использует вшитый fallback.
    """

    __tablename__ = 'notification_rules'
    __table_args__ = (
        CheckConstraint(
            'cooldown_seconds >= 0',
            name='ck_notification_rules_cooldown_non_negative',
        ),
        UniqueConstraint('code', name='uq_notification_rules_code'),
        Index('ix_notification_rules_enabled_priority', 'is_enabled', 'priority'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text('true')
    )
    template_text: Mapped[str] = mapped_column(Text, nullable=False)
    template_keyboard_json: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    cooldown_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sa_text('0')
    )
    segment_filter_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=100, server_default=sa_text('100')
    )
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)


class TrafficTopupOption(TimestampMixin, Base):
    """Конфигурируемая опция «докупки» трафика (FEA-A8).

    Заменяет хардкод `PricingService.TOPUPS`. Список редактируется через
    web-admin (`/admin/upsells/traffic/`); коды `topup50`/`topup100` сохранены
    в seed для обратной совместимости с smart-push клавиатурами FEA-NOTIF.
    """

    __tablename__ = 'traffic_topup_options'
    __table_args__ = (
        CheckConstraint(
            'extra_traffic_gb > 0',
            name='ck_traffic_topup_options_extra_positive',
        ),
        CheckConstraint(
            'amount >= 0',
            name='ck_traffic_topup_options_amount_non_negative',
        ),
        UniqueConstraint('code', name='uq_traffic_topup_options_code'),
        Index('ix_traffic_topup_options_enabled_sort', 'is_enabled', 'sort_order'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(64), nullable=False)
    extra_traffic_gb: Mapped[int] = mapped_column(Integer, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text('true')
    )
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=100, server_default=sa_text('100')
    )
    badge_label: Mapped[str | None] = mapped_column(String(64), nullable=True)


class WebAdminUser(TimestampMixin, Base):
    """Пользователь веб-админки с RBAC-ролью (FEA-C39).

    Bootstrap: запись с username из `WEB_ADMIN_USERNAME` создаётся при
    старте, если её ещё нет, с ролью `superadmin` и password_hash из
    env (см. app.web.auth.bootstrap_web_admin_from_env). Пока в таблице
    есть legacy-fallback на env-credentials, чтобы не ломать боевые
    деплои до миграции операторами.
    """

    __tablename__ = 'web_admin_users'
    __table_args__ = (
        CheckConstraint(
            "char_length(trim(username)) > 0",
            name='ck_web_admin_users_username_not_blank',
        ),
        CheckConstraint(
            "char_length(password_hash) > 0",
            name='ck_web_admin_users_password_hash_not_blank',
        ),
        Index(
            'uq_web_admin_users_username_lower',
            sa_text('lower(username)'),
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[WebAdminRole] = mapped_column(
        Enum(WebAdminRole, name='web_admin_role'),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text('true')
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
