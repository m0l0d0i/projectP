from __future__ import annotations

import inspect
import logging
import secrets
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from dateutil.relativedelta import relativedelta
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Invoice, InvoicePurpose, Subscription, TransactionType, User
from app.db.repositories import (
    AppSettingsRepository,
    SubscriptionRepository,
    TariffRepository,
    TransactionRepository,
    UserRepository,
)
from app.services.marzban import MarzbanAPIError, MarzbanClient, MarzbanUser
from app.services.node_policy import NodePolicyService, NodeSelectionDecision, NodeSelectionRequest
from app.services.routing_profiles import ResolvedRoutingProfile, RoutingProfilesService
from app.services.subscription_urls import canonicalize_subscription_url_from_settings
from app.services.tariffs import PricingService
from app.utils.runtime_settings import effective_int_from_row

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ResetTrafficQuote:
    monthly_price: Decimal
    days_left_in_month: int
    days_in_month: int
    reset_price: Decimal
    cycle_start_at: datetime | None = None
    cycle_end_at: datetime | None = None

    @property
    def days_left_in_cycle(self) -> int:
        return self.days_left_in_month

    @property
    def days_in_cycle(self) -> int:
        return self.days_in_month


@dataclass(slots=True)
class SyncedSubscription:
    subscription: Subscription
    remote: MarzbanUser


@dataclass(slots=True)
class TrialSettingsSnapshot:
    duration_days: int
    traffic_gb: int
    device_count: int


@dataclass(slots=True)
class SubscriptionRoutingRuntimePreview:
    profile_code: str | None
    profile_title: str | None
    requested_tags: list[str]
    matched_by_default: bool
    runtime_config: dict[str, Any]
    node_decision: NodeSelectionDecision
    xray_fragment_preview: dict[str, Any] | None

    @property
    def selected_node_code(self) -> str | None:
        return self.node_decision.selected_node_code


