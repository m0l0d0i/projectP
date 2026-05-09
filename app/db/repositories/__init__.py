from __future__ import annotations

import secrets
import string
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    AppLink,
    AppSettings,
    AuditAction,
    AuditActorType,
    AuditLog,
    BroadcastDeliveryStatus,
    BroadcastJob,
    BroadcastJobDelivery,
    BroadcastJobStatus,
    Invoice,
    InvoicePurpose,
    InvoiceStatus,
    MarzbanPageSettings,
    NotificationRule,
    TrafficTopupOption,
    OutboxKind,
    OutboxMessage,
    OutboxStatus,
    PricingRule,
    PromoCode,
    PromoRedemption,
    Referral,
    ReferralSource,
    Subscription,
    SupportMessage,
    SupportSenderType,
    SupportTicket,
    SupportTicketStatus,
    TariffDeviceMode,
    TariffPeriodOption,
    TariffPlan,
    TariffPricingMode,
    TariffTrafficMode,
    Transaction,
    TransactionType,
    User,
)
from app.services.subscription_urls import (
    SubscriptionUrlError,
    canonicalize_subscription_url,
    normalize_public_subscription_origin,
)


RUB = Decimal('0.01')
THREE_PLACES = Decimal('0.001')


def _quantize_decimal(value: Decimal | int | float | str, quantum: Decimal) -> Decimal:
    if isinstance(value, Decimal):
        dec_value = value
    else:
        dec_value = Decimal(str(value))
    return dec_value.quantize(quantum, rounding=ROUND_HALF_UP)


def _money(value: Decimal | int | float | str) -> Decimal:
    return _quantize_decimal(value, RUB)


def _seconds_decimal(value: Decimal | int | float | str) -> Decimal:
    normalized = _quantize_decimal(value, THREE_PLACES)
    return max(Decimal('0.000'), normalized)


def _normalize_optional_str(value: str | None) -> str | None:
    normalized = (value or '').strip()
    return normalized or None


def _normalize_username(value: str | None) -> str | None:
    normalized = _normalize_optional_str(value)
    if normalized is None:
        return None
    return normalized.lower()


