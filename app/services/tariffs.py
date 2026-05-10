from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from typing import TYPE_CHECKING, Any, Iterable

from app.db.repositories import (
    PricingRuleRepository,
    TariffRepository,
    TrafficTopupOptionRepository,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


RUB = Decimal('0.01')
_UNSET = object()


@dataclass(frozen=True, slots=True)
class TariffOption:
    code: str
    title: str
    monthly_traffic_gb: int | None
    tariff_id: int | None = None
    description: str | None = None
    badge_text: str | None = None
    is_active: bool = True
    is_public: bool = True
    is_archived: bool = False
    sort_order: int = 100
    pricing_mode: str = 'fixed'
    traffic_mode: str = 'fixed'
    device_mode: str = 'fixed'
    base_monthly_price: Decimal | None = None
    base_traffic_gb: int | None = None
    fixed_traffic_gb: int | None = None
    min_traffic_gb: int | None = None
    max_traffic_gb: int | None = None
    traffic_step_gb: int | None = None
    traffic_step_price: Decimal | None = None
    base_device_count: int | None = None
    fixed_device_count: int | None = None
    min_device_count: int | None = None
    max_device_count: int | None = None
    device_step: int | None = None
    device_step_price: Decimal | None = None
    allow_unlimited_devices: bool = False
    unlimited_devices_surcharge: Decimal | None = None
    period_options: tuple[int, ...] = (1,)
    legacy_price_single: Decimal | None = None
    legacy_price_unlimited: Decimal | None = None

    @property
    def supports_constructor_traffic(self) -> bool:
        return self.traffic_mode == 'constructor'

    @property
    def supports_constructor_devices(self) -> bool:
        return self.device_mode == 'constructor'

    @property
    def supports_unlimited_devices(self) -> bool:
        return self.device_mode == 'unlimited' or self.allow_unlimited_devices or self.legacy_price_unlimited is not None

    @property
    def supports_unlimited_traffic(self) -> bool:
        return self.traffic_mode == 'unlimited' or self.monthly_traffic_gb is None


@dataclass(frozen=True, slots=True)
class TariffBasket:
    plan: TariffOption
    months: int
    device_mode: str
    device_count: int
    device_label: str
    online_limit: int | None
    subtotal: Decimal
    discount_percent: Decimal
    total: Decimal
    balance_used: Decimal
    payable: Decimal
    monthly_traffic_gb: int | None
    effective_monthly_price: Decimal
    monthly_price_before_discount: Decimal


@dataclass(frozen=True, slots=True)
class TopUpOption:
    code: str
    title: str
    extra_traffic_gb: int
    amount: Decimal
    sort_order: int = 100
    badge_label: str | None = None
    is_best_price: bool = False

    @property
    def price_per_gb(self) -> Decimal:
        if self.extra_traffic_gb <= 0:
            return Decimal('0.00')
        return (self.amount / Decimal(self.extra_traffic_gb)).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP,
        )

    @property
    def display_badge(self) -> str | None:
        """Кастомный badge_label из БД имеет приоритет над авто-«лучшая цена»."""
        if self.badge_label:
            return self.badge_label
        if self.is_best_price:
            return '⭐ Лучшая цена/ГБ'
        return None


@dataclass(frozen=True, slots=True)
class TopUpBasket:
    topup: TopUpOption
    total: Decimal
    balance_used: Decimal
    payable: Decimal


@dataclass(frozen=True, slots=True)
class DeviceTopupQuote:
    """Снимок цены и режима mid-cycle апсейла устройства (FEA-A9).

    `monthly_extra_device_price` — стоимость одного дополнительного
    устройства в месяц по правилам/тарифу (без proration); `price` —
    итоговая сумма к оплате с учётом выбранного режима.
    """

    price: Decimal
    mode: str
    days_left: int
    days_in_cycle: int
    monthly_extra_device_price: Decimal
    fixed_price: Decimal
    current_device_mode: str
    current_device_count: int
    new_device_mode: str
    new_device_count: int
    online_limit: int