class SubscriptionService:
    def __init__(self, session: AsyncSession, settings: Settings, marzban: MarzbanClient) -> None:
        self.session = session
        self.settings = settings
        self.marzban = marzban
        self.subscriptions = SubscriptionRepository(session)
        self.users = UserRepository(session)
        self.transactions = TransactionRepository(session)
        self.tariffs = TariffRepository(session)
        self.app_settings = AppSettingsRepository(session)

    @staticmethod
    def _gen_username(tg_id: int) -> str:
        return f'tg{tg_id}_{secrets.token_hex(3)}'

    @staticmethod
    def _invoice_note(plan_code: str, invoice_id: int) -> str:
        return f'{plan_code}:invoice:{invoice_id}'

    @staticmethod
    def _now_utc() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _normalize_utc(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _bytes_to_exact_gb(value: int | None) -> int | None:
        if value in (None, 0):
            return None
        if value < 0:
            return None
        gb = value // (1024 ** 3)
        if gb <= 0:
            return None
        return int(gb)

    @staticmethod
    def _resolve_trial_device_mode(device_count: int) -> tuple[str, int | None, int | None]:
        normalized_count = max(1, int(device_count))
        if normalized_count == 1:
            return 'single', 1, 1
        return 'custom', normalized_count, normalized_count

    @staticmethod
    def _normalize_device_mode(value: object) -> str:
        normalized = str(value or 'single').strip().lower()
        if normalized in {'single', 'custom', 'unlimited'}:
            return normalized
        return 'single'

    @staticmethod
    def _resolve_pricing_device_count(device_mode: str, raw_count: object) -> int:
        if device_mode == 'single':
            return 1
        if device_mode == 'custom':
            try:
                normalized = int(raw_count or 0)
            except (TypeError, ValueError):
                normalized = 0
            return max(2, min(PricingService.MAX_CUSTOM_DEVICES, normalized or 2))
        return 0

    @staticmethod
    def _calculate_cycle_day_stats(
        *,
        now: datetime,
        cycle_start_at: datetime,
        cycle_end_at: datetime,
    ) -> tuple[int, int]:
        normalized_now = now.astimezone(timezone.utc)
        normalized_start = cycle_start_at.astimezone(timezone.utc)
        normalized_end = cycle_end_at.astimezone(timezone.utc)

        total_seconds = max((normalized_end - normalized_start).total_seconds(), 1)
        remaining_seconds = max((normalized_end - normalized_now).total_seconds(), 0)

        total_days = max(1, int((total_seconds + 86399) // 86400))
        remaining_days = max(1, int((remaining_seconds + 86399) // 86400))
        return remaining_days, total_days

    def _normalize_existing_subscription_url(self, value: str | None) -> str | None:
        return canonicalize_subscription_url_from_settings(value, self.settings)

    def _resolve_subscription_url(self, subscription: Subscription, remote: MarzbanUser | None = None) -> str | None:
        remote_url = remote.subscription_url if remote is not None else None
        if remote_url:
            normalized_remote_url = self._normalize_existing_subscription_url(remote_url)
            if normalized_remote_url:
                return normalized_remote_url
            logger.warning(
                'Ignoring non-canonical remote subscription_url for subscription_id=%s username=%s value=%s',
                getattr(subscription, 'id', None),
                getattr(subscription, 'marzban_username', None),
                remote_url,
            )

        existing_url = self._normalize_existing_subscription_url(getattr(subscription, 'subscription_url', None))
        return existing_url

    def _calculate_cycle_end(
        self,
        *,
        cycle_start_at: datetime,
        expire_at: datetime | None,
    ) -> datetime:
        normalized_start = self._normalize_utc(cycle_start_at) or self._now_utc()
        normalized_expire = self._normalize_utc(expire_at)
        candidate_end = normalized_start + relativedelta(months=1)

        if normalized_expire is not None and normalized_expire < candidate_end:
            candidate_end = normalized_expire

        if candidate_end <= normalized_start:
            candidate_end = normalized_start + timedelta(seconds=1)

        return candidate_end

    def _assign_cycle_state(
        self,
        subscription: Subscription,
        *,
        cycle_start_at: datetime | None,
        cycle_end_at: datetime | None,
        cycle_base_bytes: int | None,
        cycle_extra_traffic_bytes: int | None = None,
        next_reset_at: datetime | None = None,
        last_reset_at: datetime | None = None,
    ) -> None:
        subscription.traffic_cycle_start_at = self._normalize_utc(cycle_start_at)
        subscription.traffic_cycle_end_at = self._normalize_utc(cycle_end_at)
        subscription.traffic_cycle_base_bytes = cycle_base_bytes
        if cycle_extra_traffic_bytes is not None:
            subscription.cycle_extra_traffic_bytes = max(0, int(cycle_extra_traffic_bytes))
        subscription.next_traffic_reset_at = self._normalize_utc(next_reset_at)
        subscription.last_traffic_reset_at = self._normalize_utc(last_reset_at)

        effective_total = subscription.effective_cycle_total_bytes
        if effective_total is None:
            if cycle_base_bytes is None and subscription.monthly_traffic_bytes in (None, 0):
                subscription.data_limit_bytes = None
        else:
            subscription.data_limit_bytes = effective_total

    def _configure_unlimited_cycle(self, subscription: Subscription) -> None:
        self._assign_cycle_state(
            subscription,
            cycle_start_at=None,
            cycle_end_at=None,
            cycle_base_bytes=None,
            cycle_extra_traffic_bytes=0,
            next_reset_at=None,
        )

    def _configure_periodic_cycle(
        self,
        subscription: Subscription,
        *,
        cycle_anchor_at: datetime,
        expire_at: datetime | None,
        cycle_base_bytes: int,
        reset_extra_traffic: bool,
    ) -> None:
        normalized_anchor = self._normalize_utc(cycle_anchor_at) or self._now_utc()
        cycle_end_at = self._calculate_cycle_end(
            cycle_start_at=normalized_anchor,
            expire_at=expire_at,
        )

        cycle_extra = 0 if reset_extra_traffic else max(0, int(getattr(subscription, 'cycle_extra_traffic_bytes', 0) or 0))
        next_reset_at = cycle_end_at if expire_at is None or cycle_end_at < expire_at else None

        self._assign_cycle_state(
            subscription,
            cycle_start_at=normalized_anchor,
            cycle_end_at=cycle_end_at,
            cycle_base_bytes=max(0, int(cycle_base_bytes)),
            cycle_extra_traffic_bytes=cycle_extra,
            next_reset_at=next_reset_at,
        )

    def _configure_trial_cycle(
        self,
        subscription: Subscription,
        *,
        cycle_anchor_at: datetime,
        expire_at: datetime,
        trial_limit_bytes: int | None,
    ) -> None:
        normalized_anchor = self._normalize_utc(cycle_anchor_at) or self._now_utc()
        normalized_expire = self._normalize_utc(expire_at) or normalized_anchor

        self._assign_cycle_state(
            subscription,
            cycle_start_at=normalized_anchor,
            cycle_end_at=normalized_expire,
            cycle_base_bytes=trial_limit_bytes,
            cycle_extra_traffic_bytes=0,
            next_reset_at=None,
        )

    def _derive_cycle_bounds(self, subscription: Subscription, *, now: datetime) -> tuple[datetime, datetime]:
        cycle_start_at = self._normalize_utc(getattr(subscription, 'traffic_cycle_start_at', None))
        cycle_end_at = self._normalize_utc(getattr(subscription, 'traffic_cycle_end_at', None))

        if cycle_start_at is not None and cycle_end_at is not None and cycle_end_at > cycle_start_at:
            return cycle_start_at, cycle_end_at

        fallback_end = self._normalize_utc(getattr(subscription, 'next_traffic_reset_at', None))
        expire_at = self._normalize_utc(getattr(subscription, 'expire_date', None))

        if fallback_end is None:
            fallback_end = self._calculate_cycle_end(cycle_start_at=now, expire_at=expire_at)

        fallback_start = self._normalize_utc(getattr(subscription, 'traffic_cycle_start_at', None))
        if fallback_start is None:
            fallback_start = fallback_end - relativedelta(months=1)
            if fallback_start >= fallback_end:
                fallback_start = fallback_end - timedelta(days=30)

        if fallback_start >= fallback_end:
            fallback_start = fallback_end - timedelta(seconds=1)

        return fallback_start, fallback_end

    @staticmethod
    def _coerce_int(value: object, default: int | None = None) -> int | None:
        try:
            if value is None or value == '':
                return default
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _coerce_str(value: object) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    def _set_subscription_tariff_context(
        self,
        subscription: Subscription,
        *,
        tariff_plan_id: int | None,
        tariff_code: str | None,
    ) -> None:
        if hasattr(subscription, 'current_tariff_id'):
            subscription.current_tariff_id = tariff_plan_id
        if hasattr(subscription, 'current_tariff_code'):
            subscription.current_tariff_code = tariff_code

    def _invoice_tariff_snapshot(self, invoice: Invoice) -> dict[str, object]:
        snapshot = getattr(invoice, 'tariff_snapshot_json', None)
        if isinstance(snapshot, dict):
            return dict(snapshot)
        payload = dict(invoice.payload_json or {})
        embedded = payload.get('tariff_snapshot')
        if isinstance(embedded, dict):
            return dict(embedded)
        return {}

    async def _resolve_tariff_plan_from_invoice(self, invoice: Invoice):
        snapshot = self._invoice_tariff_snapshot(invoice)
        payload = dict(invoice.payload_json or {})

        tariff_plan_id = self._coerce_int(
            getattr(invoice, 'tariff_plan_id', None)
            or snapshot.get('tariff_plan_id')
            or payload.get('tariff_plan_id')
        )
        if tariff_plan_id is not None:
            plan = await self.tariffs.get_by_id(tariff_plan_id)
            if plan is not None:
                return plan

        tariff_code = self._coerce_str(
            snapshot.get('tariff_code')
            or payload.get('package_code')
            or payload.get('tariff_code')
        )
        if tariff_code:
            plan = await self.tariffs.get_by_code(tariff_code)
            if plan is not None:
                return plan

        return None

    def _snapshot_value(self, snapshot: dict[str, object], payload: dict[str, object], *keys: str, default=None):
        for key in keys:
            if key in snapshot and snapshot.get(key) not in (None, ''):
                return snapshot.get(key)
        for key in keys:
            if key in payload and payload.get(key) not in (None, ''):
                return payload.get(key)
        return default

    @staticmethod
    def _normalize_routing_tags(value: list[str] | tuple[str, ...] | set[str] | str | None) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            raw_items = [part.strip() for part in value.replace(';', ',').split(',')]
        else:
            raw_items = [str(item).strip() for item in value]

        normalized: list[str] = []
        seen: set[str] = set()
        for item in raw_items:
            candidate = item.lower()
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            normalized.append(candidate)
        return normalized

    def _subscription_routing_tags(self, subscription: Subscription | None) -> list[str]:
        tags: list[str] = []
        if subscription is None:
            return tags

        def _push(value: str | None) -> None:
            normalized = (value or '').strip().lower()
            if normalized and normalized not in tags:
                tags.append(normalized)

        if getattr(subscription, 'is_trial', False):
            _push('trial')

        device_mode = self._normalize_device_mode(getattr(subscription, 'used_device_mode', None))
        _push(device_mode)
        if device_mode == 'unlimited':
            _push('unlimited_devices')

        if getattr(subscription, 'monthly_traffic_bytes', None) in (None, 0):
            _push('unlimited_traffic')

        tariff_code = self._coerce_str(getattr(subscription, 'current_tariff_code', None))
        if tariff_code:
            _push(tariff_code)

        return tags

    async def _resolve_routing_profile(
        self,
        *,
        requested_tags: list[str] | tuple[str, ...] | set[str] | str | None = None,
        profile_code: str | None = None,
        fallback_to_default: bool = True,
    ) -> ResolvedRoutingProfile:
        service = RoutingProfilesService(self.session)
        normalized_requested_tags = self._normalize_routing_tags(requested_tags)

        explicit_code = self._coerce_str(profile_code)
        if explicit_code:
            profile = await service.get_by_code(explicit_code)
            if profile is None or not getattr(profile, 'is_enabled', False):
                raise ValueError('Указанный routing profile не найден или отключен.')
            return ResolvedRoutingProfile(
                profile=profile,
                requested_tags=normalized_requested_tags,
                matched_by_default=bool(getattr(profile, 'is_default', False)),
            )

        return await service.select_for_tags(normalized_requested_tags, fallback_to_default=fallback_to_default)

    @staticmethod
    def _resolved_profile_runtime_config(resolved: ResolvedRoutingProfile) -> dict[str, Any]:
        runtime_config = getattr(resolved, 'runtime_config', None)
        if runtime_config is None:
            config = getattr(resolved, 'config', None)
            return dict(config or {})
        if isinstance(runtime_config, dict):
            return dict(runtime_config)
        if is_dataclass(runtime_config):
            return dict(asdict(runtime_config))
        if hasattr(runtime_config, '__dict__'):
            return {k: v for k, v in vars(runtime_config).items() if not k.startswith('_')}
        config = getattr(resolved, 'config', None)
        return dict(config or {})

    def _build_node_selection_request_from_profile(self, resolved: ResolvedRoutingProfile) -> NodeSelectionRequest:
        profile = getattr(resolved, 'profile', None)
        runtime_config = self._resolved_profile_runtime_config(resolved)

        required_tags = set(self._normalize_routing_tags(runtime_config.get('required_tags')))
        preferred_tags = set(self._normalize_routing_tags(runtime_config.get('preferred_tags')))
        avoid_tags = set(self._normalize_routing_tags(runtime_config.get('avoid_tags')))
        location_allow = set(self._normalize_routing_tags(runtime_config.get('location_allow')))
        transport_allow = set(self._normalize_routing_tags(runtime_config.get('transport_allow')))

        requested_tags = set(self._normalize_routing_tags(getattr(resolved, 'requested_tags', None)))
        profile_tags = set(self._normalize_routing_tags(getattr(profile, 'match_tags', None)))
        required_tags |= profile_tags
        required_tags |= requested_tags

        metadata = {
            'routing_profile_code': getattr(profile, 'code', None),
            'routing_profile_title': getattr(profile, 'title', None),
            'routing_match_tags': sorted(profile_tags),
            'routing_requested_tags': sorted(requested_tags),
        }

        for key in ('premium_only', 'reserve_only', 'preferred_node_codes', 'avoid_node_codes', 'preferred_locations', 'xray_outbound_tag', 'xray_balancer_tag'):
            if key in runtime_config and runtime_config.get(key) not in (None, '', [], {}):
                metadata[key] = runtime_config.get(key)

        return NodeSelectionRequest(
            policy_name=str(runtime_config.get('strategy') or getattr(profile, 'code', None) or 'default'),
            required_tags=required_tags,
            preferred_tags=preferred_tags,
            avoid_tags=avoid_tags,
            location_allow=location_allow,
            transport_allow=transport_allow,
            require_subscription_url=bool(runtime_config.get('require_subscription_url', False)),
            require_api_url=bool(runtime_config.get('require_api_url', False)),
            require_synced_source=bool(runtime_config.get('require_synced_source', True)),
            allow_missing_source=bool(runtime_config.get('allow_missing_source', False)),
            metadata=metadata,
        )

    def _build_xray_fragment_preview(
        self,
        resolved: ResolvedRoutingProfile,
        decision: NodeSelectionDecision,
    ) -> dict[str, Any] | None:
        runtime_config = self._resolved_profile_runtime_config(resolved)
        if not runtime_config:
            return None

        selected = decision.node
        preview = {
            'profile_code': getattr(getattr(resolved, 'profile', None), 'code', None),
            'strategy': runtime_config.get('strategy') or getattr(getattr(resolved, 'profile', None), 'code', None) or 'default',
            'selected_node_code': getattr(selected, 'code', None),
            'selected_node_api_base_url': getattr(selected, 'api_base_url', None),
            'selected_node_subscription_base_url': getattr(selected, 'subscription_base_url', None),
        }

        for key in (
            'xray_outbound_tag',
            'xray_balancer_tag',
            'xray_domain_match',
            'xray_ip_match',
            'xray_rule_protocols',
            'xray_rule_networks',
            'xray_rule_overrides',
            'xray_fragment',
        ):
            if key in runtime_config and runtime_config.get(key) not in (None, '', [], {}):
                preview[key] = runtime_config.get(key)

        return preview if len(preview) > 4 else None

    async def preview_runtime_routing(
        self,
        subscription: Subscription | None = None,
        *,
        requested_tags: list[str] | tuple[str, ...] | set[str] | str | None = None,
        profile_code: str | None = None,
        fallback_to_default: bool = True,
    ) -> SubscriptionRoutingRuntimePreview:
        derived_tags = self._subscription_routing_tags(subscription)
        explicit_tags = self._normalize_routing_tags(requested_tags)
        merged_tags = derived_tags + [tag for tag in explicit_tags if tag not in derived_tags]

        resolved = await self._resolve_routing_profile(
            requested_tags=merged_tags,
            profile_code=profile_code,
            fallback_to_default=fallback_to_default,
        )
        request = self._build_node_selection_request_from_profile(resolved)
        decision = await NodePolicyService(self.session).select_node(request)
        preview = self._build_xray_fragment_preview(resolved, decision)

        profile = getattr(resolved, 'profile', None)
        return SubscriptionRoutingRuntimePreview(
            profile_code=getattr(profile, 'code', None),
            profile_title=getattr(profile, 'title', None),
            requested_tags=self._normalize_routing_tags(getattr(resolved, 'requested_tags', None)),
            matched_by_default=bool(getattr(resolved, 'matched_by_default', False)),
            runtime_config=self._resolved_profile_runtime_config(resolved),
            node_decision=decision,
            xray_fragment_preview=preview,
        )

    async def resolve_runtime_node_for_subscription(
        self,
        subscription: Subscription | None = None,
        *,
        requested_tags: list[str] | tuple[str, ...] | set[str] | str | None = None,
        profile_code: str | None = None,
        fallback_to_default: bool = True,
    ) -> NodeSelectionDecision:
        preview = await self.preview_runtime_routing(
            subscription,
            requested_tags=requested_tags,
            profile_code=profile_code,
            fallback_to_default=fallback_to_default,
        )
        return preview.node_decision

    async def _load_trial_settings(self) -> TrialSettingsSnapshot:
        row = await self.app_settings.get()
        if row is not None:
            return TrialSettingsSnapshot(
                duration_days=effective_int_from_row(row, 'trial_duration_days', self.settings.trial_duration_days, minimum=1),
                traffic_gb=effective_int_from_row(row, 'trial_traffic_gb', self.settings.trial_traffic_gb, minimum=0),
                device_count=effective_int_from_row(row, 'trial_device_count', self.settings.trial_device_count, minimum=1),
            )

        fallback_days = max(1, int(getattr(self.settings, 'trial_duration_days', 1) or 1))
        fallback_traffic = max(0, int(getattr(self.settings, 'trial_traffic_gb', 5)))
        fallback_devices = max(1, int(getattr(self.settings, 'trial_device_count', 1)))

        return TrialSettingsSnapshot(
            duration_days=fallback_days,
            traffic_gb=fallback_traffic,
            device_count=fallback_devices,
        )

    async def _list_tariff_candidates_for_traffic(self, monthly_traffic_gb: int) -> list[object]:
        active_plans = await self.tariffs.list_active()
        candidates = [plan for plan in active_plans if getattr(plan, 'monthly_traffic_gb', None) == monthly_traffic_gb]
        if candidates:
            return candidates

        list_all = getattr(self.tariffs, 'list_all', None)
        if callable(list_all):
            result = list_all()
            all_plans = await result if inspect.isawaitable(result) else result
            candidates = [plan for plan in all_plans if getattr(plan, 'monthly_traffic_gb', None) == monthly_traffic_gb]
            if candidates:
                logger.warning(
                    'Recovered tariff from inactive/all plans for reset: monthly_traffic_gb=%s candidates=%s',
                    monthly_traffic_gb,
                    [getattr(plan, 'code', None) for plan in candidates],
                )
                return candidates

        return []

    async def _recover_tariff_plan_for_subscription(self, subscription: Subscription):
        current_tariff_id = self._coerce_int(getattr(subscription, 'current_tariff_id', None))
        if current_tariff_id is not None:
            plan = await self.tariffs.get_by_id(current_tariff_id)
            if plan is not None:
                return plan

        current_tariff_code = self._coerce_str(getattr(subscription, 'current_tariff_code', None))
        if current_tariff_code:
            plan = await self.tariffs.get_by_code(current_tariff_code)
            if plan is not None:
                return plan

        monthly_traffic_bytes = getattr(subscription, 'traffic_cycle_base_bytes', None)
        if monthly_traffic_bytes in (None, 0):
            monthly_traffic_bytes = getattr(subscription, 'monthly_traffic_bytes', None)
        monthly_traffic_gb = self._bytes_to_exact_gb(monthly_traffic_bytes)
        if monthly_traffic_gb is None:
            return None

        candidates = await self._list_tariff_candidates_for_traffic(monthly_traffic_gb)
        if not candidates:
            logger.warning(
                'Failed to recover tariff for reset: no plan with monthly_traffic_gb=%s for subscription_id=%s '
                '(device_mode=%s, device_count=%s)',
                monthly_traffic_gb,
                getattr(subscription, 'id', None),
                getattr(subscription, 'used_device_mode', None),
                getattr(subscription, 'used_device_count', None),
            )
            return None

        device_mode = self._normalize_device_mode(getattr(subscription, 'used_device_mode', None))
        used_device_count = self._resolve_pricing_device_count(
            device_mode,
            getattr(subscription, 'used_device_count', None),
        )

        if device_mode == 'single':
            preferred = [plan for plan in candidates if int(getattr(plan, 'online_limit_single', 1) or 1) == 1]
            if preferred:
                return preferred[0]

        if device_mode == 'custom' and used_device_count > 1:
            return candidates[0]

        if device_mode == 'unlimited':
            preferred = [plan for plan in candidates if getattr(plan, 'online_limit_unlimited', None) is None]
            if preferred:
                return preferred[0]
            return candidates[0]

        return candidates[0]

    async def _calculate_reset_monthly_price(self, subscription: Subscription, plan) -> Decimal:
        monthly_traffic_gb = getattr(plan, 'monthly_traffic_gb', None) if plan is not None else None
        if monthly_traffic_gb is None:
            cycle_base_bytes = getattr(subscription, 'traffic_cycle_base_bytes', None)
            if cycle_base_bytes in (None, 0):
                cycle_base_bytes = getattr(subscription, 'monthly_traffic_bytes', None)
            monthly_traffic_gb = self._bytes_to_exact_gb(cycle_base_bytes)

        if monthly_traffic_gb is None:
            raise ValueError('Не удалось определить месячный лимит трафика для расчета сброса.')

        rules = await PricingService.get_rules(self.session)
        device_mode = self._normalize_device_mode(getattr(subscription, 'used_device_mode', None))
        device_count = self._resolve_pricing_device_count(device_mode, getattr(subscription, 'used_device_count', None))

        calculate_plan_monthly_price = getattr(PricingService, 'calculate_plan_monthly_price', None)
        if plan is not None and callable(calculate_plan_monthly_price):
            try:
                monthly_price = calculate_plan_monthly_price(
                    plan,
                    traffic_gb=monthly_traffic_gb,
                    device_mode=device_mode,
                    device_count=device_count,
                    rules=rules,
                )
                return Decimal(str(monthly_price))
            except Exception:
                logger.exception(
                    'Failed to calculate DB-driven reset monthly price, falling back to legacy pricing: subscription_id=%s tariff=%s',
                    getattr(subscription, 'id', None),
                    getattr(plan, 'code', None),
                )

        monthly_price = PricingService.calculate_monthly_price(
            int(monthly_traffic_gb),
            device_mode,
            device_count,
            rules,
        )
        return Decimal(str(monthly_price))

    async def get_or_create_marzban_user(self, user: User, subscription: Subscription | None = None) -> str:
        if subscription is not None and subscription.marzban_username:
            return subscription.marzban_username
        return self._gen_username(user.tg_id)

    async def sync_remote_state(self, subscription: Subscription) -> SyncedSubscription:
        remote = await self.marzban.get_user(subscription.marzban_username)
        self._sync_local_from_remote(subscription, remote)
        return SyncedSubscription(subscription=subscription, remote=remote)

    def _sync_local_from_remote(self, subscription: Subscription, remote: MarzbanUser) -> None:
        subscription.expire_date = remote.expire_datetime
        subscription.data_limit_bytes = remote.data_limit
        subscription.used_traffic_bytes = remote.used_traffic
        subscription.subscription_url = self._resolve_subscription_url(subscription, remote)

        online_field = self.settings.marzban_online_limit_field
        if online_field:
            subscription.online_limit = remote.raw.get(online_field)

        subscription.is_active = remote.status not in {'expired', 'disabled'} and (
            remote.expire_datetime is None or remote.expire_datetime > self._now_utc()
        )

    async def calculate_manual_reset_quote(self, subscription: Subscription) -> ResetTrafficQuote:
        if getattr(subscription, 'is_trial', False):
            raise ValueError('Для тестовой подписки сброс трафика недоступен.')

        monthly_traffic_bytes = getattr(subscription, 'monthly_traffic_bytes', None)
        if monthly_traffic_bytes in (None, 0):
            raise ValueError('Для безлимитного тарифа ручной сброс трафика не требуется.')

        current_tariff_code = getattr(subscription, 'current_tariff_code', None)
        used_device_mode = getattr(subscription, 'used_device_mode', None)
        used_device_count = int(getattr(subscription, 'used_device_count', 0) or 0)
        subscription_id = getattr(subscription, 'id', None)

        plan = None
        if current_tariff_code:
            plan_result = self.tariffs.get_by_code(current_tariff_code)
            plan = await plan_result if inspect.isawaitable(plan_result) else plan_result
            if plan is None:
                logger.warning(
                    'Tariff code is set but plan not found for reset: subscription_id=%s tariff_code=%s',
                    subscription_id,
                    current_tariff_code,
                )

        if plan is None:
            plan = await self._recover_tariff_plan_for_subscription(subscription)
            if plan is not None and not current_tariff_code and hasattr(subscription, 'current_tariff_code'):
                subscription.current_tariff_code = getattr(plan, 'code', None)

        if plan is None:
            logger.warning(
                'Falling back to pricing-rules-only reset calculation: subscription_id=%s monthly_traffic_bytes=%s '
                'device_mode=%s device_count=%s',
                subscription_id,
                monthly_traffic_bytes,
                used_device_mode,
                used_device_count,
            )

        try:
            monthly_price = await self._calculate_reset_monthly_price(subscription, plan)
        except ValueError:
            logger.error(
                'Unable to determine tariff for manual reset: subscription_id=%s monthly_traffic_bytes=%s '
                'device_mode=%s device_count=%s',
                subscription_id,
                monthly_traffic_bytes,
                used_device_mode,
                used_device_count,
            )
            raise ValueError('Не удалось определить тариф услуги для сброса трафика. Обратитесь в поддержку.')

        now = self._now_utc()
        cycle_start_at, cycle_end_at = self._derive_cycle_bounds(subscription, now=now)
        days_left_in_cycle, days_in_cycle = self._calculate_cycle_day_stats(
            now=now,
            cycle_start_at=cycle_start_at,
            cycle_end_at=cycle_end_at,
        )
        reset_price = PricingService.calculate_reset_price(monthly_price, days_left_in_cycle, days_in_cycle)

        return ResetTrafficQuote(
            monthly_price=Decimal(str(monthly_price)),
            days_left_in_month=days_left_in_cycle,
            days_in_month=days_in_cycle,
            reset_price=Decimal(str(reset_price)),
            cycle_start_at=cycle_start_at,
            cycle_end_at=cycle_end_at,
        )

    async def reset_traffic_paid(self, user: User, subscription: Subscription) -> tuple[ResetTrafficQuote, MarzbanUser]:
        # 1. Защита от гонки (Race Condition): блокируем строки подписки и пользователя для безопасного списания
        subscription = await self.subscriptions.get_by_id_for_update(subscription.id)
        user = await self.users.get_by_id_for_update(user.id)

        if not subscription or not user:
            raise ValueError('Пользователь или услуга не найдены.')

        if subscription.user_id != user.id:
            raise ValueError('Услуга принадлежит другому пользователю.')

        if subscription.is_trial:
            raise ValueError('Для тестовой подписки сброс трафика недоступен.')

        quote = await self.calculate_manual_reset_quote(subscription)
        if (user.balance or Decimal('0.00')) < quote.reset_price:
            raise ValueError(f'Недостаточно средств. Нужно {quote.reset_price} ₽.')

        remote = await self.marzban.reset_user_usage(subscription.marzban_username)
        await self.users.subtract_balance(user, quote.reset_price)
        await self.transactions.create(
            user.id,
            quote.reset_price,
            TransactionType.outcome,
            f'Платный сброс трафика по услуге {subscription.service_id}',
        )

        self._sync_local_from_remote(subscription, remote)

        cycle_start_at = self._now_utc()
        expire_at = self._normalize_utc(subscription.expire_date)
        cycle_base_bytes = getattr(subscription, 'monthly_traffic_bytes', None)
        if cycle_base_bytes in (None, 0):
            cycle_base_bytes = getattr(subscription, 'traffic_cycle_base_bytes', None)

        if cycle_base_bytes not in (None, 0):
            self._configure_periodic_cycle(
                subscription,
                cycle_anchor_at=cycle_start_at,
                expire_at=expire_at,
                cycle_base_bytes=int(cycle_base_bytes),
                reset_extra_traffic=True,
            )

        subscription.subscription_url = self._resolve_subscription_url(subscription, remote)
        subscription.notified_low_traffic = False
        subscription.notified_exhausted = False
        return quote, remote

    async def issue_trial(self, user: User) -> MarzbanUser:
        # Защита от спам-кликов (Race Condition) при выдаче триала
        user = await self.users.get_by_id_for_update(user.id)
        if not user:
            raise ValueError('Пользователь не найден')

        if user.trial_issued_at:
            raise ValueError('Тест уже выдавался ранее')

        now = self._now_utc()
        local_active = await self.subscriptions.get_latest_active(user.id)
        if local_active and local_active.expire_date and local_active.expire_date > now and local_active.is_active:
            raise ValueError('У вас уже есть активная подписка. Тестовый период недоступен.')

        active = await self.subscriptions.get_latest_active(user.id)
        if active:
            try:
                synced = await self.sync_remote_state(active)
                if synced.subscription.is_alive_local:
                    raise ValueError('У вас уже есть активная услуга. Тест недоступен.')
            except MarzbanAPIError as exc:
                if exc.status_code not in (404, None):
                    raise

        trial_settings = await self._load_trial_settings()
        expire_at = now + timedelta(days=trial_settings.duration_days)
        data_limit = PricingService.gb_to_bytes(trial_settings.traffic_gb)
        device_mode, used_device_count, online_limit = self._resolve_trial_device_mode(trial_settings.device_count)

        marzban_username = await self.get_or_create_marzban_user(user)
        trial_note = (
            f'trial:{trial_settings.duration_days}d:'
            f'{trial_settings.traffic_gb}gb:{trial_settings.device_count}dev'
        )

        marzban_user = await self.marzban.create_or_update_vless_user(
            username=marzban_username,
            expire_at=expire_at,
            data_limit_bytes=data_limit,
            online_limit=online_limit,
            note=trial_note,
            reset_traffic=True,
            is_trial=True,
        )

        subscription = await self.subscriptions.create(user_id=user.id, marzban_username=marzban_username)
        self._set_subscription_tariff_context(
            subscription,
            tariff_plan_id=None,
            tariff_code=f'trial_{trial_settings.duration_days}d',
        )
        subscription.expire_date = expire_at
        subscription.is_active = True
        subscription.data_limit_bytes = data_limit
        subscription.used_traffic_bytes = marzban_user.used_traffic
        subscription.monthly_traffic_bytes = None
        subscription.used_device_mode = device_mode
        subscription.used_device_count = used_device_count
        subscription.online_limit = online_limit
        subscription.subscription_url = self._resolve_subscription_url(subscription, marzban_user)
        subscription.is_trial = True

        self._configure_trial_cycle(
            subscription,
            cycle_anchor_at=now,
            expire_at=expire_at,
            trial_limit_bytes=data_limit,
        )
        await self.subscriptions.reset_notification_flags(subscription)

        user.trial_issued_at = self._now_utc()
        return marzban_user

    async def apply_invoice(self, user: User, invoice: Invoice) -> MarzbanUser | None:
        if invoice.purpose == InvoicePurpose.balance_topup:
            return None

        payload = dict(invoice.payload_json or {})
        subscription_id = payload.get('subscription_id')
        subscription: Subscription | None = None

        if subscription_id:
            subscription = await self.subscriptions.get_by_id_for_update(int(subscription_id))
            if subscription and subscription.user_id != user.id:
                raise ValueError('Неверная услуга для счета')

        if invoice.purpose == InvoicePurpose.tariff:
            return await self._apply_tariff_invoice(user, subscription, invoice)

        if invoice.purpose == InvoicePurpose.topup:
            if subscription is None:
                # Блокируем подписку для безопасного начисления экстра-трафика (Race Condition fix)
                latest_active = await self.subscriptions.get_latest_active(user.id)
                if latest_active:
                    subscription = await self.subscriptions.get_by_id_for_update(latest_active.id)
            return await self._apply_topup_invoice(subscription, invoice)

        raise ValueError(f'Unsupported invoice purpose: {invoice.purpose}')

    async def _apply_tariff_invoice(self, user: User, subscription: Subscription | None, invoice: Invoice) -> MarzbanUser:
        payload = dict(invoice.payload_json or {})
        snapshot = self._invoice_tariff_snapshot(invoice)
        plan = await self._resolve_tariff_plan_from_invoice(invoice)

        plan_code = self._coerce_str(
            self._snapshot_value(snapshot, payload, 'tariff_code', 'package_code')
        )
        if not plan_code and plan is not None:
            plan_code = self._coerce_str(getattr(plan, 'code', None))
        if not plan_code:
            raise ValueError('Счет не содержит tariff code')

        months = self._coerce_int(self._snapshot_value(snapshot, payload, 'months'), 1) or 1
        device_mode = self._normalize_device_mode(
            self._snapshot_value(snapshot, payload, 'selected_device_mode', 'device_mode', default='unlimited')
        )
        device_count = self._coerce_int(
            self._snapshot_value(snapshot, payload, 'selected_device_count', 'device_count'),
            0,
        ) or 0
        online_limit = self._coerce_int(self._snapshot_value(snapshot, payload, 'online_limit'), None)
        now = self._now_utc()

        monthly_gb_value = self._snapshot_value(snapshot, payload, 'monthly_traffic_gb')
        monthly_gb = self._coerce_int(monthly_gb_value, None)
        if monthly_gb is None and plan is not None:
            monthly_gb = self._coerce_int(getattr(plan, 'monthly_traffic_gb', None), None)

        monthly_limit_bytes = None if monthly_gb is None else PricingService.gb_to_bytes(int(monthly_gb))
        invoice_note = self._invoice_note(plan_code, invoice.id)

        tariff_plan_id = self._coerce_int(
            getattr(invoice, 'tariff_plan_id', None)
            or snapshot.get('tariff_plan_id')
            or getattr(plan, 'id', None),
            None,
        )

        if subscription is None:
            marzban_username = await self.get_or_create_marzban_user(user)
            subscription = await self.subscriptions.create(user_id=user.id, marzban_username=marzban_username)
            remote = None
        else:
            marzban_username = await self.get_or_create_marzban_user(user, subscription)
            remote = None
            try:
                synced = await self.sync_remote_state(subscription)
                remote = synced.remote
            except MarzbanAPIError as exc:
                if exc.status_code != 404:
                    raise

        already_applied = False
        if remote is not None and remote.raw.get('note') == invoice_note:
            already_applied = True

        if remote is None:
            expire_at = now + relativedelta(months=months)
            data_limit_bytes = monthly_limit_bytes
            marzban_user = await self.marzban.create_or_update_vless_user(
                username=marzban_username,
                expire_at=expire_at,
                data_limit_bytes=data_limit_bytes,
                online_limit=online_limit,
                note=invoice_note,
                reset_traffic=True,
                is_trial=False,
            )
        elif already_applied:
            marzban_user = remote
            expire_at = marzban_user.expire_datetime or subscription.expire_date or now
        else:
            tariff_limit_gb = int(monthly_gb or 0) if monthly_gb is not None else 0
            marzban_user = await self.marzban.renew_subscription(
                marzban_username,
                months=months,
                tariff_limit_gb=tariff_limit_gb,
                online_limit=online_limit,
                note=invoice_note,
                status='active',
            )
            expire_at = marzban_user.expire_datetime or (remote.expire_datetime or now) + relativedelta(months=months)

        self._sync_local_from_remote(subscription, marzban_user)

        self._set_subscription_tariff_context(
            subscription,
            tariff_plan_id=tariff_plan_id,
            tariff_code=plan_code,
        )
        subscription.expire_date = marzban_user.expire_datetime or expire_at
        subscription.is_active = True
        subscription.monthly_traffic_bytes = monthly_limit_bytes
        subscription.used_device_mode = device_mode
        subscription.used_device_count = device_count if device_mode == 'custom' else (1 if device_mode == 'single' else None)
        subscription.online_limit = online_limit
        subscription.subscription_url = self._resolve_subscription_url(subscription, marzban_user)
        subscription.is_trial = False

        if monthly_limit_bytes in (None, 0):
            self._configure_unlimited_cycle(subscription)
        else:
            if already_applied and getattr(subscription, 'traffic_cycle_start_at', None) and getattr(subscription, 'traffic_cycle_end_at', None):
                if getattr(subscription, 'traffic_cycle_base_bytes', None) in (None, 0):
                    subscription.traffic_cycle_base_bytes = monthly_limit_bytes
                subscription.data_limit_bytes = subscription.effective_cycle_total_bytes
            else:
                self._configure_periodic_cycle(
                    subscription,
                    cycle_anchor_at=now,
                    expire_at=subscription.expire_date,
                    cycle_base_bytes=monthly_limit_bytes,
                    reset_extra_traffic=True,
                )

        await self.subscriptions.reset_notification_flags(subscription)
        return marzban_user

    async def _apply_topup_invoice(self, subscription: Subscription | None, invoice: Invoice) -> MarzbanUser:
        if subscription is None:
            raise ValueError('У пользователя нет активной услуги для докупки трафика')

        if getattr(subscription, 'is_trial', False):
            raise ValueError('Для тестовой подписки докупка трафика недоступна.')

        if getattr(subscription, 'monthly_traffic_bytes', None) in (None, 0):
            raise ValueError('Для безлимитного тарифа докупка трафика не требуется и недоступна.')

        payload = dict(invoice.payload_json or {})
        topup_code = payload['topup_code']
        topup = PricingService.get_topup(topup_code)
        extra_traffic_bytes = PricingService.gb_to_bytes(int(topup.extra_traffic_gb))

        cycle_start_at, cycle_end_at = self._derive_cycle_bounds(subscription, now=self._now_utc())
        if getattr(subscription, 'traffic_cycle_base_bytes', None) in (None, 0):
            subscription.traffic_cycle_base_bytes = getattr(subscription, 'monthly_traffic_bytes', None)

        marzban_user = await self.marzban.topup_traffic(subscription.marzban_username, topup.extra_traffic_gb)
        self._sync_local_from_remote(subscription, marzban_user)

        subscription.traffic_cycle_start_at = cycle_start_at
        subscription.traffic_cycle_end_at = cycle_end_at
        subscription.cycle_extra_traffic_bytes = max(
            0,
            int(getattr(subscription, 'cycle_extra_traffic_bytes', 0) or 0) + int(extra_traffic_bytes),
        )
        subscription.next_traffic_reset_at = (
            cycle_end_at if subscription.expire_date is None or cycle_end_at < subscription.expire_date else None
        )
        subscription.data_limit_bytes = subscription.effective_cycle_total_bytes or marzban_user.data_limit
        subscription.subscription_url = self._resolve_subscription_url(subscription, marzban_user)
        subscription.notified_low_traffic = False
        subscription.notified_exhausted = False
        return marzban_user

    async def process_monthly_reset(self, subscription: Subscription) -> MarzbanUser | None:
        # Транзакционная блокировка подписки от гонки данных при фоновом ресете
        subscription = await self.subscriptions.get_by_id_for_update(subscription.id)
        if not subscription:
            return None

        now = self._now_utc()
        monthly_limit_bytes = getattr(subscription, 'monthly_traffic_bytes', None)
        if monthly_limit_bytes in (None, 0):
            return None

        cycle_due_at = self._normalize_utc(getattr(subscription, 'traffic_cycle_end_at', None))
        if cycle_due_at is None:
            cycle_due_at = self._normalize_utc(getattr(subscription, 'next_traffic_reset_at', None))

        if cycle_due_at is None or cycle_due_at > now:
            return None

        if subscription.expire_date and subscription.expire_date <= now:
            subscription.is_active = False
            subscription.next_traffic_reset_at = None
            subscription.traffic_cycle_end_at = None
            return None

        remote = await self.marzban.create_or_update_vless_user(
            username=subscription.marzban_username,
            expire_at=subscription.expire_date,
            data_limit_bytes=monthly_limit_bytes,
            online_limit=subscription.online_limit,
            note=getattr(subscription, 'current_tariff_code', None) or 'subscription_reset',
            reset_traffic=True,
            is_trial=False,
        )
        self._sync_local_from_remote(subscription, remote)

        next_cycle_start = cycle_due_at
        next_cycle_end = self._calculate_cycle_end(
            cycle_start_at=next_cycle_start,
            expire_at=subscription.expire_date,
        )
        while next_cycle_end <= now and (subscription.expire_date is None or next_cycle_start < subscription.expire_date):
            next_cycle_start = next_cycle_end
            next_cycle_end = self._calculate_cycle_end(
                cycle_start_at=next_cycle_start,
                expire_at=subscription.expire_date,
            )

        self._assign_cycle_state(
            subscription,
            cycle_start_at=next_cycle_start,
            cycle_end_at=next_cycle_end,
            cycle_base_bytes=monthly_limit_bytes,
            cycle_extra_traffic_bytes=0,
            next_reset_at=next_cycle_end if subscription.expire_date is None or next_cycle_end < subscription.expire_date else None,
            last_reset_at=now,
        )
        subscription.notified_low_traffic = False
        subscription.notified_exhausted = False
        subscription.subscription_url = self._resolve_subscription_url(subscription, remote)
        return remote