def _normalize_optional_bigint(value: int | str | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value

    normalized = value.strip()
    if not normalized:
        return None
    return int(normalized)


def _normalize_utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _normalize_non_negative_bigint(value: int | None) -> int | None:
    if value is None:
        return None
    return max(0, int(value))


def _normalize_int_list(
    values: list[int | str] | tuple[int | str, ...] | set[int | str] | None,
) -> list[int]:
    if not values:
        return []

    result: list[int] = []
    seen: set[int] = set()

    for value in values:
        if value is None:
            continue

        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                continue
            normalized = int(stripped)
        else:
            normalized = int(value)

        if normalized in seen:
            continue

        seen.add(normalized)
        result.append(normalized)

    return result


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _apply_profile_fields(
        user: User,
        *,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> bool:
        changed = False

        normalized_username = _normalize_username(username)
        normalized_first_name = _normalize_optional_str(first_name)
        normalized_last_name = _normalize_optional_str(last_name)

        if getattr(user, 'username', None) != normalized_username:
            user.username = normalized_username
            changed = True

        if hasattr(user, 'first_name') and getattr(user, 'first_name', None) != normalized_first_name:
            setattr(user, 'first_name', normalized_first_name)
            changed = True

        if hasattr(user, 'last_name') and getattr(user, 'last_name', None) != normalized_last_name:
            setattr(user, 'last_name', normalized_last_name)
            changed = True

        return changed

    @staticmethod
    def _normalize_search_query(query: str) -> str:
        return (query or '').strip().lstrip('@')

    def _build_search_stmt(self, query: str, *, include_subscriptions: bool) -> tuple[bool, object]:
        normalized_query = self._normalize_search_query(query)
        stmt = select(User)

        if include_subscriptions:
            stmt = stmt.outerjoin(Subscription, Subscription.user_id == User.id).distinct()

        if normalized_query.isdigit():
            numeric_value = int(normalized_query)
            stmt = stmt.where(
                or_(
                    User.id == numeric_value,
                    User.tg_id == numeric_value,
                )
            )
            return True, stmt

        pattern = f'%{normalized_query.lower()}%'
        conditions = [
            func.lower(func.coalesce(User.username, '')).like(pattern),
            func.lower(func.coalesce(User.first_name, '')).like(pattern),
            func.lower(func.coalesce(User.last_name, '')).like(pattern),
        ]

        if include_subscriptions:
            conditions.extend(
                [
                    func.lower(func.coalesce(Subscription.service_id, '')).like(pattern),
                    func.lower(func.coalesce(Subscription.marzban_username, '')).like(pattern),
                ]
            )

        stmt = stmt.where(or_(*conditions))
        return True, stmt

    async def get_by_tg_id(self, tg_id: int) -> User | None:
        res = await self.session.execute(select(User).where(User.tg_id == tg_id))
        return res.scalar_one_or_none()

    async def get_by_tg_id_for_update(self, tg_id: int) -> User | None:
        res = await self.session.execute(select(User).where(User.tg_id == tg_id).with_for_update())
        return res.scalar_one_or_none()

    async def get_by_id(self, user_id: int) -> User | None:
        res = await self.session.execute(select(User).where(User.id == user_id))
        return res.scalar_one_or_none()

    async def get_by_id_for_update(self, user_id: int) -> User | None:
        res = await self.session.execute(select(User).where(User.id == user_id).with_for_update())
        return res.scalar_one_or_none()

    async def get_by_referral_code(self, code: str) -> User | None:
        res = await self.session.execute(select(User).where(User.referral_code == code))
        return res.scalar_one_or_none()

    async def create(
        self,
        *,
        tg_id: int,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> User:
        user = User(
            tg_id=tg_id,
            username=_normalize_username(username),
        )
        if hasattr(user, 'first_name'):
            setattr(user, 'first_name', _normalize_optional_str(first_name))
        if hasattr(user, 'last_name'):
            setattr(user, 'last_name', _normalize_optional_str(last_name))

        self.session.add(user)
        await self.session.flush()
        return user

    async def create_or_get(
        self,
        tg_id: int,
        username: str | None,
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> tuple[User, bool]:
        normalized_username = _normalize_username(username)
        normalized_first_name = _normalize_optional_str(first_name)
        normalized_last_name = _normalize_optional_str(last_name)

        stmt = (
            insert(User)
            .values(
                tg_id=tg_id,
                username=normalized_username,
                first_name=normalized_first_name,
                last_name=normalized_last_name,
            )
            .on_conflict_do_nothing(index_elements=['tg_id'])
            .returning(User.id)
        )
        res = await self.session.execute(stmt)
        inserted_id = res.scalar_one_or_none()

        user = await self.get_by_tg_id(tg_id)
        if user is None:
            raise RuntimeError(f'Failed to create or fetch user with tg_id={tg_id}')

        if self._apply_profile_fields(
            user,
            username=normalized_username,
            first_name=normalized_first_name,
            last_name=normalized_last_name,
        ):
            await self.session.flush()

        return user, inserted_id is not None

    async def add_balance(self, user: User, amount: Decimal) -> None:
        user.balance = _money((user.balance or Decimal('0.00')) + amount)

    async def subtract_balance(self, user: User, amount: Decimal) -> None:
        user.balance = _money(max(Decimal('0.00'), (user.balance or Decimal('0.00')) - amount))

    async def set_blocked(self, user: User, blocked: bool, reason: str | None = None) -> None:
        user.is_blocked = blocked
        user.blocked_at = datetime.now(timezone.utc) if blocked else None
        user.blocked_reason = reason if blocked else None

    async def set_bot_blocked(self, user: User, blocked: bool, reason: str | None = None) -> None:
        user.bot_blocked = blocked
        user.bot_blocked_at = datetime.now(timezone.utc) if blocked else None
        user.bot_blocked_reason = reason if blocked else None

    async def search(self, query: str, limit: int = 20) -> list[User]:
        normalized_query = self._normalize_search_query(query)
        if not normalized_query:
            return []

        _, stmt = self._build_search_stmt(normalized_query, include_subscriptions=False)
        stmt = stmt.order_by(User.created_at.desc(), User.id.desc()).limit(limit)
        res = await self.session.execute(stmt)
        return list(res.scalars().all())

    async def search_extended(self, query: str, *, limit: int = 20, offset: int = 0) -> list[User]:
        normalized_query = self._normalize_search_query(query)
        if not normalized_query:
            return []

        _, stmt = self._build_search_stmt(normalized_query, include_subscriptions=True)
        stmt = stmt.order_by(User.created_at.desc(), User.id.desc()).offset(offset).limit(limit)
        res = await self.session.execute(stmt)
        return list(res.scalars().all())

    async def count_search_extended(self, query: str) -> int:
        normalized_query = self._normalize_search_query(query)
        if not normalized_query:
            return 0

        stmt = select(func.count(func.distinct(User.id))).select_from(User).outerjoin(
            Subscription,
            Subscription.user_id == User.id,
        )

        if normalized_query.isdigit():
            numeric_value = int(normalized_query)
            stmt = stmt.where(
                or_(
                    User.id == numeric_value,
                    User.tg_id == numeric_value,
                )
            )
        else:
            pattern = f'%{normalized_query.lower()}%'
            stmt = stmt.where(
                or_(
                    func.lower(func.coalesce(User.username, '')).like(pattern),
                    func.lower(func.coalesce(User.first_name, '')).like(pattern),
                    func.lower(func.coalesce(User.last_name, '')).like(pattern),
                    func.lower(func.coalesce(Subscription.service_id, '')).like(pattern),
                    func.lower(func.coalesce(Subscription.marzban_username, '')).like(pattern),
                )
            )

        res = await self.session.execute(stmt)
        return int(res.scalar_one())

    async def list_recent(self, limit: int = 20, offset: int = 0) -> list[User]:
        res = await self.session.execute(
            select(User)
            .order_by(User.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(res.scalars().all())

    async def list_after_id(self, *, last_user_id: int | None = None, limit: int = 100) -> list[User]:
        stmt = select(User)
        if last_user_id is not None:
            stmt = stmt.where(User.id > last_user_id)
        stmt = stmt.order_by(User.id.asc()).limit(limit)
        res = await self.session.execute(stmt)
        return list(res.scalars().all())

    async def count(self) -> int:
        res = await self.session.execute(select(func.count(User.id)))
        return int(res.scalar_one())

    async def count_broadcast_recipients(self) -> int:
        res = await self.session.execute(
            select(func.count(User.id)).where(
                User.bot_blocked.is_(False),
                User.is_blocked.is_(False),
            )
        )
        return int(res.scalar_one())

    async def list_broadcast_recipients_chunk(
        self,
        after_id: int = 0,
        limit: int = 1000,
    ) -> list[User]:
        res = await self.session.execute(
            select(User)
            .where(
                User.id > after_id,
                User.bot_blocked.is_(False),
                User.is_blocked.is_(False),
            )
            .order_by(User.id.asc())
            .limit(limit)
        )
        return list(res.scalars().all())

    async def list_broadcast_recipients(self, *, after_id: int = 0, limit: int = 1000) -> list[User]:
        return await self.list_broadcast_recipients_chunk(after_id=after_id, limit=limit)


class AppSettingsRepository:
    SINGLETON_ID = 1

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self) -> AppSettings | None:
        res = await self.session.execute(
            select(AppSettings).where(AppSettings.id == self.SINGLETON_ID)
        )
        return res.scalar_one_or_none()

    async def get_for_update(self) -> AppSettings | None:
        res = await self.session.execute(
            select(AppSettings)
            .where(AppSettings.id == self.SINGLETON_ID)
            .with_for_update()
        )
        return res.scalar_one_or_none()

    async def ensure(self) -> AppSettings:
        row = await self.get()
        if row is not None:
            return row

        try:
            async with self.session.begin_nested():
                row = AppSettings(id=self.SINGLETON_ID)
                self.session.add(row)
                await self.session.flush()
                return row
        except IntegrityError:
            row = await self.get()
            if row is not None:
                return row
            raise

    async def update_trial_settings(
        self,
        row: AppSettings,
        *,
        trial_duration_days: int,
        trial_traffic_gb: int,
        trial_device_count: int,
    ) -> AppSettings:
        row.trial_duration_days = max(1, int(trial_duration_days))
        row.trial_traffic_gb = max(0, int(trial_traffic_gb))
        row.trial_device_count = max(1, int(trial_device_count))
        await self.session.flush()
        return row

    async def update_antispam_settings(
        self,
        row: AppSettings,
        *,
        anti_spam_enabled: bool,
        anti_spam_message_limit: int,
        anti_spam_message_window_seconds: int,
        anti_spam_callback_limit: int,
        anti_spam_callback_window_seconds: int,
        anti_spam_block_seconds: int,
        anti_spam_min_interval_seconds: Decimal | int | float | str,
    ) -> AppSettings:
        row.anti_spam_enabled = bool(anti_spam_enabled)
        row.anti_spam_message_limit = max(1, int(anti_spam_message_limit))
        row.anti_spam_message_window_seconds = max(1, int(anti_spam_message_window_seconds))
        row.anti_spam_callback_limit = max(1, int(anti_spam_callback_limit))
        row.anti_spam_callback_window_seconds = max(1, int(anti_spam_callback_window_seconds))
        row.anti_spam_block_seconds = max(1, int(anti_spam_block_seconds))
        row.anti_spam_min_interval_seconds = _seconds_decimal(anti_spam_min_interval_seconds)
        await self.session.flush()
        return row

    async def update_rules_links(
        self,
        row: AppSettings,
        *,
        rules_service_url: str | None,
        rules_of_use_url: str | None,
        rules_privacy_url: str | None,
    ) -> AppSettings:
        row.rules_service_url = _normalize_optional_str(rules_service_url)
        row.rules_of_use_url = _normalize_optional_str(rules_of_use_url)
        row.rules_privacy_url = _normalize_optional_str(rules_privacy_url)
        await self.session.flush()
        return row

    async def update_people_settings(
        self,
        row: AppSettings,
        *,
        admin_ids: list[int | str] | tuple[int | str, ...] | set[int | str] | None,
        support_ids: list[int | str] | tuple[int | str, ...] | set[int | str] | None,
        support_chat_id: int | str | None,
        startup_notify_ids: list[int | str] | tuple[int | str, ...] | set[int | str] | None,
    ) -> AppSettings:
        row.admin_ids = _normalize_int_list(admin_ids)
        row.support_ids = _normalize_int_list(support_ids)
        row.support_chat_id = _normalize_optional_bigint(support_chat_id)
        row.startup_notify_ids = _normalize_int_list(startup_notify_ids)
        await self.session.flush()
        return row

    async def update_ui_settings(
        self,
        row: AppSettings,
        *,
        show_subscription_copy_button: bool,
        show_subscription_page_button: bool,
    ) -> AppSettings:
        row.show_subscription_copy_button = bool(show_subscription_copy_button)
        row.show_subscription_page_button = bool(show_subscription_page_button)
        await self.session.flush()
        return row

    async def update_support_chat_test_status(
        self,
        row: AppSettings,
        *,
        status: str,
        error: str | None,
    ) -> AppSettings:
        row.support_chat_test_last_status = (status or '').strip() or 'unknown'
        row.support_chat_test_last_error = _normalize_optional_str(error)
        await self.session.flush()
        return row


class PricingRuleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self) -> PricingRule | None:
        res = await self.session.execute(select(PricingRule).where(PricingRule.id == 1))
        return res.scalar_one_or_none()

    async def get_for_update(self) -> PricingRule | None:
        res = await self.session.execute(
            select(PricingRule)
            .where(PricingRule.id == 1)
            .with_for_update()
        )
        return res.scalar_one_or_none()

    async def get_or_create(self) -> PricingRule:
        row = await self.get()
        if row is not None:
            return row

        try:
            async with self.session.begin_nested():
                row = PricingRule(id=1)
                self.session.add(row)
                await self.session.flush()
                return row
        except IntegrityError:
            row = await self.get()
            if row is not None:
                return row
            raise

    async def ensure(self) -> PricingRule:
        return await self.get_or_create()

    async def update(
        self,
        row: PricingRule,
        *,
        base_price: Decimal | int | float | str,
        base_traffic_gb: int,
        traffic_step_gb: int,
        traffic_step_price: Decimal | int | float | str,
        device_step_price: Decimal | int | float | str,
        unlimited_devices_price: Decimal | int | float | str,
        unlimited_combo_price: Decimal | int | float | str,
        max_discount_percent: Decimal | int | float | str,
        max_months: int,
        min_topup_amount: Decimal | int | float | str,
    ) -> PricingRule:
        row.base_price = _money(base_price)
        row.base_traffic_gb = max(0, int(base_traffic_gb))
        row.traffic_step_gb = max(1, int(traffic_step_gb))
        row.traffic_step_price = _money(traffic_step_price)
        row.device_step_price = _money(device_step_price)
        row.unlimited_devices_price = _money(unlimited_devices_price)
        row.unlimited_combo_price = _money(unlimited_combo_price)
        row.max_discount_percent = _money(max_discount_percent)
        row.max_months = max(1, int(max_months))
        row.min_topup_amount = _money(min_topup_amount)
        await self.session.flush()
        return row


class TariffRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _normalize_code(code: str) -> str:
        normalized = (code or '').strip().lower()
        if not normalized:
            raise ValueError('Tariff code is required')
        return normalized

    @staticmethod
    def _normalize_title(title: str) -> str:
        normalized = (title or '').strip()
        if not normalized:
            raise ValueError('Tariff title is required')
        return normalized

    @staticmethod
    def _normalize_badge(value: str | None) -> str | None:
        normalized = (value or '').strip()
        return normalized[:64] if normalized else None

    @staticmethod
    def _normalize_description(value: str | None) -> str | None:
        normalized = (value or '').strip()
        return normalized or None

    @staticmethod
    def _normalize_period_months(value: int | str) -> int:
        months = int(value)
        if months < 1 or months > 36:
            raise ValueError('Tariff period months must be between 1 and 36')
        return months

    @staticmethod
    def _normalize_mode_value(enum_cls, value, *, field_name: str):
        if isinstance(value, enum_cls):
            return value
        normalized = (str(value or '')).strip()
        if not normalized:
            raise ValueError(f'{field_name} is required')
        try:
            return enum_cls(normalized)
        except ValueError as exc:  # pragma: no cover - defensive validation
            raise ValueError(f'Unsupported {field_name}: {normalized}') from exc

    @staticmethod
    def _normalize_non_negative_int(value: int | None, *, field_name: str) -> int | None:
        if value is None:
            return None
        normalized = int(value)
        if normalized < 0:
            raise ValueError(f'{field_name} must be non-negative')
        return normalized

    @staticmethod
    def _normalize_positive_int(value: int | None, *, field_name: str) -> int | None:
        if value is None:
            return None
        normalized = int(value)
        if normalized < 1:
            raise ValueError(f'{field_name} must be positive')
        return normalized

    @staticmethod
    def _normalize_non_negative_money(value: Decimal | int | float | str | None, *, field_name: str) -> Decimal | None:
        if value is None:
            return None
        normalized = _money(value)
        if normalized < Decimal('0.00'):
            raise ValueError(f'{field_name} must be non-negative')
        return normalized

    @staticmethod
    def _normalize_period_options(
        period_options: list[int] | list[dict[str, object]] | None,
    ) -> list[tuple[int, int, bool]]:
        if not period_options:
            return []

        result: list[tuple[int, int, bool]] = []
        seen: set[int] = set()

        for idx, raw_item in enumerate(period_options, start=1):
            if isinstance(raw_item, dict):
                raw_months = raw_item.get('months')
                if raw_months is None:
                    raise ValueError('Each tariff period option must include months')
                months = TariffRepository._normalize_period_months(int(raw_months))
                sort_order = int(raw_item.get('sort_order', idx))
                is_enabled = bool(raw_item.get('is_enabled', True))
            else:
                months = TariffRepository._normalize_period_months(int(raw_item))
                sort_order = idx
                is_enabled = True

            if months in seen:
                continue

            seen.add(months)
            result.append((months, sort_order, is_enabled))

        result.sort(key=lambda item: (item[1], item[0]))
        return result

    async def list_all(self) -> list[TariffPlan]:
        res = await self.session.execute(
            select(TariffPlan)
            .options(selectinload(TariffPlan.period_options))
            .order_by(TariffPlan.sort_order.asc(), TariffPlan.id.asc())
        )
        return list(res.scalars().all())

    async def list_active(self) -> list[TariffPlan]:
        return await self.list_public_active()

    async def list_public_active(self) -> list[TariffPlan]:
        res = await self.session.execute(
            select(TariffPlan)
            .options(selectinload(TariffPlan.period_options))
            .where(
                TariffPlan.is_active.is_(True),
                TariffPlan.is_public.is_(True),
                TariffPlan.is_archived.is_(False),
            )
            .order_by(TariffPlan.sort_order.asc(), TariffPlan.id.asc())
        )
        return list(res.scalars().all())

    async def list_archived(self) -> list[TariffPlan]:
        res = await self.session.execute(
            select(TariffPlan)
            .options(selectinload(TariffPlan.period_options))
            .where(TariffPlan.is_archived.is_(True))
            .order_by(TariffPlan.archived_at.desc().nullslast(), TariffPlan.sort_order.asc(), TariffPlan.id.asc())
        )
        return list(res.scalars().all())

    async def count(self) -> int:
        res = await self.session.execute(select(func.count(TariffPlan.id)))
        return int(res.scalar_one())

    async def get_by_id(self, tariff_id: int) -> TariffPlan | None:
        res = await self.session.execute(select(TariffPlan).options(selectinload(TariffPlan.period_options)).where(TariffPlan.id == tariff_id))
        return res.scalar_one_or_none()

    async def get_by_id_for_update(self, tariff_id: int) -> TariffPlan | None:
        res = await self.session.execute(
            select(TariffPlan).options(selectinload(TariffPlan.period_options)).where(TariffPlan.id == tariff_id).with_for_update()
        )
        return res.scalar_one_or_none()

    async def get_by_code(self, code: str) -> TariffPlan | None:
        res = await self.session.execute(select(TariffPlan).options(selectinload(TariffPlan.period_options)).where(TariffPlan.code == self._normalize_code(code)))
        return res.scalar_one_or_none()

    async def get_by_code_for_update(self, code: str) -> TariffPlan | None:
        res = await self.session.execute(
            select(TariffPlan)
            .options(selectinload(TariffPlan.period_options))
            .where(TariffPlan.code == self._normalize_code(code))
            .with_for_update()
        )
        return res.scalar_one_or_none()

    async def list_period_options(self, tariff_id: int) -> list[TariffPeriodOption]:
        res = await self.session.execute(
            select(TariffPeriodOption)
            .where(TariffPeriodOption.tariff_plan_id == tariff_id)
            .order_by(TariffPeriodOption.sort_order.asc(), TariffPeriodOption.months.asc(), TariffPeriodOption.id.asc())
        )
        return list(res.scalars().all())

    async def replace_period_options(
        self,
        plan: TariffPlan,
        period_options: list[int] | list[dict[str, object]] | None,
    ) -> list[TariffPeriodOption]:
        normalized = self._normalize_period_options(period_options)
        existing = await self.list_period_options(plan.id)
        for row in existing:
            await self.session.delete(row)
        await self.session.flush()

        created: list[TariffPeriodOption] = []
        for months, sort_order, is_enabled in normalized:
            option = TariffPeriodOption(
                tariff_plan_id=plan.id,
                months=months,
                sort_order=sort_order,
                is_enabled=is_enabled,
            )
            self.session.add(option)
            created.append(option)

        await self.session.flush()
        plan.period_options = list(created)
        return created

    async def count_usage(self, plan: TariffPlan | int | None) -> int:
        if plan is None:
            return 0
        plan_id = plan.id if isinstance(plan, TariffPlan) else int(plan)
        plan_row = await self.get_by_id(plan_id)
        if plan_row is None:
            return 0

        invoice_count_res = await self.session.execute(
            select(func.count(Invoice.id)).where(
                or_(
                    Invoice.tariff_plan_id == plan_id,
                    func.coalesce(Invoice.payload_json['package_code'].astext, '') == plan_row.code,
                )
            )
        )
        subscription_count_res = await self.session.execute(
            select(func.count(Subscription.id)).where(
                or_(
                    Subscription.current_tariff_id == plan_id,
                    Subscription.current_tariff_code == plan_row.code,
                )
            )
        )
        return int(invoice_count_res.scalar_one()) + int(subscription_count_res.scalar_one())

    async def create_plan(
        self,
        *,
        code: str,
        title: str,
        description: str | None = None,
        badge_text: str | None = None,
        is_active: bool = True,
        is_public: bool = True,
        is_archived: bool = False,
        is_highlighted: bool = False,
        sort_order: int = 100,
        pricing_mode: TariffPricingMode | str = TariffPricingMode.constructor,
        traffic_mode: TariffTrafficMode | str = TariffTrafficMode.constructor,
        device_mode: TariffDeviceMode | str = TariffDeviceMode.constructor,
        base_monthly_price: Decimal | int | float | str = Decimal('0.00'),
        base_traffic_gb: int | None = None,
        fixed_traffic_gb: int | None = None,
        min_traffic_gb: int | None = None,
        max_traffic_gb: int | None = None,
        traffic_step_gb: int | None = None,
        traffic_step_price: Decimal | int | float | str | None = None,
        base_device_count: int | None = None,
        fixed_device_count: int | None = None,
        min_device_count: int | None = None,
        max_device_count: int | None = None,
        device_step: int | None = None,
        device_step_price: Decimal | int | float | str | None = None,
        allow_unlimited_devices: bool = False,
        unlimited_devices_surcharge: Decimal | int | float | str | None = None,
        period_options: list[int] | list[dict[str, object]] | None = None,
        legacy_monthly_traffic_gb: int | None = None,
        legacy_price_single: Decimal | int | float | str | None = None,
        legacy_price_unlimited: Decimal | int | float | str | None = None,
        legacy_online_limit_single: int = 1,
        legacy_online_limit_unlimited: int | None = None,
    ) -> TariffPlan:
        existing = await self.get_by_code(code)
        if existing is not None:
            raise ValueError(f'Tariff with code {code!r} already exists')

        plan = TariffPlan(
            code=self._normalize_code(code),
            title=self._normalize_title(title),
            description=self._normalize_description(description),
            badge_text=self._normalize_badge(badge_text),
            is_active=bool(is_active),
            is_public=bool(is_public),
            is_archived=bool(is_archived),
            archived_at=datetime.now(timezone.utc) if is_archived else None,
            is_highlighted=bool(is_highlighted),
            sort_order=int(sort_order),
            pricing_mode=self._normalize_mode_value(TariffPricingMode, pricing_mode, field_name='pricing_mode'),
            traffic_mode=self._normalize_mode_value(TariffTrafficMode, traffic_mode, field_name='traffic_mode'),
            device_mode=self._normalize_mode_value(TariffDeviceMode, device_mode, field_name='device_mode'),
            base_monthly_price=self._normalize_non_negative_money(base_monthly_price, field_name='base_monthly_price') or Decimal('0.00'),
            base_traffic_gb=self._normalize_non_negative_int(base_traffic_gb, field_name='base_traffic_gb'),
            fixed_traffic_gb=self._normalize_non_negative_int(fixed_traffic_gb, field_name='fixed_traffic_gb'),
            min_traffic_gb=self._normalize_non_negative_int(min_traffic_gb, field_name='min_traffic_gb'),
            max_traffic_gb=self._normalize_non_negative_int(max_traffic_gb, field_name='max_traffic_gb'),
            traffic_step_gb=self._normalize_positive_int(traffic_step_gb, field_name='traffic_step_gb'),
            traffic_step_price=self._normalize_non_negative_money(traffic_step_price, field_name='traffic_step_price'),
            base_device_count=self._normalize_positive_int(base_device_count, field_name='base_device_count'),
            fixed_device_count=self._normalize_positive_int(fixed_device_count, field_name='fixed_device_count'),
            min_device_count=self._normalize_positive_int(min_device_count, field_name='min_device_count'),
            max_device_count=self._normalize_positive_int(max_device_count, field_name='max_device_count'),
            device_step=self._normalize_positive_int(device_step, field_name='device_step'),
            device_step_price=self._normalize_non_negative_money(device_step_price, field_name='device_step_price'),
            allow_unlimited_devices=bool(allow_unlimited_devices),
            unlimited_devices_surcharge=self._normalize_non_negative_money(
                unlimited_devices_surcharge,
                field_name='unlimited_devices_surcharge',
            ),
            monthly_traffic_gb=self._normalize_non_negative_int(legacy_monthly_traffic_gb, field_name='monthly_traffic_gb'),
            price_single=self._normalize_non_negative_money(legacy_price_single, field_name='price_single') or Decimal('0.00'),
            price_unlimited=self._normalize_non_negative_money(legacy_price_unlimited, field_name='price_unlimited') or Decimal('0.00'),
            online_limit_single=max(1, int(legacy_online_limit_single)),
            online_limit_unlimited=self._normalize_positive_int(legacy_online_limit_unlimited, field_name='online_limit_unlimited'),
        )
        self.session.add(plan)
        await self.session.flush()
        await self.replace_period_options(plan, period_options)
        return plan

    async def update_plan(
        self,
        plan: TariffPlan,
        *,
        code: str | None = None,
        title: str | None = None,
        description: str | None = None,
        badge_text: str | None = None,
        is_active: bool | None = None,
        is_public: bool | None = None,
        is_archived: bool | None = None,
        is_highlighted: bool | None = None,
        sort_order: int | None = None,
        pricing_mode: TariffPricingMode | str | None = None,
        traffic_mode: TariffTrafficMode | str | None = None,
        device_mode: TariffDeviceMode | str | None = None,
        base_monthly_price: Decimal | int | float | str | None = None,
        base_traffic_gb: int | None = None,
        fixed_traffic_gb: int | None = None,
        min_traffic_gb: int | None = None,
        max_traffic_gb: int | None = None,
        traffic_step_gb: int | None = None,
        traffic_step_price: Decimal | int | float | str | None = None,
        base_device_count: int | None = None,
        fixed_device_count: int | None = None,
        min_device_count: int | None = None,
        max_device_count: int | None = None,
        device_step: int | None = None,
        device_step_price: Decimal | int | float | str | None = None,
        allow_unlimited_devices: bool | None = None,
        unlimited_devices_surcharge: Decimal | int | float | str | None = None,
        period_options: list[int] | list[dict[str, object]] | None = None,
        legacy_monthly_traffic_gb: int | None = None,
        legacy_price_single: Decimal | int | float | str | None = None,
        legacy_price_unlimited: Decimal | int | float | str | None = None,
        legacy_online_limit_single: int | None = None,
        legacy_online_limit_unlimited: int | None = None,
    ) -> TariffPlan:
        if code is not None:
            normalized_code = self._normalize_code(code)
            if normalized_code != plan.code:
                duplicate = await self.get_by_code(normalized_code)
                if duplicate is not None and duplicate.id != plan.id:
                    raise ValueError(f'Tariff with code {normalized_code!r} already exists')
                plan.code = normalized_code

        if title is not None:
            plan.title = self._normalize_title(title)
        if description is not None:
            plan.description = self._normalize_description(description)
        if badge_text is not None:
            plan.badge_text = self._normalize_badge(badge_text)
        if is_active is not None:
            plan.is_active = bool(is_active)
        if is_public is not None:
            plan.is_public = bool(is_public)
        if is_highlighted is not None:
            plan.is_highlighted = bool(is_highlighted)
        if sort_order is not None:
            plan.sort_order = int(sort_order)
        if pricing_mode is not None:
            plan.pricing_mode = self._normalize_mode_value(TariffPricingMode, pricing_mode, field_name='pricing_mode')
        if traffic_mode is not None:
            plan.traffic_mode = self._normalize_mode_value(TariffTrafficMode, traffic_mode, field_name='traffic_mode')
        if device_mode is not None:
            plan.device_mode = self._normalize_mode_value(TariffDeviceMode, device_mode, field_name='device_mode')
        if base_monthly_price is not None:
            plan.base_monthly_price = self._normalize_non_negative_money(base_monthly_price, field_name='base_monthly_price') or Decimal('0.00')
        if base_traffic_gb is not None:
            plan.base_traffic_gb = self._normalize_non_negative_int(base_traffic_gb, field_name='base_traffic_gb')
        if fixed_traffic_gb is not None:
            plan.fixed_traffic_gb = self._normalize_non_negative_int(fixed_traffic_gb, field_name='fixed_traffic_gb')
        if min_traffic_gb is not None:
            plan.min_traffic_gb = self._normalize_non_negative_int(min_traffic_gb, field_name='min_traffic_gb')
        if max_traffic_gb is not None:
            plan.max_traffic_gb = self._normalize_non_negative_int(max_traffic_gb, field_name='max_traffic_gb')
        if traffic_step_gb is not None:
            plan.traffic_step_gb = self._normalize_positive_int(traffic_step_gb, field_name='traffic_step_gb')
        if traffic_step_price is not None:
            plan.traffic_step_price = self._normalize_non_negative_money(traffic_step_price, field_name='traffic_step_price')
        if base_device_count is not None:
            plan.base_device_count = self._normalize_positive_int(base_device_count, field_name='base_device_count')
        if fixed_device_count is not None:
            plan.fixed_device_count = self._normalize_positive_int(fixed_device_count, field_name='fixed_device_count')
        if min_device_count is not None:
            plan.min_device_count = self._normalize_positive_int(min_device_count, field_name='min_device_count')
        if max_device_count is not None:
            plan.max_device_count = self._normalize_positive_int(max_device_count, field_name='max_device_count')
        if device_step is not None:
            plan.device_step = self._normalize_positive_int(device_step, field_name='device_step')
        if device_step_price is not None:
            plan.device_step_price = self._normalize_non_negative_money(device_step_price, field_name='device_step_price')
        if allow_unlimited_devices is not None:
            plan.allow_unlimited_devices = bool(allow_unlimited_devices)
        if unlimited_devices_surcharge is not None:
            plan.unlimited_devices_surcharge = self._normalize_non_negative_money(
                unlimited_devices_surcharge,
                field_name='unlimited_devices_surcharge',
            )
        if legacy_monthly_traffic_gb is not None:
            plan.monthly_traffic_gb = self._normalize_non_negative_int(legacy_monthly_traffic_gb, field_name='monthly_traffic_gb')
        if legacy_price_single is not None:
            plan.price_single = self._normalize_non_negative_money(legacy_price_single, field_name='price_single') or Decimal('0.00')
        if legacy_price_unlimited is not None:
            plan.price_unlimited = self._normalize_non_negative_money(legacy_price_unlimited, field_name='price_unlimited') or Decimal('0.00')
        if legacy_online_limit_single is not None:
            plan.online_limit_single = max(1, int(legacy_online_limit_single))
        if legacy_online_limit_unlimited is not None:
            plan.online_limit_unlimited = self._normalize_positive_int(
                legacy_online_limit_unlimited,
                field_name='online_limit_unlimited',
            )

        if is_archived is not None:
            archive_flag = bool(is_archived)
            plan.is_archived = archive_flag
            if archive_flag:
                plan.is_active = False
                plan.is_public = False
                plan.archived_at = plan.archived_at or datetime.now(timezone.utc)
            else:
                plan.archived_at = None

        await self.session.flush()

        if period_options is not None:
            await self.replace_period_options(plan, period_options)

        return plan

    async def archive(self, plan: TariffPlan) -> TariffPlan:
        plan.is_archived = True
        plan.is_active = False
        plan.is_public = False
        plan.archived_at = datetime.now(timezone.utc)
        await self.session.flush()
        return plan

    async def reactivate(self, plan: TariffPlan, *, is_public: bool = True) -> TariffPlan:
        plan.is_archived = False
        plan.archived_at = None
        plan.is_active = True
        plan.is_public = bool(is_public)
        await self.session.flush()
        return plan

    async def delete_by_id(self, tariff_id: int) -> bool:
        plan = await self.get_by_id_for_update(tariff_id)
        if plan is None:
            return False
        if await self.count_usage(plan) > 0:
            raise ValueError('Used tariff plans cannot be deleted and must be archived instead')
        for option in await self.list_period_options(plan.id):
            await self.session.delete(option)
        await self.session.delete(plan)
        await self.session.flush()
        return True

    async def upsert(
        self,
        *,
        code: str,
        title: str,
        monthly_traffic_gb: int | None,
        price_single: Decimal,
        price_unlimited: Decimal,
        online_limit_single: int = 1,
        online_limit_unlimited: int | None = None,
        is_active: bool = True,
        sort_order: int = 100,
    ) -> TariffPlan:
        existing = await self.get_by_code(code)
        base_monthly_price = _money(price_single)
        unlimited_surcharge = max(_money(price_unlimited) - base_monthly_price, Decimal('0.00'))

        payload = dict(
            title=title,
            is_active=is_active,
            is_public=True,
            is_archived=False,
            sort_order=sort_order,
            pricing_mode=TariffPricingMode.constructor,
            traffic_mode=TariffTrafficMode.fixed if monthly_traffic_gb is not None else TariffTrafficMode.unlimited,
            device_mode=TariffDeviceMode.constructor,
            base_monthly_price=base_monthly_price,
            base_traffic_gb=monthly_traffic_gb,
            fixed_traffic_gb=monthly_traffic_gb,
            min_traffic_gb=monthly_traffic_gb,
            max_traffic_gb=monthly_traffic_gb,
            traffic_step_gb=50,
            traffic_step_price=Decimal('0.00'),
            base_device_count=1,
            fixed_device_count=1 if online_limit_unlimited is None else None,
            min_device_count=1,
            max_device_count=max(1, int(online_limit_unlimited or online_limit_single or 1)),
            device_step=1,
            device_step_price=Decimal('0.00'),
            allow_unlimited_devices=online_limit_unlimited is None,
            unlimited_devices_surcharge=unlimited_surcharge,
            period_options=[1],
            legacy_monthly_traffic_gb=monthly_traffic_gb,
            legacy_price_single=price_single,
            legacy_price_unlimited=price_unlimited,
            legacy_online_limit_single=online_limit_single,
            legacy_online_limit_unlimited=online_limit_unlimited,
        )

        if existing is None:
            return await self.create_plan(code=code, **payload)

        return await self.update_plan(existing, code=code, **payload)

    async def delete(self, code: str) -> bool:
        plan = await self.get_by_code_for_update(code)
        if plan is None:
            return False
        return await self.delete_by_id(plan.id)


class SubscriptionRepository:

    SERVICE_ID_ALPHABET = string.ascii_letters + string.digits

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def _random_service_id(self, length: int = 8) -> str:
        return ''.join(secrets.choice(self.SERVICE_ID_ALPHABET) for _ in range(length))

    @staticmethod
    def _resolve_cycle_total_bytes(subscription: Subscription) -> int | None:
        base_bytes = subscription.traffic_cycle_base_bytes
        if base_bytes is None:
            base_bytes = subscription.monthly_traffic_bytes

        if base_bytes is None:
            return None

        return max(0, int(base_bytes)) + max(0, int(subscription.cycle_extra_traffic_bytes or 0))

    async def service_id_exists(self, service_id: str) -> bool:
        res = await self.session.execute(
            select(Subscription.id).where(Subscription.service_id == service_id).limit(1)
        )
        return res.scalar_one_or_none() is not None

    async def generate_unique_service_id(self, length: int = 8) -> str:
        while True:
            candidate = self._random_service_id(length=length)
            if not await self.service_id_exists(candidate):
                return candidate

    async def list_by_user_id(self, user_id: int) -> list[Subscription]:
        res = await self.session.execute(
            select(Subscription)
            .where(Subscription.user_id == user_id)
            .order_by(Subscription.created_at.desc())
        )
        return list(res.scalars().all())

    async def get_by_id(self, subscription_id: int) -> Subscription | None:
        res = await self.session.execute(select(Subscription).where(Subscription.id == subscription_id))
        return res.scalar_one_or_none()

    async def get_by_service_id(self, service_id: str) -> Subscription | None:
        res = await self.session.execute(select(Subscription).where(Subscription.service_id == service_id))
        return res.scalar_one_or_none()

    async def get_by_service_id_for_update(self, service_id: str) -> Subscription | None:
        res = await self.session.execute(
            select(Subscription)
            .where(Subscription.service_id == service_id)
            .with_for_update()
        )
        return res.scalar_one_or_none()

    async def get_by_id_for_update(self, subscription_id: int) -> Subscription | None:
        res = await self.session.execute(
            select(Subscription).where(Subscription.id == subscription_id).with_for_update()
        )
        return res.scalar_one_or_none()

    async def get_latest(self, user_id: int) -> Subscription | None:
        res = await self.session.execute(
            select(Subscription)
            .where(Subscription.user_id == user_id)
            .order_by(Subscription.created_at.desc())
            .limit(1)
        )
        return res.scalar_one_or_none()

    async def get_latest_for_update(self, user_id: int) -> Subscription | None:
        res = await self.session.execute(
            select(Subscription)
            .where(Subscription.user_id == user_id)
            .order_by(Subscription.created_at.desc())
            .limit(1)
            .with_for_update()
        )
        return res.scalar_one_or_none()

    async def get_latest_active(self, user_id: int) -> Subscription | None:
        now = datetime.now(timezone.utc)
        stmt = (
            select(Subscription)
            .where(
                Subscription.user_id == user_id,
                Subscription.is_active.is_(True),
                or_(Subscription.expire_date.is_(None), Subscription.expire_date > now),
                or_(
                    Subscription.data_limit_bytes.is_(None),
                    Subscription.data_limit_bytes == 0,
                    Subscription.used_traffic_bytes < Subscription.data_limit_bytes,
                ),
            )
            .order_by(Subscription.id.desc())
            .limit(1)
        )
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def get_latest_active_for_update(self, user_id: int) -> Subscription | None:
        now = datetime.now(timezone.utc)
        stmt = (
            select(Subscription)
            .where(
                Subscription.user_id == user_id,
                Subscription.is_active.is_(True),
                or_(Subscription.expire_date.is_(None), Subscription.expire_date > now),
                or_(
                    Subscription.data_limit_bytes.is_(None),
                    Subscription.data_limit_bytes == 0,
                    Subscription.used_traffic_bytes < Subscription.data_limit_bytes,
                ),
            )
            .order_by(Subscription.id.desc())
            .limit(1)
            .with_for_update()
        )
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def create(
        self,
        *,
        user_id: int,
        marzban_username: str,
        service_id: str | None = None,
        current_tariff_id: int | None = None,
        current_tariff_code: str | None = None,
    ) -> Subscription:
        resolved_service_id = service_id or await self.generate_unique_service_id()
        sub = Subscription(
            user_id=user_id,
            marzban_username=marzban_username,
            service_id=resolved_service_id,
            current_tariff_id=current_tariff_id,
            current_tariff_code=_normalize_optional_str(current_tariff_code),
        )
        self.session.add(sub)
        await self.session.flush()
        return sub

    async def set_tariff_context(
        self,
        subscription: Subscription,
        *,
        current_tariff_id: int | None = None,
        current_tariff_code: str | None = None,
    ) -> Subscription:
        subscription.current_tariff_id = current_tariff_id
        subscription.current_tariff_code = _normalize_optional_str(current_tariff_code)
        await self.session.flush()
        return subscription

    async def active_with_users(self) -> list[tuple[Subscription, User]]:
        res = await self.session.execute(
            select(Subscription, User)
            .join(User, User.id == Subscription.user_id)
            .where(Subscription.is_active.is_(True))
        )
        return list(res.all())

    async def trial_pending_milestones(
        self, *, expire_after: datetime
    ) -> list[tuple[Subscription, User]]:
        """Триал-подписки, у которых хотя бы один из notified_trial_* флагов
        ещё не выставлен. Используется в job `check_trial_milestones`.

        Фильтр идёт по `expire_date >= expire_after` (обычно `now -
        max_post_expire_lag`), а не по `created_at` — это автоматически
        работает для триалов любой длительности (1 день, неделя, месяц):
        живой триал имеет `expire_date > now`, post_expire-окно покрывается
        отрицательным запасом. Подписки, у которых post_expire-окно уже
        прошло, выпадают из выборки и не сканируются впустую.
        """
        res = await self.session.execute(
            select(Subscription, User)
            .join(User, User.id == Subscription.user_id)
            .where(
                Subscription.is_trial.is_(True),
                Subscription.expire_date.is_not(None),
                Subscription.expire_date >= expire_after,
                or_(
                    Subscription.notified_trial_mid.is_(False),
                    Subscription.notified_trial_last_day.is_(False),
                    Subscription.notified_trial_post_expire.is_(False),
                ),
            )
        )
        return list(res.all())

    async def due_monthly_resets(self, now: datetime) -> list[Subscription]:
        normalized_now = _normalize_utc_datetime(now) or datetime.now(timezone.utc)
        res = await self.session.execute(
            select(Subscription).where(
                Subscription.is_active.is_(True),
                Subscription.monthly_traffic_bytes.is_not(None),
                or_(
                    Subscription.traffic_cycle_end_at.is_not(None),
                    Subscription.next_traffic_reset_at.is_not(None),
                ),
                or_(
                    Subscription.traffic_cycle_end_at <= normalized_now,
                    Subscription.next_traffic_reset_at <= normalized_now,
                ),
            )
        )
        return list(res.scalars().all())

    async def reset_notification_flags(self, subscription: Subscription) -> None:
        subscription.notified_3d = False
        subscription.notified_1d = False
        subscription.notified_exhausted = False
        subscription.notified_low_traffic = False
        subscription.notified_expired = False

    async def sync_cycle_state(
        self,
        subscription: Subscription,
        *,
        traffic_cycle_start_at: datetime | None,
        traffic_cycle_end_at: datetime | None,
        traffic_cycle_base_bytes: int | None,
        next_traffic_reset_at: datetime | None = None,
        sync_data_limit: bool = True,
    ) -> Subscription:
        normalized_start = _normalize_utc_datetime(traffic_cycle_start_at)
        normalized_end = _normalize_utc_datetime(traffic_cycle_end_at)
        normalized_next_reset = _normalize_utc_datetime(next_traffic_reset_at) or normalized_end

        subscription.traffic_cycle_start_at = normalized_start
        subscription.traffic_cycle_end_at = normalized_end
        subscription.traffic_cycle_base_bytes = _normalize_non_negative_bigint(traffic_cycle_base_bytes)
        subscription.next_traffic_reset_at = normalized_next_reset

        if sync_data_limit:
            await self.sync_data_limit_with_cycle(subscription)

        await self.session.flush()
        return subscription

    async def sync_data_limit_with_cycle(self, subscription: Subscription) -> Subscription:
        total_bytes = self._resolve_cycle_total_bytes(subscription)
        subscription.data_limit_bytes = total_bytes
        await self.session.flush()
        return subscription

    async def add_cycle_extra_traffic(
        self,
        subscription: Subscription,
        *,
        extra_traffic_bytes: int,
        sync_data_limit: bool = True,
    ) -> Subscription:
        extra_bytes = max(0, int(extra_traffic_bytes))
        if extra_bytes <= 0:
            return subscription

        subscription.cycle_extra_traffic_bytes = max(0, int(subscription.cycle_extra_traffic_bytes or 0)) + extra_bytes

        if sync_data_limit:
            await self.sync_data_limit_with_cycle(subscription)
        else:
            await self.session.flush()

        return subscription

    async def clear_cycle_extra_traffic(
        self,
        subscription: Subscription,
        *,
        sync_data_limit: bool = True,
    ) -> Subscription:
        subscription.cycle_extra_traffic_bytes = 0

        if sync_data_limit:
            await self.sync_data_limit_with_cycle(subscription)
        else:
            await self.session.flush()

        return subscription

    async def mark_traffic_cycle_reset(
        self,
        subscription: Subscription,
        *,
        traffic_cycle_start_at: datetime | None,
        traffic_cycle_end_at: datetime | None,
        traffic_cycle_base_bytes: int | None,
        next_traffic_reset_at: datetime | None = None,
        reset_used_traffic: bool = True,
        clear_cycle_extra_traffic: bool = True,
    ) -> Subscription:
        normalized_now = datetime.now(timezone.utc)

        subscription.traffic_cycle_start_at = _normalize_utc_datetime(traffic_cycle_start_at)
        subscription.traffic_cycle_end_at = _normalize_utc_datetime(traffic_cycle_end_at)
        subscription.traffic_cycle_base_bytes = _normalize_non_negative_bigint(traffic_cycle_base_bytes)
        subscription.next_traffic_reset_at = _normalize_utc_datetime(next_traffic_reset_at) or subscription.traffic_cycle_end_at
        subscription.last_traffic_reset_at = normalized_now

        if clear_cycle_extra_traffic:
            subscription.cycle_extra_traffic_bytes = 0

        if reset_used_traffic:
            subscription.used_traffic_bytes = 0

        await self.reset_notification_flags(subscription)
        await self.sync_data_limit_with_cycle(subscription)
        await self.session.flush()
        return subscription

    async def set_subscription_url(self, subscription: Subscription, subscription_url: str | None) -> Subscription:
        normalized_input = _normalize_optional_str(subscription_url)
        if normalized_input is None:
            subscription.subscription_url = None
            await self.session.flush()
            return subscription

        public_origin = normalize_public_subscription_origin(normalized_input)
        canonical_subscription_url = canonicalize_subscription_url(
            normalized_input,
            public_origin=public_origin,
            allow_bare_token=False,
        )
        if canonical_subscription_url is None:
            raise SubscriptionUrlError('Subscription URL must be canonical /sub/<token> and must not use legacy paths.')

        subscription.subscription_url = canonical_subscription_url
        await self.session.flush()
        return subscription

    async def set_runtime_state(
        self,
        subscription: Subscription,
        *,
        is_active: bool | None = None,
        expire_date: datetime | None = None,
        data_limit_bytes: int | None = None,
        used_traffic_bytes: int | None = None,
        online_limit: int | None = None,
        monthly_traffic_bytes: int | None = None,
        next_traffic_reset_at: datetime | None = None,
        current_tariff_id: int | None = None,
        current_tariff_code: str | None = None,
    ) -> Subscription:
        if is_active is not None:
            subscription.is_active = bool(is_active)

        if expire_date is not None:
            subscription.expire_date = _normalize_utc_datetime(expire_date)

        if data_limit_bytes is not None:
            subscription.data_limit_bytes = _normalize_non_negative_bigint(data_limit_bytes)

        if used_traffic_bytes is not None:
            subscription.used_traffic_bytes = max(0, int(used_traffic_bytes))

        if online_limit is not None:
            subscription.online_limit = max(1, int(online_limit))

        if monthly_traffic_bytes is not None:
            subscription.monthly_traffic_bytes = _normalize_non_negative_bigint(monthly_traffic_bytes)

        if next_traffic_reset_at is not None:
            subscription.next_traffic_reset_at = _normalize_utc_datetime(next_traffic_reset_at)

        if current_tariff_id is not None or current_tariff_code is not None:
            subscription.current_tariff_id = current_tariff_id
            subscription.current_tariff_code = _normalize_optional_str(current_tariff_code)

        await self.session.flush()
        return subscription


class InvoiceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        user_id: int,
        purpose: InvoicePurpose,
        amount: Decimal,
        balance_used: Decimal,
        payable_amount: Decimal,
        provider: str,
        payload_json: dict,
        external_invoice_id: str | None = None,
        payment_url: str | None = None,
        currency: str = 'RUB',
        tariff_plan_id: int | None = None,
        tariff_snapshot_json: dict | None = None,
        idempotency_key: str | None = None,
    ) -> Invoice:
        invoice = Invoice(
            user_id=user_id,
            purpose=purpose,
            amount=_money(amount),
            balance_used=_money(balance_used),
            payable_amount=_money(payable_amount),
            provider=provider,
            payload_json=payload_json or {},
            external_invoice_id=external_invoice_id,
            payment_url=payment_url,
            currency=currency,
            tariff_plan_id=tariff_plan_id,
            tariff_snapshot_json=tariff_snapshot_json,
            idempotency_key=idempotency_key,
            status=InvoiceStatus.pending,
        )
        self.session.add(invoice)
        await self.session.flush()
        return invoice

    async def get_by_idempotency_key(self, idempotency_key: str) -> Invoice | None:
        res = await self.session.execute(
            select(Invoice).where(Invoice.idempotency_key == idempotency_key)
        )
        return res.scalar_one_or_none()

    async def get_by_id(self, invoice_id: int) -> Invoice | None:
        res = await self.session.execute(select(Invoice).where(Invoice.id == invoice_id))
        return res.scalar_one_or_none()

    async def get_by_id_for_update(self, invoice_id: int) -> Invoice | None:
        res = await self.session.execute(
            select(Invoice).where(Invoice.id == invoice_id).with_for_update()
        )
        return res.scalar_one_or_none()

    async def get_by_external_invoice_id(self, provider: str, external_invoice_id: str) -> Invoice | None:
        res = await self.session.execute(
            select(Invoice).where(
                Invoice.provider == provider,
                Invoice.external_invoice_id == external_invoice_id,
            )
        )
        return res.scalar_one_or_none()

    async def list_by_user_id(self, user_id: int, *, limit: int = 200) -> list[Invoice]:
        res = await self.session.execute(
            select(Invoice)
            .where(Invoice.user_id == user_id)
            .order_by(Invoice.created_at.desc(), Invoice.id.desc())
            .limit(limit)
        )
        return list(res.scalars().all())

    async def list_recent(self, *, limit: int = 50, offset: int = 0) -> list[Invoice]:
        res = await self.session.execute(
            select(Invoice)
            .order_by(Invoice.created_at.desc(), Invoice.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(res.scalars().all())

    async def count(self) -> int:
        res = await self.session.execute(select(func.count(Invoice.id)))
        return int(res.scalar_one())

    async def get_by_external_invoice_id_for_update(self, provider: str, external_invoice_id: str) -> Invoice | None:
        res = await self.session.execute(
            select(Invoice)
            .where(
                Invoice.provider == provider,
                Invoice.external_invoice_id == external_invoice_id,
            )
            .with_for_update()
        )
        return res.scalar_one_or_none()

    async def list_pending_by_provider(self, provider: str, *, limit: int = 100) -> list[Invoice]:
        res = await self.session.execute(
            select(Invoice)
            .where(
                Invoice.provider == provider,
                Invoice.status == InvoiceStatus.pending,
                Invoice.external_invoice_id.is_not(None),
            )
            .order_by(Invoice.created_at.asc(), Invoice.id.asc())
            .limit(limit)
        )
        return list(res.scalars().all())


class TransactionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, user_id: int, amount: Decimal, tx_type: TransactionType, description: str) -> Transaction:
        transaction = Transaction(
            user_id=user_id,
            amount=_money(amount),
            type=tx_type,
            description=description,
        )
        self.session.add(transaction)
        await self.session.flush()
        return transaction


class ReferralRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_if_not_exists(self, inviter_id: int, invited_id: int, source: ReferralSource) -> Referral | None:
        if inviter_id == invited_id:
            return None

        res = await self.session.execute(select(Referral).where(Referral.invited_id == invited_id))
        existing = res.scalar_one_or_none()
        if existing:
            return existing

        try:
            async with self.session.begin_nested():
                referral = Referral(
                    inviter_id=inviter_id,
                    invited_id=invited_id,
                    source=source,
                    is_activated=False,
                )
                self.session.add(referral)
                await self.session.flush()
                return referral
        except IntegrityError:
            res = await self.session.execute(select(Referral).where(Referral.invited_id == invited_id))
            return res.scalar_one_or_none()

    async def get_by_invited_id_for_update(self, invited_id: int) -> Referral | None:
        res = await self.session.execute(
            select(Referral).where(Referral.invited_id == invited_id).with_for_update()
        )
        return res.scalar_one_or_none()

    async def count_for_inviter(self, inviter_id: int) -> int:
        res = await self.session.execute(select(func.count(Referral.id)).where(Referral.inviter_id == inviter_id))
        return int(res.scalar_one())

    async def exists_for_invited(self, invited_id: int) -> bool:
        res = await self.session.execute(select(func.count(Referral.id)).where(Referral.invited_id == invited_id))
        return int(res.scalar_one()) > 0


class PromoRepository:
    STATUS_ALL = 'all'
    STATUS_ACTIVE = 'active'
    STATUS_ARCHIVED = 'archived'

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _normalize_code(code: str | None) -> str:
        return (code or '').strip().upper()

    @staticmethod
    def _normalize_bonus_amount(value: Decimal | int | float | str) -> Decimal:
        amount = _money(value)
        if amount <= 0:
            raise ValueError('Бонусная сумма должна быть больше 0.')
        return amount

    @staticmethod
    def _normalize_max_uses(value: int | None) -> int | None:
        if value is None:
            return None
        normalized = int(value)
        if normalized < 1:
            raise ValueError('Лимит использований должен быть не меньше 1.')
        return normalized

    @classmethod
    def _normalize_status_filter(cls, value: str | None) -> str:
        normalized = (value or cls.STATUS_ALL).strip().lower()
        if normalized in {cls.STATUS_ACTIVE, 'enabled'}:
            return cls.STATUS_ACTIVE
        if normalized in {cls.STATUS_ARCHIVED, 'inactive', 'disabled'}:
            return cls.STATUS_ARCHIVED
        return cls.STATUS_ALL

    @classmethod
    def _apply_filters(
        cls,
        stmt,
        *,
        status_filter: str | None = None,
        query: str | None = None,
    ):
        normalized_status = cls._normalize_status_filter(status_filter)
        normalized_query = cls._normalize_code(query)

        if normalized_status == cls.STATUS_ACTIVE:
            stmt = stmt.where(PromoCode.is_active.is_(True))
        elif normalized_status == cls.STATUS_ARCHIVED:
            stmt = stmt.where(PromoCode.is_active.is_(False))

        if normalized_query:
            like_pattern = f'%{normalized_query}%'
            stmt = stmt.where(PromoCode.code.ilike(like_pattern))

        return stmt

    async def get_by_id(self, promo_id: int) -> PromoCode | None:
        res = await self.session.execute(select(PromoCode).where(PromoCode.id == promo_id))
        return res.scalar_one_or_none()

    async def get_by_id_for_update(self, promo_id: int) -> PromoCode | None:
        res = await self.session.execute(
            select(PromoCode).where(PromoCode.id == promo_id).with_for_update()
        )
        return res.scalar_one_or_none()

    async def get_by_code_for_update(self, code: str) -> PromoCode | None:
        normalized_code = self._normalize_code(code)
        if not normalized_code:
            return None
        res = await self.session.execute(
            select(PromoCode).where(PromoCode.code == normalized_code).with_for_update()
        )
        return res.scalar_one_or_none()

    async def get_by_code(self, code: str) -> PromoCode | None:
        normalized_code = self._normalize_code(code)
        if not normalized_code:
            return None
        res = await self.session.execute(select(PromoCode).where(PromoCode.code == normalized_code))
        return res.scalar_one_or_none()

    async def list_all(self) -> list[PromoCode]:
        res = await self.session.execute(
            select(PromoCode).order_by(PromoCode.is_active.desc(), PromoCode.created_at.desc(), PromoCode.id.desc())
        )
        return list(res.scalars().all())

    async def list_recent(
        self,
        limit: int,
        offset: int = 0,
        *,
        status_filter: str | None = None,
        query: str | None = None,
    ) -> list[PromoCode]:
        stmt = select(PromoCode)
        stmt = self._apply_filters(stmt, status_filter=status_filter, query=query)
        stmt = stmt.order_by(PromoCode.is_active.desc(), PromoCode.created_at.desc(), PromoCode.id.desc())
        stmt = stmt.offset(offset).limit(limit)
        res = await self.session.execute(stmt)
        return list(res.scalars().all())

    async def count(
        self,
        *,
        status_filter: str | None = None,
        query: str | None = None,
    ) -> int:
        stmt = select(func.count(PromoCode.id))
        stmt = self._apply_filters(stmt, status_filter=status_filter, query=query)
        res = await self.session.execute(stmt)
        return int(res.scalar_one())

    async def create(
        self,
        *,
        code: str,
        bonus_amount: Decimal,
        max_uses: int | None,
        expires_at: datetime | None,
        created_by_tg_id: int | None,
    ) -> PromoCode:
        normalized_code = self._normalize_code(code)
        if not normalized_code:
            raise ValueError('Код промокода не может быть пустым.')

        if await self.get_by_code(normalized_code):
            raise ValueError('Промокод с таким кодом уже существует.')

        promo = PromoCode(
            code=normalized_code,
            bonus_amount=self._normalize_bonus_amount(bonus_amount),
            max_uses=self._normalize_max_uses(max_uses),
            expires_at=_normalize_utc_datetime(expires_at),
            is_active=True,
            created_by_tg_id=created_by_tg_id,
        )
        self.session.add(promo)
        await self.session.flush()
        return promo

    async def update(
        self,
        promo: PromoCode,
        *,
        code: str,
        bonus_amount: Decimal,
        max_uses: int | None,
        expires_at: datetime | None,
        is_active: bool | None = None,
    ) -> PromoCode:
        normalized_code = self._normalize_code(code)
        if not normalized_code:
            raise ValueError('Код промокода не может быть пустым.')

        existing = await self.get_by_code(normalized_code)
        if existing is not None and existing.id != promo.id:
            raise ValueError('Промокод с таким кодом уже существует.')

        promo.code = normalized_code
        promo.bonus_amount = self._normalize_bonus_amount(bonus_amount)
        promo.max_uses = self._normalize_max_uses(max_uses)
        promo.expires_at = _normalize_utc_datetime(expires_at)
        if is_active is not None:
            promo.is_active = bool(is_active)
        await self.session.flush()
        return promo

    async def set_active(self, promo: PromoCode, is_active: bool) -> PromoCode:
        promo.is_active = bool(is_active)
        await self.session.flush()
        return promo

    async def delete_by_id(self, promo_id: int) -> bool:
        promo = await self.get_by_id_for_update(promo_id)
        if promo is None:
            return False
        if int(promo.used_count or 0) > 0:
            raise ValueError('Нельзя удалить промокод, который уже использовался. Его можно только архивировать.')
        await self.session.delete(promo)
        await self.session.flush()
        return True

    async def delete(self, code: str) -> bool:
        promo = await self.get_by_code_for_update(code)
        if not promo:
            return False
        if int(promo.used_count or 0) > 0:
            raise ValueError('Нельзя удалить промокод, который уже использовался. Его можно только архивировать.')
        await self.session.delete(promo)
        await self.session.flush()
        return True


class PromoRedemptionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def has_redeemed(self, promo_id: int, user_id: int) -> bool:
        res = await self.session.execute(
            select(func.count(PromoRedemption.id)).where(
                PromoRedemption.promo_id == promo_id,
                PromoRedemption.user_id == user_id,
            )
        )
        return int(res.scalar_one()) > 0

    async def create(self, promo_id: int, user_id: int) -> PromoRedemption:
        try:
            async with self.session.begin_nested():
                row = PromoRedemption(promo_id=promo_id, user_id=user_id)
                self.session.add(row)
                await self.session.flush()
                return row
        except IntegrityError:
            res = await self.session.execute(
                select(PromoRedemption).where(
                    PromoRedemption.promo_id == promo_id,
                    PromoRedemption.user_id == user_id,
                )
            )
            existing = res.scalar_one_or_none()
            if existing is None:
                raise
            return existing


class SupportTicketRepository:
    ACTIVE_STATUSES: tuple[SupportTicketStatus, ...] = (
        SupportTicketStatus.waiting_operator,
        SupportTicketStatus.waiting_user,
    )

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _normalize_search_query(query: str | None) -> str:
        return (query or '').strip().lstrip('@')

    @staticmethod
    def _normalize_last_actor_type(value: SupportSenderType | str | None) -> str | None:
        if value is None:
            return None
        if isinstance(value, SupportSenderType):
            return value.value
        normalized = value.strip().lower()
        return normalized or None

    @staticmethod
    def _last_message_subquery():
        return (
            select(
                SupportMessage.ticket_id.label('ticket_id'),
                func.max(SupportMessage.created_at).label('last_message_at'),
            )
            .group_by(SupportMessage.ticket_id)
            .subquery()
        )

    @staticmethod
    def _admin_reply_subquery():
        return (
            select(
                SupportMessage.ticket_id.label('ticket_id'),
                func.max(
                    case(
                        (SupportMessage.sender_type == SupportSenderType.admin, 1),
                        else_=0,
                    )
                ).label('has_admin_reply_int'),
            )
            .group_by(SupportMessage.ticket_id)
            .subquery()
        )

    def _apply_admin_filters(self, stmt, *, query: str | None, status: SupportTicketStatus | None):
        normalized_query = self._normalize_search_query(query)

        if status is not None:
            stmt = stmt.where(SupportTicket.status == status)

        if not normalized_query:
            return stmt

        if normalized_query.isdigit():
            numeric_value = int(normalized_query)
            stmt = stmt.where(
                or_(
                    SupportTicket.id == numeric_value,
                    User.tg_id == numeric_value,
                )
            )
            return stmt

        pattern = f'%{normalized_query.lower()}%'
        return stmt.where(
            or_(
                func.lower(func.coalesce(User.username, '')).like(pattern),
                func.lower(func.coalesce(User.first_name, '')).like(pattern),
                func.lower(func.coalesce(User.last_name, '')).like(pattern),
            )
        )

    async def list_by_user(self, user_id: int) -> list[SupportTicket]:
        res = await self.session.execute(
            select(SupportTicket)
            .where(SupportTicket.user_id == user_id)
            .order_by(SupportTicket.created_at.desc(), SupportTicket.id.desc())
        )
        return list(res.scalars().all())

    async def list_all(self) -> list[SupportTicket]:
        res = await self.session.execute(
            select(SupportTicket).order_by(SupportTicket.created_at.desc(), SupportTicket.id.desc())
        )
        return list(res.scalars().all())

    async def list_recent(self, limit: int, offset: int = 0) -> list[SupportTicket]:
        res = await self.session.execute(
            select(SupportTicket)
            .order_by(SupportTicket.created_at.desc(), SupportTicket.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(res.scalars().all())

    async def count(self) -> int:
        res = await self.session.execute(select(func.count(SupportTicket.id)))
        return int(res.scalar_one())

    async def count_for_admin(
        self,
        *,
        query: str | None = None,
        status: SupportTicketStatus | None = None,
    ) -> int:
        stmt = (
            select(func.count(func.distinct(SupportTicket.id)))
            .select_from(SupportTicket)
            .join(User, User.id == SupportTicket.user_id)
        )
        stmt = self._apply_admin_filters(stmt, query=query, status=status)
        res = await self.session.execute(stmt)
        return int(res.scalar_one())

    async def list_for_admin(
        self,
        *,
        query: str | None = None,
        status: SupportTicketStatus | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[SupportTicket]:
        rows = await self.list_for_admin_with_meta(
            query=query,
            status=status,
            limit=limit,
            offset=offset,
        )
        return [row[0] for row in rows]

    async def list_for_admin_with_meta(
        self,
        *,
        query: str | None = None,
        status: SupportTicketStatus | None = None,
        limit: int = 20,
        offset: int = 0,
        unanswered_first: bool = False,
    ) -> list[tuple[SupportTicket, datetime | None, bool]]:
        last_message_subq = self._last_message_subquery()
        admin_reply_subq = self._admin_reply_subquery()

        stmt = (
            select(
                SupportTicket,
                last_message_subq.c.last_message_at,
                admin_reply_subq.c.has_admin_reply_int,
            )
            .join(User, User.id == SupportTicket.user_id)
            .outerjoin(last_message_subq, last_message_subq.c.ticket_id == SupportTicket.id)
            .outerjoin(admin_reply_subq, admin_reply_subq.c.ticket_id == SupportTicket.id)
        )
        stmt = self._apply_admin_filters(stmt, query=query, status=status)

        sort_last_activity = func.coalesce(last_message_subq.c.last_message_at, SupportTicket.created_at)
        waiting_priority = case(
            (SupportTicket.status == SupportTicketStatus.waiting_operator, 0),
            (SupportTicket.status == SupportTicketStatus.waiting_user, 1),
            (SupportTicket.status == SupportTicketStatus.closed, 2),
            else_=3,
        )

        if unanswered_first:
            stmt = stmt.order_by(
                waiting_priority.asc(),
                sort_last_activity.asc(),
                SupportTicket.id.asc(),
            )
        else:
            stmt = stmt.order_by(
                sort_last_activity.desc(),
                SupportTicket.id.desc(),
            )

        stmt = stmt.offset(offset).limit(limit)
        res = await self.session.execute(stmt)

        items: list[tuple[SupportTicket, datetime | None, bool]] = []
        for ticket, last_message_at, has_admin_reply_int in res.all():
            items.append(
                (
                    ticket,
                    last_message_at,
                    bool(has_admin_reply_int or 0),
                )
            )
        return items

    async def get_by_id(self, ticket_id: int) -> SupportTicket | None:
        res = await self.session.execute(select(SupportTicket).where(SupportTicket.id == ticket_id))
        return res.scalar_one_or_none()

    async def get_by_id_for_update(self, ticket_id: int) -> SupportTicket | None:
        res = await self.session.execute(
            select(SupportTicket).where(SupportTicket.id == ticket_id).with_for_update()
        )
        return res.scalar_one_or_none()

    async def get_active_by_user(self, user_id: int) -> SupportTicket | None:
        res = await self.session.execute(
            select(SupportTicket).where(
                SupportTicket.user_id == user_id,
                SupportTicket.status.in_(self.ACTIVE_STATUSES),
            )
        )
        return res.scalar_one_or_none()

    async def get_active_by_user_for_update(self, user_id: int) -> SupportTicket | None:
        res = await self.session.execute(
            select(SupportTicket)
            .where(
                SupportTicket.user_id == user_id,
                SupportTicket.status.in_(self.ACTIVE_STATUSES),
            )
            .with_for_update()
        )
        return res.scalar_one_or_none()

    async def get_open_by_user(self, user_id: int) -> SupportTicket | None:
        return await self.get_active_by_user(user_id)

    async def get_open_by_user_for_update(self, user_id: int) -> SupportTicket | None:
        return await self.get_active_by_user_for_update(user_id)

    async def get_last_message_timestamp(self, ticket_id: int) -> datetime | None:
        res = await self.session.execute(
            select(func.max(SupportMessage.created_at)).where(SupportMessage.ticket_id == ticket_id)
        )
        return res.scalar_one_or_none()

    async def has_admin_reply(self, ticket_id: int) -> bool:
        res = await self.session.execute(
            select(func.count(SupportMessage.id)).where(
                SupportMessage.ticket_id == ticket_id,
                SupportMessage.sender_type == SupportSenderType.admin,
            )
        )
        return int(res.scalar_one()) > 0

    async def due_auto_close(
        self,
        threshold: datetime,
        *,
        after_id: int = 0,
        limit: int = 500,
    ) -> list[SupportTicket]:
        last_message_subq = (
            select(
                SupportMessage.ticket_id,
                func.max(SupportMessage.created_at).label('last_message_at'),
            )
            .group_by(SupportMessage.ticket_id)
            .subquery()
        )

        stmt = (
            select(SupportTicket)
            .options(selectinload(SupportTicket.user))
            .outerjoin(last_message_subq, last_message_subq.c.ticket_id == SupportTicket.id)
            .where(
                SupportTicket.status.in_(self.ACTIVE_STATUSES),
                SupportTicket.id > after_id,
                func.coalesce(last_message_subq.c.last_message_at, SupportTicket.created_at) <= threshold,
            )
            .order_by(SupportTicket.id.asc())
            .limit(limit)
        )
        res = await self.session.execute(stmt)
        return list(res.scalars().all())

    async def create(
        self,
        user_id: int,
        *,
        status: SupportTicketStatus = SupportTicketStatus.waiting_operator,
        last_actor_type: SupportSenderType | str | None = SupportSenderType.user,
        last_actor_tg_id: int | None = None,
    ) -> SupportTicket:
        ticket = SupportTicket(
            user_id=user_id,
            status=status,
            closed_at=None,
            close_reason=None,
            closed_by_admin_tg_id=None,
            last_actor_type=self._normalize_last_actor_type(last_actor_type),
            last_actor_tg_id=_normalize_optional_bigint(last_actor_tg_id),
        )
        self.session.add(ticket)
        await self.session.flush()
        return ticket

    async def touch_user_reply(self, ticket: SupportTicket, *, sender_tg_id: int | None = None) -> SupportTicket:
        if ticket.status == SupportTicketStatus.closed:
            raise ValueError('Cannot update closed support ticket')
        ticket.status = SupportTicketStatus.waiting_operator
        ticket.last_actor_type = SupportSenderType.user.value
        ticket.last_actor_tg_id = _normalize_optional_bigint(sender_tg_id)
        ticket.closed_at = None
        ticket.close_reason = None
        ticket.closed_by_admin_tg_id = None
        await self.session.flush()
        return ticket

    async def touch_admin_reply(self, ticket: SupportTicket, *, sender_tg_id: int | None = None) -> SupportTicket:
        if ticket.status == SupportTicketStatus.closed:
            raise ValueError('Cannot update closed support ticket')
        ticket.status = SupportTicketStatus.waiting_user
        ticket.last_actor_type = SupportSenderType.admin.value
        ticket.last_actor_tg_id = _normalize_optional_bigint(sender_tg_id)
        ticket.closed_at = None
        ticket.close_reason = None
        await self.session.flush()
        return ticket

    async def close(
        self,
        ticket: SupportTicket,
        reason: str | None = None,
        *,
        closed_by_admin_tg_id: int | None = None,
        actor_tg_id: int | None = None,
        actor_type: SupportSenderType | str | None = None,
    ) -> bool:
        if ticket.status == SupportTicketStatus.closed:
            return False

        normalized_closed_by_admin_tg_id = _normalize_optional_bigint(closed_by_admin_tg_id)
        normalized_actor_tg_id = _normalize_optional_bigint(actor_tg_id)
        normalized_actor_type = self._normalize_last_actor_type(actor_type)
        if normalized_actor_type is None and normalized_closed_by_admin_tg_id is not None:
            normalized_actor_type = SupportSenderType.admin.value
        if normalized_actor_tg_id is None and normalized_closed_by_admin_tg_id is not None:
            normalized_actor_tg_id = normalized_closed_by_admin_tg_id

        ticket.status = SupportTicketStatus.closed
        ticket.closed_at = datetime.now(timezone.utc)
        ticket.close_reason = _normalize_optional_str(reason)
        ticket.closed_by_admin_tg_id = normalized_closed_by_admin_tg_id
        if normalized_actor_type is not None:
            ticket.last_actor_type = normalized_actor_type
        if normalized_actor_tg_id is not None:
            ticket.last_actor_tg_id = normalized_actor_tg_id
        await self.session.flush()
        return True


class BroadcastJobRepository:
    STATUS_ALL = 'all'
    STATUS_DRAFT = BroadcastJobStatus.draft.value
    STATUS_SCHEDULED = BroadcastJobStatus.scheduled.value
    STATUS_RUNNING = BroadcastJobStatus.running.value
    STATUS_COMPLETED = BroadcastJobStatus.completed.value
    STATUS_FAILED = BroadcastJobStatus.failed.value
    STATUS_CANCELLED = BroadcastJobStatus.cancelled.value
    _UNSET = object()

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @classmethod
    def _normalize_status(cls, status: BroadcastJobStatus | str | None) -> BroadcastJobStatus:
        if isinstance(status, BroadcastJobStatus):
            normalized = status.value
        else:
            normalized = (status or cls.STATUS_SCHEDULED).strip().lower()

        if normalized == 'pending':
            normalized = cls.STATUS_SCHEDULED

        try:
            return BroadcastJobStatus(normalized)
        except ValueError as exc:
            raise ValueError('Неизвестный статус рассылки.') from exc

    @classmethod
    def _normalize_status_filter(cls, value: str | None) -> str:
        normalized = (value or cls.STATUS_ALL).strip().lower()
        if normalized == 'pending':
            normalized = cls.STATUS_SCHEDULED
        if normalized in {
            cls.STATUS_DRAFT,
            cls.STATUS_SCHEDULED,
            cls.STATUS_RUNNING,
            cls.STATUS_COMPLETED,
            cls.STATUS_FAILED,
            cls.STATUS_CANCELLED,
        }:
            return normalized
        return cls.STATUS_ALL

    @staticmethod
    def _normalize_text(value: str | None) -> str | None:
        normalized = (value or '').strip()
        return normalized or None

    @staticmethod
    def _normalize_media_ref(value: str | None) -> str | None:
        normalized = (value or '').strip()
        return normalized or None

    @staticmethod
    def _normalize_media_type(value: str | None) -> str | None:
        normalized = (value or '').strip().lower()
        if not normalized:
            return None
        if normalized != 'photo':
            raise ValueError('Поддерживается только один тип медиа: photo.')
        return normalized

    @staticmethod
    def _normalize_payload_json(value: dict | None) -> dict:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError('payload_json должен быть объектом JSON.')
        return dict(value)

    @staticmethod
    def _normalize_keyboard_json(value: list | tuple | None) -> list[list[dict[str, object]]]:
        if value is None:
            return []
        if not isinstance(value, (list, tuple)):
            raise ValueError('Клавиатура должна быть списком рядов.')

        normalized_rows: list[list[dict[str, object]]] = []
        for row in value:
            if row is None:
                continue
            if not isinstance(row, (list, tuple)):
                raise ValueError('Каждый ряд клавиатуры должен быть списком кнопок.')

            normalized_buttons: list[dict[str, object]] = []
            for button in row:
                if not isinstance(button, dict):
                    raise ValueError('Каждая кнопка должна быть объектом.')

                text = str(button.get('text', '') or '').strip()
                if not text:
                    raise ValueError('У каждой inline-кнопки должен быть текст.')

                normalized_button: dict[str, object] = {'text': text}

                url = button.get('url')
                callback_data = button.get('callback_data')
                if url is not None:
                    normalized_url = str(url).strip()
                    if not normalized_url:
                        raise ValueError('URL кнопки не может быть пустым.')
                    normalized_button['url'] = normalized_url
                if callback_data is not None:
                    normalized_callback = str(callback_data).strip()
                    if not normalized_callback:
                        raise ValueError('callback_data кнопки не может быть пустым.')
                    normalized_button['callback_data'] = normalized_callback

                if 'url' not in normalized_button and 'callback_data' not in normalized_button:
                    raise ValueError('У inline-кнопки должен быть url или callback_data.')

                for passthrough_key in (
                    'switch_inline_query',
                    'switch_inline_query_current_chat',
                    'switch_inline_query_chosen_chat',
                    'web_app',
                    'pay',
                    'copy_text',
                ):
                    if passthrough_key in button:
                        normalized_button[passthrough_key] = button[passthrough_key]

                normalized_buttons.append(normalized_button)

            if normalized_buttons:
                normalized_rows.append(normalized_buttons)

        return normalized_rows

    @classmethod
    def _apply_status_filter(cls, stmt, *, status_filter: str | None = None):
        normalized_status = cls._normalize_status_filter(status_filter)
        if normalized_status == cls.STATUS_ALL:
            return stmt
        return stmt.where(BroadcastJob.status == BroadcastJobStatus(normalized_status))

    @classmethod
    def _apply_content(
        cls,
        job: BroadcastJob,
        *,
        text: str | None,
        payload_json: dict | None,
        photo_file_id: str | None,
        photo_file_unique_id: str | None,
        media_type: str | None,
        keyboard_json: list | tuple | None,
    ) -> BroadcastJob:
        normalized_text = cls._normalize_text(text)
        normalized_photo_file_id = cls._normalize_media_ref(photo_file_id)
        normalized_photo_file_unique_id = cls._normalize_media_ref(photo_file_unique_id)
        normalized_media_type = cls._normalize_media_type(media_type)
        normalized_keyboard = cls._normalize_keyboard_json(keyboard_json)
        normalized_payload = cls._normalize_payload_json(payload_json)

        if normalized_media_type is None and normalized_photo_file_id is not None:
            normalized_media_type = 'photo'
        if normalized_photo_file_id is None:
            normalized_photo_file_unique_id = None
            normalized_media_type = None

        if normalized_text is None and normalized_photo_file_id is None:
            raise ValueError('У рассылки должен быть текст или фото.')

        normalized_payload.update(
            {
                'text': normalized_text,
                'photo_file_id': normalized_photo_file_id,
                'photo_file_unique_id': normalized_photo_file_unique_id,
                'media_type': normalized_media_type,
                'keyboard': normalized_keyboard,
            }
        )

        job.text = normalized_text
        job.payload_json = normalized_payload
        job.photo_file_id = normalized_photo_file_id
        job.photo_file_unique_id = normalized_photo_file_unique_id
        job.media_type = normalized_media_type
        job.keyboard_json = normalized_keyboard
        return job

    async def create(
        self,
        *,
        created_by_tg_id: int,
        text: str | None,
        run_at: datetime,
        status: BroadcastJobStatus | str = BroadcastJobStatus.scheduled,
        payload_json: dict | None = None,
        photo_file_id: str | None = None,
        photo_file_unique_id: str | None = None,
        media_type: str | None = None,
        keyboard_json: list | tuple | None = None,
    ) -> BroadcastJob:
        normalized_run_at = _normalize_utc_datetime(run_at)
        if normalized_run_at is None:
            raise ValueError('Время запуска рассылки обязательно.')

        job = BroadcastJob(
            created_by_tg_id=int(created_by_tg_id),
            run_at=normalized_run_at,
            status=self._normalize_status(status),
        )
        self._apply_content(
            job,
            text=text,
            payload_json=payload_json,
            photo_file_id=photo_file_id,
            photo_file_unique_id=photo_file_unique_id,
            media_type=media_type,
            keyboard_json=keyboard_json,
        )
        self.session.add(job)
        await self.session.flush()
        return job

    async def get_by_id(self, job_id: int) -> BroadcastJob | None:
        res = await self.session.execute(select(BroadcastJob).where(BroadcastJob.id == job_id))
        return res.scalar_one_or_none()

    async def get_by_id_for_update(self, job_id: int) -> BroadcastJob | None:
        res = await self.session.execute(
            select(BroadcastJob).where(BroadcastJob.id == job_id).with_for_update()
        )
        return res.scalar_one_or_none()

    async def list_pending(self, *, limit: int = 50) -> list[BroadcastJob]:
        res = await self.session.execute(
            select(BroadcastJob)
            .where(BroadcastJob.status == BroadcastJobStatus.scheduled)
            .order_by(BroadcastJob.run_at.asc(), BroadcastJob.id.asc())
            .limit(limit)
        )
        return list(res.scalars().all())

    async def list_recent(
        self,
        *,
        limit: int = 10,
        offset: int = 0,
        status_filter: str | None = None,
    ) -> list[BroadcastJob]:
        stmt = select(BroadcastJob)
        stmt = self._apply_status_filter(stmt, status_filter=status_filter)
        stmt = stmt.order_by(BroadcastJob.run_at.asc(), BroadcastJob.id.asc()).offset(offset).limit(limit)
        res = await self.session.execute(stmt)
        return list(res.scalars().all())

    async def count(self, *, status_filter: str | None = None) -> int:
        stmt = select(func.count(BroadcastJob.id))
        stmt = self._apply_status_filter(stmt, status_filter=status_filter)
        res = await self.session.execute(stmt)
        return int(res.scalar_one())

    async def count_active(self) -> int:
        res = await self.session.execute(
            select(func.count(BroadcastJob.id)).where(
                BroadcastJob.status.in_([BroadcastJobStatus.scheduled, BroadcastJobStatus.running])
            )
        )
        return int(res.scalar_one())

    async def update_text(self, job: BroadcastJob, text: str) -> BroadcastJob:
        self._apply_content(
            job,
            text=text,
            payload_json=job.payload_json,
            photo_file_id=job.photo_file_id,
            photo_file_unique_id=job.photo_file_unique_id,
            media_type=job.media_type,
            keyboard_json=job.keyboard_json,
        )
        await self.session.flush()
        return job

    async def update_content(
        self,
        job: BroadcastJob,
        *,
        text: str | None | object = _UNSET,
        payload_json: dict | None | object = _UNSET,
        photo_file_id: str | None | object = _UNSET,
        photo_file_unique_id: str | None | object = _UNSET,
        media_type: str | None | object = _UNSET,
        keyboard_json: list | tuple | None | object = _UNSET,
    ) -> BroadcastJob:
        resolved_text = job.text if text is self._UNSET else text
        resolved_payload = job.payload_json if payload_json is self._UNSET else payload_json
        resolved_photo_file_id = job.photo_file_id if photo_file_id is self._UNSET else photo_file_id
        resolved_photo_file_unique_id = (
            job.photo_file_unique_id if photo_file_unique_id is self._UNSET else photo_file_unique_id
        )
        resolved_media_type = job.media_type if media_type is self._UNSET else media_type
        resolved_keyboard = job.keyboard_json if keyboard_json is self._UNSET else keyboard_json

        self._apply_content(
            job,
            text=resolved_text,
            payload_json=resolved_payload,
            photo_file_id=resolved_photo_file_id,
            photo_file_unique_id=resolved_photo_file_unique_id,
            media_type=resolved_media_type,
            keyboard_json=resolved_keyboard,
        )
        await self.session.flush()
        return job

    async def update_run_at(self, job: BroadcastJob, run_at: datetime) -> BroadcastJob:
        normalized_run_at = _normalize_utc_datetime(run_at)
        if normalized_run_at is None:
            raise ValueError('Время запуска рассылки обязательно.')
        job.run_at = normalized_run_at
        await self.session.flush()
        return job

    async def delete(self, job: BroadcastJob) -> None:
        await self.session.delete(job)

    async def claim_due_for_processing(self, *, now: datetime | None = None) -> BroadcastJob | None:
        due_at = _normalize_utc_datetime(now) or datetime.now(timezone.utc)
        res = await self.session.execute(
            select(BroadcastJob)
            .where(
                BroadcastJob.status == BroadcastJobStatus.scheduled,
                BroadcastJob.run_at <= due_at,
            )
            .order_by(BroadcastJob.run_at.asc(), BroadcastJob.id.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        job = res.scalar_one_or_none()
        if job is None:
            return None

        now_dt = datetime.now(timezone.utc)
        job.status = BroadcastJobStatus.running
        job.started_at = now_dt
        job.finished_at = None
        job.last_error = None
        job.total_users = 0
        job.processed_users = 0
        job.sent_count = 0
        job.failed_count = 0
        job.last_user_id = None
        return job

    async def mark_running(self, job: BroadcastJob) -> BroadcastJob:
        job.status = BroadcastJobStatus.running
        job.started_at = datetime.now(timezone.utc)
        job.finished_at = None
        job.last_error = None
        return job

    async def request_cancel(
        self,
        job: BroadcastJob,
        *,
        cancelled_by_tg_id: int | None = None,
        error: str | None = None,
    ) -> BroadcastJob:
        now_dt = datetime.now(timezone.utc)
        job.cancel_requested_at = now_dt
        if cancelled_by_tg_id is not None:
            job.cancelled_by_tg_id = int(cancelled_by_tg_id)

        normalized_error = self._normalize_text(error)

        if job.status in {BroadcastJobStatus.draft, BroadcastJobStatus.scheduled}:
            job.status = BroadcastJobStatus.cancelled
            job.finished_at = now_dt
            if normalized_error is not None:
                job.last_error = normalized_error
        elif job.status == BroadcastJobStatus.running and normalized_error is not None:
            job.last_error = normalized_error

        await self.session.flush()
        return job

    async def cancel(
        self,
        job: BroadcastJob,
        *,
        error: str | None = None,
        cancelled_by_tg_id: int | None = None,
    ) -> None:
        job.status = BroadcastJobStatus.cancelled
        now_dt = datetime.now(timezone.utc)
        job.cancel_requested_at = job.cancel_requested_at or now_dt
        if cancelled_by_tg_id is not None:
            job.cancelled_by_tg_id = int(cancelled_by_tg_id)
        job.finished_at = now_dt
        normalized_error = self._normalize_text(error)
        if normalized_error:
            job.last_error = normalized_error

    async def fail(self, job: BroadcastJob, *, error: str) -> None:
        job.status = BroadcastJobStatus.failed
        job.finished_at = datetime.now(timezone.utc)
        job.last_error = self._normalize_text(error) or 'unknown_error'

    async def advance(
        self,
        job: BroadcastJob,
        *,
        processed_inc: int = 0,
        sent_inc: int = 0,
        failed_inc: int = 0,
    ) -> None:
        job.processed_users = max(0, int(job.processed_users or 0) + max(0, int(processed_inc)))
        job.sent_count = max(0, int(job.sent_count or 0) + max(0, int(sent_inc)))
        job.failed_count = max(0, int(job.failed_count or 0) + max(0, int(failed_inc)))

    async def complete(self, job: BroadcastJob) -> None:
        job.status = BroadcastJobStatus.completed
        job.finished_at = datetime.now(timezone.utc)
        job.last_error = None


class BroadcastJobDeliveryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_job_and_user(self, *, job_id: int, user_id: int) -> BroadcastJobDelivery | None:
        res = await self.session.execute(
            select(BroadcastJobDelivery).where(
                BroadcastJobDelivery.job_id == job_id,
                BroadcastJobDelivery.user_id == user_id,
            )
        )
        return res.scalar_one_or_none()

    async def upsert_result(
        self,
        *,
        job_id: int,
        user: User,
        status: BroadcastDeliveryStatus,
        attempt_count: int,
        last_error: str | None = None,
        telegram_message_id: int | None = None,
        delivered_at: datetime | None = None,
    ) -> BroadcastJobDelivery:
        row = await self.get_by_job_and_user(job_id=job_id, user_id=user.id)
        if row is None:
            row = BroadcastJobDelivery(
                job_id=job_id,
                user_id=user.id,
                user_tg_id=user.tg_id,
                status=status,
                attempt_count=attempt_count,
                telegram_message_id=telegram_message_id,
                delivered_at=delivered_at,
                last_error=last_error,
            )
            self.session.add(row)
        else:
            row.user_tg_id = user.tg_id
            row.status = status
            row.attempt_count = attempt_count
            row.telegram_message_id = telegram_message_id
            row.delivered_at = delivered_at
            row.last_error = last_error

        await self.session.flush()
        return row

    async def list_recent_for_job(self, job_id: int, *, limit: int = 20) -> list[BroadcastJobDelivery]:
        res = await self.session.execute(
            select(BroadcastJobDelivery)
            .where(BroadcastJobDelivery.job_id == job_id)
            .order_by(BroadcastJobDelivery.created_at.desc(), BroadcastJobDelivery.id.desc())
            .limit(limit)
        )
        return list(res.scalars().all())


class SupportMessageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_by_ticket(self, ticket_id: int) -> list[SupportMessage]:
        res = await self.session.execute(
            select(SupportMessage)
            .where(SupportMessage.ticket_id == ticket_id)
            .order_by(SupportMessage.created_at.asc(), SupportMessage.id.asc())
        )
        return list(res.scalars().all())

    async def get_by_admin_chat_message_id(self, admin_chat_message_id: int) -> SupportMessage | None:
        res = await self.session.execute(
            select(SupportMessage).where(SupportMessage.admin_chat_message_id == admin_chat_message_id)
        )
        return res.scalar_one_or_none()

    async def create(
        self,
        *,
        ticket_id: int,
        sender_type: SupportSenderType,
        sender_tg_id: int,
        text: str | None,
        media_type: str | None,
        media_file_id: str | None,
        admin_chat_message_id: int | None,
        media_file_unique_id: str | None = None,
        media_file_name: str | None = None,
        media_mime_type: str | None = None,
        media_size_bytes: int | None = None,
    ) -> SupportMessage:
        row = SupportMessage(
            ticket_id=ticket_id,
            sender_type=sender_type,
            sender_tg_id=sender_tg_id,
            text=_normalize_optional_str(text),
            media_type=_normalize_optional_str(media_type),
            media_file_id=_normalize_optional_str(media_file_id),
            admin_chat_message_id=_normalize_optional_bigint(admin_chat_message_id),
            media_file_unique_id=_normalize_optional_str(media_file_unique_id),
            media_file_name=_normalize_optional_str(media_file_name),
            media_mime_type=_normalize_optional_str(media_mime_type),
            media_size_bytes=_normalize_non_negative_bigint(media_size_bytes),
        )
        self.session.add(row)
        await self.session.flush()
        return row


class AuditLogRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        action: AuditAction,
        actor_type: AuditActorType,
        actor_tg_id: int | None,
        entity_type: str,
        entity_id: str,
        details: dict | None = None,
    ) -> AuditLog:
        row = AuditLog(
            action=action,
            actor_type=actor_type,
            actor_tg_id=actor_tg_id,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details or {},
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def list_recent(self, *, limit: int = 50, offset: int = 0) -> list[AuditLog]:
        res = await self.session.execute(
            select(AuditLog)
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(res.scalars().all())

    async def count(self) -> int:
        res = await self.session.execute(select(func.count(AuditLog.id)))
        return int(res.scalar_one())


class AppLinkRepository:
    DEFAULT_OS_NAMES = ('iOS', 'Android', 'macOS', 'Windows', 'Linux', 'AndroidTV')

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, link_id: int) -> AppLink | None:
        res = await self.session.execute(select(AppLink).where(AppLink.id == link_id))
        return res.scalar_one_or_none()

    async def get_by_os_name(self, os_name: str) -> AppLink | None:
        res = await self.session.execute(
            select(AppLink).where(func.lower(AppLink.os_name) == os_name.lower())
        )
        return res.scalar_one_or_none()

    async def list_all(self) -> list[AppLink]:
        res = await self.session.execute(select(AppLink).order_by(AppLink.os_name.asc()))
        return list(res.scalars().all())

    async def ensure_defaults(self, os_names: tuple[str, ...] | None = None) -> list[AppLink]:
        names = os_names or self.DEFAULT_OS_NAMES
        existing = await self.list_all()
        existing_map = {row.os_name.lower(): row for row in existing}

        for name in names:
            if name.lower() in existing_map:
                continue
            row = AppLink(os_name=name, download_url=None, guide_url=None)
            self.session.add(row)

        await self.session.flush()
        return await self.list_all()

    async def update_urls(self, link: AppLink, *, download_url: str | None, guide_url: str | None) -> AppLink:
        link.download_url = (download_url or '').strip() or None
        link.guide_url = (guide_url or '').strip() or None
        await self.session.flush()
        return link


class MarzbanPageSettingsRepository:
    SINGLETON_ID = 1

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self) -> MarzbanPageSettings | None:
        res = await self.session.execute(
            select(MarzbanPageSettings).where(MarzbanPageSettings.id == self.SINGLETON_ID)
        )
        return res.scalar_one_or_none()

    async def get_for_update(self) -> MarzbanPageSettings | None:
        res = await self.session.execute(
            select(MarzbanPageSettings)
            .where(MarzbanPageSettings.id == self.SINGLETON_ID)
            .with_for_update()
        )
        return res.scalar_one_or_none()

    async def ensure(self) -> MarzbanPageSettings:
        row = await self.get()
        if row is not None:
            return row

        try:
            async with self.session.begin_nested():
                row = MarzbanPageSettings(id=self.SINGLETON_ID)
                self.session.add(row)
                await self.session.flush()
                return row
        except IntegrityError:
            row = await self.get()
            if row is not None:
                return row
            raise

    async def update(
        self,
        row: MarzbanPageSettings,
        *,
        brand_name: str,
        page_title: str,
        hero_title: str,
        hero_text: str,
        connect_button_text: str,
        connect_hint_text: str | None,
        support_text: str | None,
        platforms_title: str,
        platforms_subtitle: str | None,
        show_usage_block: bool,
        show_subscription_copy_button: bool,
        show_platform_cards: bool,
        show_primary_connect_button: bool,
        show_one_click_block: bool,
        show_hiddify_button: bool,
        show_v2raytun_button: bool,
        show_happ_button: bool,
        show_qr_button: bool,
    ) -> MarzbanPageSettings:
        row.brand_name = brand_name.strip() or '😎 SwoiVPN'
        row.page_title = page_title.strip() or '😎 SwoiVPN — Подписка'
        row.hero_title = hero_title.strip() or 'Ваша VPN-подписка'
        row.hero_text = (
            hero_text.strip()
            or 'Здесь вы можете открыть подписку, посмотреть статус услуги и перейти к инструкциям для своей платформы.'
        )
        row.connect_button_text = connect_button_text.strip() or 'Подключить в 1 клик'
        row.connect_hint_text = _normalize_optional_str(connect_hint_text)
        row.support_text = _normalize_optional_str(support_text)
        row.platforms_title = platforms_title.strip() or 'Платформы подключения'
        row.platforms_subtitle = _normalize_optional_str(platforms_subtitle)
        row.show_usage_block = bool(show_usage_block)
        row.show_subscription_copy_button = bool(show_subscription_copy_button)
        row.show_platform_cards = bool(show_platform_cards)
        row.show_primary_connect_button = bool(show_primary_connect_button)
        row.show_one_click_block = bool(show_one_click_block)
        row.show_hiddify_button = bool(show_hiddify_button)
        row.show_v2raytun_button = bool(show_v2raytun_button)
        row.show_happ_button = bool(show_happ_button)
        row.show_qr_button = bool(show_qr_button)
        await self.session.flush()
        return row


class NotificationRuleRepository:
    """Хранилище правил push-уведомлений (FEA-NOTIF).

    Используется `NotificationDispatcher` для резолва текста/кнопок/cooldown
    по `code`, и admin-UI — для редактирования. Правила seed-ятся миграцией;
    отсутствующее правило означает «использовать вшитый fallback в коде».
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_code(self, code: str) -> NotificationRule | None:
        res = await self.session.execute(
            select(NotificationRule).where(NotificationRule.code == code)
        )
        return res.scalar_one_or_none()

    async def list_all(self) -> list[NotificationRule]:
        res = await self.session.execute(
            select(NotificationRule).order_by(
                NotificationRule.priority.asc(), NotificationRule.code.asc()
            )
        )
        return list(res.scalars().all())

    async def update_rule(
        self,
        rule: NotificationRule,
        *,
        is_enabled: bool | None = None,
        template_text: str | None = None,
        template_keyboard_json: list[Any] | None = None,
        cooldown_seconds: int | None = None,
        segment_filter_json: dict[str, Any] | None = None,
        priority: int | None = None,
        description: str | None = None,
        clear_keyboard: bool = False,
        clear_segment_filter: bool = False,
    ) -> NotificationRule:
        if is_enabled is not None:
            rule.is_enabled = bool(is_enabled)
        if template_text is not None:
            text = template_text.strip()
            if not text:
                raise ValueError('template_text must be non-empty')
            rule.template_text = text
        if clear_keyboard:
            rule.template_keyboard_json = None
        elif template_keyboard_json is not None:
            rule.template_keyboard_json = template_keyboard_json
        if cooldown_seconds is not None:
            if cooldown_seconds < 0:
                raise ValueError('cooldown_seconds must be >= 0')
            rule.cooldown_seconds = int(cooldown_seconds)
        if clear_segment_filter:
            rule.segment_filter_json = None
        elif segment_filter_json is not None:
            rule.segment_filter_json = segment_filter_json
        if priority is not None:
            rule.priority = int(priority)
        if description is not None:
            rule.description = _normalize_optional_str(description)
        await self.session.flush()
        return rule


class TrafficTopupOptionRepository:
    """Хранилище опций «докупки» трафика (FEA-A8).

    Используется `PricingService` для резолва доступных пакетов и admin-UI
    для CRUD. Сортировка — по `sort_order ASC, id ASC`.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_all(self) -> list[TrafficTopupOption]:
        res = await self.session.execute(
            select(TrafficTopupOption).order_by(
                TrafficTopupOption.sort_order.asc(),
                TrafficTopupOption.id.asc(),
            )
        )
        return list(res.scalars().all())

    async def list_enabled(self) -> list[TrafficTopupOption]:
        res = await self.session.execute(
            select(TrafficTopupOption)
            .where(TrafficTopupOption.is_enabled.is_(True))
            .order_by(
                TrafficTopupOption.sort_order.asc(),
                TrafficTopupOption.id.asc(),
            )
        )
        return list(res.scalars().all())

    async def get_by_code(self, code: str) -> TrafficTopupOption | None:
        res = await self.session.execute(
            select(TrafficTopupOption).where(TrafficTopupOption.code == code)
        )
        return res.scalar_one_or_none()

    async def get_by_id(self, option_id: int) -> TrafficTopupOption | None:
        res = await self.session.execute(
            select(TrafficTopupOption).where(TrafficTopupOption.id == option_id)
        )
        return res.scalar_one_or_none()

    async def create(
        self,
        *,
        code: str,
        title: str,
        extra_traffic_gb: int,
        amount: Decimal | int | float | str,
        is_enabled: bool = True,
        sort_order: int = 100,
        badge_label: str | None = None,
    ) -> TrafficTopupOption:
        if not code or not code.strip():
            raise ValueError('code не может быть пустым')
        if int(extra_traffic_gb) <= 0:
            raise ValueError('extra_traffic_gb должен быть > 0')
        amount_value = _money(amount)
        if amount_value < 0:
            raise ValueError('amount должен быть ≥ 0')
        row = TrafficTopupOption(
            code=code.strip(),
            title=title.strip(),
            extra_traffic_gb=int(extra_traffic_gb),
            amount=amount_value,
            is_enabled=bool(is_enabled),
            sort_order=int(sort_order),
            badge_label=_normalize_optional_str(badge_label),
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def update(
        self,
        option: TrafficTopupOption,
        *,
        title: str | None = None,
        extra_traffic_gb: int | None = None,
        amount: Decimal | int | float | str | None = None,
        is_enabled: bool | None = None,
        sort_order: int | None = None,
        badge_label: str | None = None,
    ) -> TrafficTopupOption:
        if title is not None:
            stripped = title.strip()
            if not stripped:
                raise ValueError('title не может быть пустым')
            option.title = stripped
        if extra_traffic_gb is not None:
            if int(extra_traffic_gb) <= 0:
                raise ValueError('extra_traffic_gb должен быть > 0')
            option.extra_traffic_gb = int(extra_traffic_gb)
        if amount is not None:
            amount_value = _money(amount)
            if amount_value < 0:
                raise ValueError('amount должен быть ≥ 0')
            option.amount = amount_value
        if is_enabled is not None:
            option.is_enabled = bool(is_enabled)
        if sort_order is not None:
            option.sort_order = int(sort_order)
        if badge_label is not None:
            option.badge_label = _normalize_optional_str(badge_label)
        await self.session.flush()
        return option

    async def delete(self, option: TrafficTopupOption) -> None:
        await self.session.delete(option)
        await self.session.flush()


class OutboxRepository:
    """Transactional outbox for guaranteed-delivery side effects (OPS-4).

    Producers call `enqueue_*` inside the same transaction that commits the
    triggering domain change (e.g. invoice → consumed). Workers call
    `claim_due` to lock pending rows with `FOR UPDATE SKIP LOCKED`, then
    `mark_sent` / `mark_failed` after the side effect completes.
    """

    DEFAULT_MAX_ATTEMPTS = 10
    BACKOFF_SCHEDULE_SECONDS: tuple[int, ...] = (5, 15, 60, 300, 900, 1800, 3600, 7200, 14400, 28800)

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def enqueue_tg_message(
        self,
        *,
        chat_id: int,
        text: str,
        parse_mode: str | None = None,
        disable_web_page_preview: bool | None = None,
        reply_markup=None,
        user_id: int | None = None,
        correlation_key: str | None = None,
        max_attempts: int | None = None,
    ) -> OutboxMessage | None:
        """Enqueue a Telegram text message. Returns the row, or None on
        correlation_key conflict (treated as already-enqueued duplicate).

        `reply_markup` — aiogram InlineKeyboardMarkup; сериализуется в payload
        через `model_dump(exclude_none=True)` и восстанавливается воркером.
        `user_id` — для propagation `bot_blocked` при TelegramForbiddenError
        и сброса флага после успешной доставки."""
        payload: dict = {'text': text}
        if parse_mode is not None:
            payload['parse_mode'] = parse_mode
        if disable_web_page_preview is not None:
            payload['disable_web_page_preview'] = bool(disable_web_page_preview)
        if reply_markup is not None:
            try:
                payload['reply_markup'] = reply_markup.model_dump(exclude_none=True)
            except AttributeError:
                payload['reply_markup'] = dict(reply_markup) if isinstance(reply_markup, dict) else reply_markup
        if user_id is not None:
            payload['user_id'] = int(user_id)

        row = OutboxMessage(
            kind=OutboxKind.tg_message,
            target_chat_id=chat_id,
            payload_json=payload,
            status=OutboxStatus.pending,
            attempts=0,
            max_attempts=max_attempts if max_attempts is not None else self.DEFAULT_MAX_ATTEMPTS,
            next_attempt_at=datetime.now(timezone.utc),
            correlation_key=correlation_key,
        )
        # SAVEPOINT, чтобы коллизия correlation_key не откатывала
        # внешнюю транзакцию (в `_consume_paid_invoice` она содержит
        # invoice→consumed, balance update, audit и т.п.).
        try:
            async with self.session.begin_nested():
                self.session.add(row)
                await self.session.flush()
        except IntegrityError as exc:
            if correlation_key is not None and 'uq_outbox_messages_correlation_key' in str(exc.orig):
                return None
            raise
        return row

    async def claim_due(
        self,
        *,
        limit: int = 50,
        processing_timeout_seconds: int = 120,
    ) -> list[OutboxMessage]:
        """Atomically claim up to `limit` due rows for the worker.

        Picks up:
          - status=pending AND next_attempt_at <= now (normal), и
          - status=processing AND updated_at < now - processing_timeout (orphaned:
            воркер крашнулся между claim и mark_sent/mark_failed).

        Uses `FOR UPDATE SKIP LOCKED` чтобы воркеры не дрались за один и тот же ряд.
        Status flipped to processing + attempts++ + updated_at refreshed.
        """
        now = datetime.now(timezone.utc)
        orphan_cutoff = now - timedelta(seconds=processing_timeout_seconds)
        stmt = (
            select(OutboxMessage)
            .where(
                or_(
                    and_(
                        OutboxMessage.status == OutboxStatus.pending,
                        OutboxMessage.next_attempt_at <= now,
                    ),
                    and_(
                        OutboxMessage.status == OutboxStatus.processing,
                        OutboxMessage.updated_at < orphan_cutoff,
                    ),
                )
            )
            .order_by(OutboxMessage.next_attempt_at.asc(), OutboxMessage.id.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        for row in rows:
            row.status = OutboxStatus.processing
            row.attempts += 1
            row.updated_at = now
        await self.session.flush()
        return rows

    async def mark_sent(self, message_id: int) -> None:
        now = datetime.now(timezone.utc)
        row = await self.session.get(OutboxMessage, message_id)
        if row is None:
            return
        row.status = OutboxStatus.sent
        row.processed_at = now
        row.updated_at = now
        row.last_error = None
        await self.session.flush()

    async def mark_failed(self, message_id: int, error: str, *, dead: bool = False) -> None:
        now = datetime.now(timezone.utc)
        row = await self.session.get(OutboxMessage, message_id)
        if row is None:
            return

        if dead or row.attempts >= row.max_attempts:
            row.status = OutboxStatus.dead
            row.processed_at = now
        else:
            backoff_idx = min(row.attempts - 1, len(self.BACKOFF_SCHEDULE_SECONDS) - 1)
            backoff = self.BACKOFF_SCHEDULE_SECONDS[max(0, backoff_idx)]
            row.status = OutboxStatus.pending
            row.next_attempt_at = now + timedelta(seconds=backoff)

        row.last_error = (error or '')[:2000]
        row.updated_at = now
        await self.session.flush()