@dataclass(frozen=True, slots=True)
class PricingRuleSnapshot:
    base_price: Decimal = Decimal('100.00')
    base_traffic_gb: int = 250
    traffic_step_gb: int = 50
    traffic_step_price: Decimal = Decimal('40.00')
    device_step_price: Decimal = Decimal('20.00')
    unlimited_devices_price: Decimal = Decimal('100.00')
    unlimited_combo_price: Decimal = Decimal('1000.00')
    max_discount_percent: Decimal = Decimal('25.00')
    max_months: int = 12
    min_topup_amount: Decimal = Decimal('50.00')


def money(value: Decimal | str | int | float) -> Decimal:
    val = value if isinstance(value, Decimal) else Decimal(str(value))
    return val.quantize(RUB, rounding=ROUND_HALF_UP)


class PricingService:
    TRAFFIC_OPTIONS = [250, 300, 350, 400, 450, 500]
    MAX_CUSTOM_DEVICES = 5
    MIN_MONTHS = 1
    MAX_MONTHS = 12

    @classmethod
    async def get_rules(cls, session: AsyncSession | None = None) -> PricingRuleSnapshot:
        if session is None:
            return PricingRuleSnapshot()
        repo = PricingRuleRepository(session)
        row = await repo.get_or_create()
        return PricingRuleSnapshot(
            base_price=money(row.base_price),
            base_traffic_gb=int(row.base_traffic_gb),
            traffic_step_gb=max(1, int(row.traffic_step_gb)),
            traffic_step_price=money(row.traffic_step_price),
            device_step_price=money(row.device_step_price),
            unlimited_devices_price=money(row.unlimited_devices_price),
            unlimited_combo_price=money(row.unlimited_combo_price),
            max_discount_percent=money(row.max_discount_percent),
            max_months=max(1, int(row.max_months)),
            min_topup_amount=money(row.min_topup_amount),
        )

    @classmethod
    def package_code(cls, traffic_gb: int | None) -> str:
        return 'unlim' if traffic_gb is None else f't{int(traffic_gb)}'

    @classmethod
    def parse_package_code(cls, code: str) -> int | None:
        if code == 'unlim':
            return None
        if code.startswith('t'):
            try:
                return int(code[1:])
            except ValueError as exc:
                raise ValueError('Некорректный пакет трафика') from exc
        raise ValueError('Некорректный пакет трафика')

    @classmethod
    def normalize_traffic_gb(cls, traffic_gb: int) -> int:
        traffic_gb = int(traffic_gb)
        if traffic_gb <= cls.TRAFFIC_OPTIONS[0]:
            return cls.TRAFFIC_OPTIONS[0]
        if traffic_gb >= cls.TRAFFIC_OPTIONS[-1]:
            return cls.TRAFFIC_OPTIONS[-1]
        return min(cls.TRAFFIC_OPTIONS, key=lambda item: abs(item - traffic_gb))

    @classmethod
    def traffic_title(cls, traffic_gb: int | None) -> str:
        return 'Безлимит трафика + безлимит устройств' if traffic_gb is None else f'{traffic_gb} ГБ / месяц'

    @classmethod
    def device_label(cls, device_mode: str, device_count: int) -> str:
        if device_mode == 'single':
            return '1 устройство'
        if device_mode == 'unlimited':
            return 'Безлимит устройств'
        count = max(2, min(cls.MAX_CUSTOM_DEVICES, int(device_count or 2)))
        return f'{count} устройства' if 2 <= count <= 4 else f'{count} устройств'

    @classmethod
    def online_limit(cls, device_mode: str, device_count: int) -> int | None:
        if device_mode == 'single':
            return 1
        if device_mode == 'unlimited':
            return None
        return max(2, min(cls.MAX_CUSTOM_DEVICES, int(device_count or 2)))

    @staticmethod
    def discount_percent(months: int, max_discount_percent: Decimal) -> Decimal:
        months = max(1, int(months))
        percent = Decimal(months - 1) * Decimal('0.025')
        return min(percent, max_discount_percent / Decimal('100'))

    @staticmethod
    def _int_or_none(value: object) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _decimal_or_none(value: object) -> Decimal | None:
        if value is None:
            return None
        try:
            return money(Decimal(str(value)))
        except Exception:
            return None

    @staticmethod
    def _string_or_none(value: object) -> str | None:
        if value is None:
            return None
        raw = str(value).strip()
        return raw or None

    @staticmethod
    def _bool(value: object, *, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        return str(value).strip().lower() in {'1', 'true', 'yes', 'on'}

    @classmethod
    def _default_period_options(cls, rules: PricingRuleSnapshot | None = None) -> tuple[int, ...]:
        max_months = max(cls.MIN_MONTHS, int(rules.max_months if rules else cls.MAX_MONTHS))
        return tuple(range(cls.MIN_MONTHS, max_months + 1))

    @classmethod
    async def _load_period_options(
        cls,
        session: AsyncSession | None,
        repo: TariffRepository,
        row: object,
        rules: PricingRuleSnapshot | None = None,
    ) -> tuple[int, ...]:
        tariff_id = getattr(row, 'id', None)
        if session is None or tariff_id is None:
            return cls._default_period_options(rules)

        list_period_options = getattr(repo, 'list_period_options', None)
        if not callable(list_period_options):
            return cls._default_period_options(rules)

        try:
            options = await list_period_options(int(tariff_id))
        except Exception:
            return cls._default_period_options(rules)

        months = [
            int(getattr(item, 'months', 0))
            for item in options
            if cls._bool(getattr(item, 'is_enabled', True), default=True) and int(getattr(item, 'months', 0)) >= 1
        ]
        return tuple(months) if months else cls._default_period_options(rules)

    @classmethod
    async def _option_from_row(
        cls,
        session: AsyncSession | None,
        repo: TariffRepository,
        row: object,
        rules: PricingRuleSnapshot | None = None,
    ) -> TariffOption:
        legacy_traffic = cls._int_or_none(getattr(row, 'monthly_traffic_gb', None))
        pricing_mode = str(getattr(row, 'pricing_mode', 'fixed') or 'fixed')
        traffic_mode = str(
            getattr(
                row,
                'traffic_mode',
                'unlimited' if legacy_traffic is None else 'fixed',
            )
            or ('unlimited' if legacy_traffic is None else 'fixed')
        )
        device_mode = str(getattr(row, 'device_mode', 'fixed') or 'fixed')

        base_monthly_price = cls._decimal_or_none(getattr(row, 'base_monthly_price', None))
        legacy_price_single = cls._decimal_or_none(getattr(row, 'price_single', None))
        legacy_price_unlimited = cls._decimal_or_none(getattr(row, 'price_unlimited', None))
        if base_monthly_price is None:
            base_monthly_price = legacy_price_single

        fixed_traffic_gb = cls._int_or_none(getattr(row, 'fixed_traffic_gb', None))
        if fixed_traffic_gb is None and traffic_mode == 'fixed':
            fixed_traffic_gb = legacy_traffic

        base_traffic_gb = cls._int_or_none(getattr(row, 'base_traffic_gb', None))
        if base_traffic_gb is None:
            base_traffic_gb = fixed_traffic_gb or legacy_traffic

        base_device_count = cls._int_or_none(getattr(row, 'base_device_count', None))
        fixed_device_count = cls._int_or_none(getattr(row, 'fixed_device_count', None))
        if fixed_device_count is None and device_mode == 'fixed':
            fixed_device_count = cls._int_or_none(getattr(row, 'online_limit_single', None)) or 1
        if base_device_count is None:
            base_device_count = fixed_device_count or 1

        allow_unlimited_devices = cls._bool(getattr(row, 'allow_unlimited_devices', False))
        if legacy_price_unlimited is not None:
            allow_unlimited_devices = True

        unlimited_devices_surcharge = cls._decimal_or_none(getattr(row, 'unlimited_devices_surcharge', None))
        if unlimited_devices_surcharge is None and legacy_price_single is not None and legacy_price_unlimited is not None:
            unlimited_devices_surcharge = money(max(legacy_price_unlimited - legacy_price_single, Decimal('0.00')))

        monthly_traffic_gb = legacy_traffic
        if traffic_mode == 'unlimited':
            monthly_traffic_gb = None
        elif traffic_mode == 'fixed':
            monthly_traffic_gb = fixed_traffic_gb
        else:
            monthly_traffic_gb = base_traffic_gb

        period_options = await cls._load_period_options(session, repo, row, rules)

        return TariffOption(
            tariff_id=cls._int_or_none(getattr(row, 'id', None)),
            code=str(getattr(row, 'code')),
            title=str(getattr(row, 'title')),
            monthly_traffic_gb=monthly_traffic_gb,
            description=cls._string_or_none(getattr(row, 'description', None)),
            badge_text=cls._string_or_none(getattr(row, 'badge_text', None)),
            is_active=cls._bool(getattr(row, 'is_active', True), default=True),
            is_public=cls._bool(getattr(row, 'is_public', True), default=True),
            is_archived=cls._bool(getattr(row, 'is_archived', False), default=False),
            sort_order=cls._int_or_none(getattr(row, 'sort_order', 100)) or 100,
            pricing_mode=pricing_mode,
            traffic_mode=traffic_mode,
            device_mode=device_mode,
            base_monthly_price=base_monthly_price,
            base_traffic_gb=base_traffic_gb,
            fixed_traffic_gb=fixed_traffic_gb,
            min_traffic_gb=cls._int_or_none(getattr(row, 'min_traffic_gb', None)),
            max_traffic_gb=cls._int_or_none(getattr(row, 'max_traffic_gb', None)),
            traffic_step_gb=cls._int_or_none(getattr(row, 'traffic_step_gb', None)),
            traffic_step_price=cls._decimal_or_none(getattr(row, 'traffic_step_price', None)),
            base_device_count=base_device_count,
            fixed_device_count=fixed_device_count,
            min_device_count=cls._int_or_none(getattr(row, 'min_device_count', None)),
            max_device_count=cls._int_or_none(getattr(row, 'max_device_count', None)),
            device_step=cls._int_or_none(getattr(row, 'device_step', None)),
            device_step_price=cls._decimal_or_none(getattr(row, 'device_step_price', None)),
            allow_unlimited_devices=allow_unlimited_devices,
            unlimited_devices_surcharge=unlimited_devices_surcharge,
            period_options=period_options,
            legacy_price_single=legacy_price_single,
            legacy_price_unlimited=legacy_price_unlimited,
        )

    @classmethod
    def _legacy_option(cls, traffic_gb: int | None) -> TariffOption:
        if traffic_gb is None:
            return TariffOption(
                code=cls.package_code(None),
                title=cls.traffic_title(None),
                monthly_traffic_gb=None,
                pricing_mode='constructor',
                traffic_mode='unlimited',
                device_mode='unlimited',
                allow_unlimited_devices=True,
            )

        normalized_gb = cls.normalize_traffic_gb(traffic_gb)
        return TariffOption(
            code=cls.package_code(normalized_gb),
            title=cls.traffic_title(normalized_gb),
            monthly_traffic_gb=normalized_gb,
            pricing_mode='constructor',
            traffic_mode='fixed',
            device_mode='constructor',
            fixed_traffic_gb=normalized_gb,
            base_traffic_gb=normalized_gb,
            min_traffic_gb=normalized_gb,
            max_traffic_gb=normalized_gb,
            base_device_count=1,
            min_device_count=1,
            max_device_count=cls.MAX_CUSTOM_DEVICES,
            device_step=1,
            allow_unlimited_devices=True,
        )

    @classmethod
    def _fallback_legacy_catalog(cls) -> list[TariffOption]:
        return [cls._legacy_option(gb) for gb in cls.TRAFFIC_OPTIONS] + [cls._legacy_option(None)]

    @classmethod
    async def list_plans(cls, session: AsyncSession | None = None) -> list[TariffOption]:
        if session is None:
            return cls._fallback_legacy_catalog()

        repo = TariffRepository(session)
        list_public_active = getattr(repo, 'list_public_active', None)
        if callable(list_public_active):
            rows = await list_public_active()
        else:
            rows = await repo.list_active()

        if not rows:
            return cls._fallback_legacy_catalog()

        rules = await cls.get_rules(session)
        result = [await cls._option_from_row(session, repo, row, rules) for row in rows]
        result.sort(key=lambda item: (item.sort_order, item.tariff_id or 0, item.code))
        return result

    @classmethod
    async def list_archived_plans(cls, session: AsyncSession | None) -> list[TariffOption]:
        if session is None:
            return []
        repo = TariffRepository(session)
        list_archived = getattr(repo, 'list_archived', None)
        if not callable(list_archived):
            return []
        rows = await list_archived()
        rules = await cls.get_rules(session)
        return [await cls._option_from_row(session, repo, row, rules) for row in rows]

    @classmethod
    async def get_plan(cls, session: AsyncSession | None, code: str) -> TariffOption:
        if session is not None:
            repo = TariffRepository(session)
            row = await repo.get_by_code(code)
            if row is not None:
                rules = await cls.get_rules(session)
                return await cls._option_from_row(session, repo, row, rules)

        traffic_gb = cls.parse_package_code(code)
        if traffic_gb is not None:
            traffic_gb = cls.normalize_traffic_gb(traffic_gb)
        return cls._legacy_option(traffic_gb)

    @classmethod
    async def get_period_options(cls, session: AsyncSession | None, code: str) -> tuple[int, ...]:
        plan = await cls.get_plan(session, code)
        return plan.period_options or cls._default_period_options(await cls.get_rules(session) if session else None)

    @classmethod
    def _normalize_constructor_traffic(cls, plan: TariffOption, selected_traffic_gb: int | None) -> int:
        step = max(1, int(plan.traffic_step_gb or 50))
        min_gb = int(plan.min_traffic_gb or plan.base_traffic_gb or step)
        max_gb = int(plan.max_traffic_gb or max(min_gb, plan.base_traffic_gb or min_gb))
        target = int(selected_traffic_gb if selected_traffic_gb is not None else (plan.base_traffic_gb or min_gb))
        if target < min_gb:
            target = min_gb
        if target > max_gb:
            target = max_gb
        offset = target - min_gb
        snapped = min_gb + round(offset / step) * step
        if snapped < min_gb:
            snapped = min_gb
        if snapped > max_gb:
            snapped = max_gb
        return int(snapped)

    @classmethod
    def _normalize_constructor_devices(cls, plan: TariffOption, device_mode: str, device_count: int) -> tuple[str, int, int | None]:
        if device_mode == 'unlimited':
            if not plan.supports_unlimited_devices:
                raise ValueError('Этот тариф не поддерживает безлимит устройств')
            return 'unlimited', 0, None

        if plan.device_mode == 'fixed':
            fixed_count = max(1, int(plan.fixed_device_count or 1))
            if fixed_count == 1:
                return 'single', 1, 1
            return 'custom', fixed_count, fixed_count

        if plan.device_mode == 'unlimited':
            return 'unlimited', 0, None

        min_count = max(1, int(plan.min_device_count or plan.base_device_count or 1))
        max_count = max(min_count, int(plan.max_device_count or cls.MAX_CUSTOM_DEVICES))
        step = max(1, int(plan.device_step or 1))

        if device_mode == 'single':
            if min_count > 1:
                count = min_count
                return ('single' if count == 1 else 'custom', count, count)
            return 'single', 1, 1

        count = max(min_count, min(max_count, int(device_count or min_count)))
        offset = count - min_count
        snapped = min_count + round(offset / step) * step
        snapped = max(min_count, min(max_count, snapped))
        if snapped == 1:
            return 'single', 1, 1
        return 'custom', snapped, snapped

    @classmethod
    def _resolve_plan_selection(
        cls,
        plan: TariffOption,
        *,
        device_mode: str,
        device_count: int,
        selected_traffic_gb: int | None,
    ) -> tuple[int | None, str, int, int | None]:
        traffic_mode = plan.traffic_mode
        if traffic_mode == 'unlimited':
            traffic_gb = None
        elif traffic_mode == 'fixed':
            traffic_gb = plan.fixed_traffic_gb if plan.fixed_traffic_gb is not None else plan.monthly_traffic_gb
        else:
            traffic_gb = cls._normalize_constructor_traffic(plan, selected_traffic_gb)

        # legacy fallback: unlimited traffic forces unlimited devices
        if traffic_gb is None and device_mode != 'unlimited':
            if plan.supports_unlimited_devices or plan.device_mode == 'unlimited':
                device_mode = 'unlimited'
            else:
                raise ValueError('Безлимитный трафик доступен только с безлимитом устройств')

        if plan.device_mode == 'fixed' and (plan.fixed_device_count or 1) == 1:
            return traffic_gb, 'single', 1, 1
        if plan.device_mode == 'unlimited':
            return traffic_gb, 'unlimited', 0, None

        if plan.pricing_mode == 'fixed' and plan.legacy_price_single is not None and device_mode == 'single':
            return traffic_gb, 'single', 1, 1
        if plan.pricing_mode == 'fixed' and plan.legacy_price_unlimited is not None and device_mode == 'unlimited':
            return traffic_gb, 'unlimited', 0, None

        normalized_mode, normalized_count, online_limit = cls._normalize_constructor_devices(plan, device_mode, device_count)
        return traffic_gb, normalized_mode, normalized_count, online_limit

    @classmethod
    def calculate_monthly_price(
        cls,
        traffic_gb: int | None,
        device_mode: str,
        device_count: int,
        rules: PricingRuleSnapshot,
    ) -> Decimal:
        if traffic_gb is None:
            if device_mode != 'unlimited':
                raise ValueError('Безлимитный трафик доступен только с безлимитом устройств')
            return money(rules.unlimited_combo_price)

        traffic_gb = cls.normalize_traffic_gb(traffic_gb)
        traffic_steps = max(0, (traffic_gb - rules.base_traffic_gb) // rules.traffic_step_gb)
        traffic_surcharge = Decimal(traffic_steps) * rules.traffic_step_price

        if device_mode == 'single':
            device_surcharge = Decimal('0.00')
        elif device_mode == 'unlimited':
            device_surcharge = rules.unlimited_devices_price
        else:
            normalized_count = max(2, min(cls.MAX_CUSTOM_DEVICES, int(device_count or 2)))
            device_surcharge = min(Decimal(normalized_count - 1) * rules.device_step_price, rules.unlimited_devices_price)

        return money(rules.base_price + traffic_surcharge + device_surcharge)

    @classmethod
    def calculate_plan_monthly_price(
        cls,
        plan: TariffOption,
        *,
        traffic_gb: int | None,
        device_mode: str,
        device_count: int,
        rules: PricingRuleSnapshot,
    ) -> Decimal:
        # Legacy fixed plans keep explicit prices for single/unlimited devices.
        if plan.pricing_mode == 'fixed' and plan.legacy_price_single is not None and device_mode == 'single':
            return money(plan.legacy_price_single)
        if plan.pricing_mode == 'fixed' and plan.legacy_price_unlimited is not None and device_mode == 'unlimited':
            return money(plan.legacy_price_unlimited)

        base = money(plan.base_monthly_price or plan.legacy_price_single or rules.base_price)

        if traffic_gb is None:
            if device_mode != 'unlimited':
                raise ValueError('Безлимитный трафик доступен только с безлимитом устройств')
        elif plan.traffic_mode == 'constructor':
            base_traffic = int(plan.base_traffic_gb or plan.min_traffic_gb or traffic_gb)
            step = max(1, int(plan.traffic_step_gb or rules.traffic_step_gb))
            step_price = money(plan.traffic_step_price or rules.traffic_step_price)
            traffic_delta = max(0, int(traffic_gb) - base_traffic)
            traffic_steps = traffic_delta // step
            base = money(base + (Decimal(traffic_steps) * step_price))

        if plan.device_mode == 'constructor' and device_mode == 'custom':
            base_devices = max(1, int(plan.base_device_count or plan.min_device_count or 1))
            step = max(1, int(plan.device_step or 1))
            step_price = money(plan.device_step_price or rules.device_step_price)
            delta = max(0, int(device_count) - base_devices)
            steps = delta // step
            base = money(base + (Decimal(steps) * step_price))
        elif device_mode == 'unlimited':
            surcharge = plan.unlimited_devices_surcharge
            if surcharge is None:
                surcharge = rules.unlimited_devices_price if traffic_gb is not None else rules.unlimited_combo_price - rules.base_price
            base = money(base + money(surcharge))

        return money(base)

    @classmethod
    async def calculate_tariff_basket(
        cls,
        *,
        session: AsyncSession | None,
        plan_code: str,
        months: int,
        user_balance: Decimal,
        use_balance: bool,
        device_mode: str,
        device_count: int,
        selected_traffic_gb: int | None = None,
    ) -> TariffBasket:
        rules = await cls.get_rules(session)
        plan = await cls.get_plan(session, plan_code)

        allowed_periods = tuple(sorted({m for m in plan.period_options if int(m) >= 1})) or cls._default_period_options(rules)
        requested_months = max(cls.MIN_MONTHS, min(int(months), max(allowed_periods)))
        months = requested_months if requested_months in allowed_periods else min(allowed_periods, key=lambda item: abs(item - requested_months))

        traffic_gb, normalized_device_mode, normalized_device_count, online_limit = cls._resolve_plan_selection(
            plan,
            device_mode=device_mode,
            device_count=device_count,
            selected_traffic_gb=selected_traffic_gb,
        )

        monthly_price = cls.calculate_plan_monthly_price(
            plan,
            traffic_gb=traffic_gb,
            device_mode=normalized_device_mode,
            device_count=normalized_device_count,
            rules=rules,
        )
        subtotal = money(monthly_price * months)
        discount = cls.discount_percent(months, rules.max_discount_percent)
        total = money(subtotal * (Decimal('1.0') - discount))
        balance_used = money(min(user_balance, total) if use_balance else Decimal('0.00'))
        payable = money(total - balance_used)
        effective = money(total / Decimal(months))

        return TariffBasket(
            plan=plan,
            months=months,
            device_mode=normalized_device_mode,
            device_count=normalized_device_count,
            device_label=cls.device_label(normalized_device_mode, normalized_device_count),
            online_limit=online_limit,
            subtotal=subtotal,
            discount_percent=discount,
            total=total,
            balance_used=balance_used,
            payable=payable,
            monthly_traffic_gb=traffic_gb,
            effective_monthly_price=effective,
            monthly_price_before_discount=monthly_price,
        )

    @classmethod
    def basket_snapshot(cls, basket: TariffBasket) -> dict[str, object]:
        return {
            'tariff_plan_id': basket.plan.tariff_id,
            'tariff_code': basket.plan.code,
            'tariff_title': basket.plan.title,
            'description': basket.plan.description,
            'badge_text': basket.plan.badge_text,
            'pricing_mode': basket.plan.pricing_mode,
            'traffic_mode': basket.plan.traffic_mode,
            'device_mode': basket.plan.device_mode,
            'months': basket.months,
            'period_options': list(basket.plan.period_options),
            'selected_device_mode': basket.device_mode,
            'selected_device_count': basket.device_count,
            'device_label': basket.device_label,
            'online_limit': basket.online_limit,
            'monthly_traffic_gb': basket.monthly_traffic_gb,
            'monthly_price_before_discount': str(money(basket.monthly_price_before_discount)),
            'subtotal': str(money(basket.subtotal)),
            'discount_percent': str(basket.discount_percent),
            'total': str(money(basket.total)),
            'balance_used': str(money(basket.balance_used)),
            'payable': str(money(basket.payable)),
        }

    @staticmethod
    def gb_to_bytes(gb: int) -> int:
        return int(gb) * 1024 * 1024 * 1024

    @classmethod
    async def list_topups(
        cls, session: 'AsyncSession', *, only_enabled: bool = True,
    ) -> list[TopUpOption]:
        """Возвращает доступные пакеты докупки трафика из БД (FEA-A8).

        `is_best_price` проставляется опции с минимальной ценой за ГБ среди
        возвращённого набора (auto-бейдж «⭐ Лучшая цена/ГБ», если у опции
        нет своего `badge_label`)."""
        repo = TrafficTopupOptionRepository(session)
        rows = await repo.list_enabled() if only_enabled else await repo.list_all()
        if not rows:
            return []

        bare = [
            TopUpOption(
                code=row.code,
                title=row.title,
                extra_traffic_gb=int(row.extra_traffic_gb),
                amount=money(row.amount),
                sort_order=int(row.sort_order),
                badge_label=row.badge_label,
                is_best_price=False,
            )
            for row in rows
        ]

        # «Лучшая цена/ГБ» — только если есть хотя бы 2 опции и победитель
        # уникален (иначе бейдж не помогает выбору).
        if len(bare) >= 2:
            ppg = [opt.price_per_gb for opt in bare]
            min_ppg = min(ppg)
            winners = [i for i, value in enumerate(ppg) if value == min_ppg]
            if len(winners) == 1:
                idx = winners[0]
                bare[idx] = TopUpOption(
                    code=bare[idx].code,
                    title=bare[idx].title,
                    extra_traffic_gb=bare[idx].extra_traffic_gb,
                    amount=bare[idx].amount,
                    sort_order=bare[idx].sort_order,
                    badge_label=bare[idx].badge_label,
                    is_best_price=True,
                )
        return bare

    @classmethod
    async def get_topup(cls, session: 'AsyncSession', code: str) -> TopUpOption:
        repo = TrafficTopupOptionRepository(session)
        row = await repo.get_by_code(code)
        if row is None or not row.is_enabled:
            raise ValueError('Пакет трафика не найден или отключён')
        # Для одиночного резолва бейдж best-price не вычисляется — для этого
        # вызывайте `list_topups()`.
        return TopUpOption(
            code=row.code,
            title=row.title,
            extra_traffic_gb=int(row.extra_traffic_gb),
            amount=money(row.amount),
            sort_order=int(row.sort_order),
            badge_label=row.badge_label,
            is_best_price=False,
        )

    @classmethod
    async def min_topup_amount(cls, session: AsyncSession | None = None) -> Decimal:
        rules = await cls.get_rules(session)
        return rules.min_topup_amount

    @classmethod
    async def calculate_topup_basket(
        cls,
        session: 'AsyncSession',
        topup_code: str,
        user_balance: Decimal,
        use_balance: bool,
    ) -> TopUpBasket:
        topup = await cls.get_topup(session, topup_code)
        total = money(topup.amount)
        balance_used = money(min(user_balance, total) if use_balance else Decimal('0.00'))
        payable = money(total - balance_used)
        return TopUpBasket(topup=topup, total=total, balance_used=balance_used, payable=payable)

    @classmethod
    def calculate_reset_price(cls, monthly_price: Decimal, days_left_in_month: int, days_in_month: int) -> Decimal:
        if days_in_month <= 0:
            return money(monthly_price)

        safe_days_left = max(0, min(int(days_left_in_month), int(days_in_month)))
        ratio = Decimal(safe_days_left) / Decimal(days_in_month)
        raw = Decimal(monthly_price) * ratio
        return money(raw.quantize(Decimal('1'), rounding=ROUND_CEILING))

    @classmethod
    async def quote_device_topup(
        cls,
        *,
        session: 'AsyncSession',
        current_device_mode: str,
        current_device_count: int | None,
        days_left: int,
        days_in_cycle: int,
        price_mode: str,
        fixed_price: Decimal,
        plan: TariffOption | None = None,
    ) -> DeviceTopupQuote:
        """Расчёт цены добавления одного устройства до конца цикла (FEA-A9).

        `prorated`: monthly_extra_device_price * days_left / days_in_cycle,
        с округлением вверх до рубля (как `calculate_reset_price`).
        `fixed`: fixed_price as is.
        Источник `monthly_extra_device_price` — `plan.device_step_price`,
        если задан, иначе `rules.device_step_price`.
        """
        normalized_mode = (current_device_mode or '').strip().lower()
        if normalized_mode not in {'single', 'custom'}:
            raise ValueError(
                'Добавление устройства недоступно для безлимитного режима устройств.'
            )

        existing_count = 1 if normalized_mode == 'single' else max(2, int(current_device_count or 0))
        new_device_count = existing_count + 1
        if new_device_count > cls.MAX_CUSTOM_DEVICES:
            raise ValueError(
                f'Достигнут максимум устройств ({cls.MAX_CUSTOM_DEVICES}).'
            )

        rules = await cls.get_rules(session)
        plan_step_price = getattr(plan, 'device_step_price', None) if plan is not None else None
        monthly_extra_device_price = money(plan_step_price or rules.device_step_price)

        normalized_price_mode = (price_mode or 'prorated').strip().lower()
        if normalized_price_mode not in {'prorated', 'fixed'}:
            normalized_price_mode = 'prorated'

        normalized_fixed_price = money(fixed_price or Decimal('0.00'))

        if normalized_price_mode == 'fixed':
            price = normalized_fixed_price
        else:
            price = cls.calculate_reset_price(
                monthly_extra_device_price,
                days_left_in_month=days_left,
                days_in_month=days_in_cycle,
            )

        return DeviceTopupQuote(
            price=price,
            mode=normalized_price_mode,
            days_left=int(days_left),
            days_in_cycle=int(days_in_cycle),
            monthly_extra_device_price=monthly_extra_device_price,
            fixed_price=normalized_fixed_price,
            current_device_mode=normalized_mode,
            current_device_count=existing_count,
            new_device_mode='custom',
            new_device_count=new_device_count,
            online_limit=new_device_count,
        )
