from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import (
    AuditAction,
    AuditActorType,
    Invoice,
    InvoicePurpose,
    InvoiceStatus,
    Subscription,
    TransactionType,
    User,
)
from app.db.repositories import (
    AuditLogRepository,
    InvoiceRepository,
    SubscriptionRepository,
    TransactionRepository,
    UserRepository,
)
from app.observability.metrics import PAYMENTS_CONSUMED, PAYMENTS_CREATED, PAYMENTS_FAILED
from app.services.idempotency import build_invoice_idempotency_key
from app.services.payments.base import PaymentProvider, PaymentProviderError
from app.services.referrals import ReferralService
from app.services.subscriptions import SubscriptionService
from app.services.tariffs import PricingService

RUB = Decimal('0.01')
_ALLOWED_DEVICE_MODES = {'single', 'custom', 'unlimited'}
_IDEMPOTENCY_INDEX_NAME = 'uq_invoices_idempotency_key'


class DuplicateInvoiceError(Exception):
    """Raised when invoice creation collides on its idempotency_key.

    Indicates the same purchase intent was submitted twice within the
    bucket window (typical cause: double-click on the buy button).
    """

    def __init__(self, idempotency_key: str) -> None:
        super().__init__(f'Duplicate invoice intent (key={idempotency_key})')
        self.idempotency_key = idempotency_key


@dataclass(slots=True)
class ProcessInvoiceResult:
    invoice: Invoice
    already_processed: bool
    status_text: str


class PaymentService:
    _CANCELLED_RESULT_STATUS = 'cancelled_ignored'

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        payments: PaymentProvider,
        subscription_service: SubscriptionService,
    ) -> None:
        self.session = session
        self.settings = settings
        self.payments = payments
        self.subscription_service = subscription_service
        self.users = UserRepository(session)
        self.invoices = InvoiceRepository(session)
        self.subscriptions = SubscriptionRepository(session)
        self.transactions = TransactionRepository(session)
        self.referrals = ReferralService(session)
        self.audit = AuditLogRepository(session)

    @staticmethod
    def _money(value: Decimal | str | int | float) -> Decimal:
        value = value if isinstance(value, Decimal) else Decimal(str(value))
        return value.quantize(RUB, rounding=ROUND_HALF_UP)

    @staticmethod
    def _normalize_utc(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @classmethod
    def _serialize_datetime(cls, value: datetime | None) -> str | None:
        normalized = cls._normalize_utc(value)
        return normalized.isoformat() if normalized is not None else None

    @staticmethod
    def _subscription_is_trial(subscription: Subscription) -> bool:
        return bool(getattr(subscription, 'is_trial', False))

    @staticmethod
    def _subscription_is_unlimited(subscription: Subscription) -> bool:
        return getattr(subscription, 'monthly_traffic_bytes', None) in (None, 0)

    @staticmethod
    def _normalize_device_mode(device_mode: str) -> str:
        normalized = (device_mode or '').strip().lower()
        if normalized not in _ALLOWED_DEVICE_MODES:
            raise ValueError('Некорректный режим устройств')
        return normalized

    @classmethod
    def _normalize_device_count(cls, *, device_mode: str, device_count: int | None) -> int:
        normalized_mode = cls._normalize_device_mode(device_mode)
        raw_count = int(device_count or 0)
        if normalized_mode == 'single':
            return 1
        if normalized_mode == 'custom':
            return max(2, min(PricingService.MAX_CUSTOM_DEVICES, raw_count))
        return 0

    @staticmethod
    def _coerce_int(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _normalize_optional_str(value: Any) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    @staticmethod
    def _snapshot_value(snapshot: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            if key in snapshot and snapshot[key] is not None:
                return snapshot[key]
        return None

    @classmethod
    def _require_positive_months(cls, months: int) -> int:
        normalized = int(months or 0)
        if normalized <= 0:
            raise ValueError('Количество месяцев должно быть больше нуля')
        return normalized

    @classmethod
    def _require_positive_amount(cls, amount: Decimal | str | int | float, *, field_label: str) -> Decimal:
        normalized = cls._money(amount)
        if normalized <= 0:
            raise ValueError(f'{field_label} должна быть больше нуля')
        return normalized

    @classmethod
    def _build_subscription_snapshot_payload(cls, subscription: Subscription) -> dict[str, Any]:
        return {
            'subscription_id': subscription.id,
            'subscription_service_id': subscription.service_id,
            'subscription_marzban_username': subscription.marzban_username,
            'traffic_cycle_start_at': cls._serialize_datetime(getattr(subscription, 'traffic_cycle_start_at', None)),
            'traffic_cycle_end_at': cls._serialize_datetime(getattr(subscription, 'traffic_cycle_end_at', None)),
            'last_traffic_reset_at': cls._serialize_datetime(getattr(subscription, 'last_traffic_reset_at', None)),
            'monthly_traffic_bytes': getattr(subscription, 'monthly_traffic_bytes', None),
            'traffic_cycle_base_bytes': getattr(subscription, 'traffic_cycle_base_bytes', None),
            'cycle_extra_traffic_bytes_before': int(getattr(subscription, 'cycle_extra_traffic_bytes', 0) or 0),
            'effective_cycle_total_bytes_before': getattr(subscription, 'effective_cycle_total_bytes', None),
        }

    def _normalize_invoice_amounts(self, invoice: Invoice) -> None:
        invoice.amount = self._money(invoice.amount)
        invoice.balance_used = self._money(invoice.balance_used)
        invoice.payable_amount = self._money(invoice.payable_amount)

    @staticmethod
    def _normalize_provider_status(status: Any) -> str | None:
        normalized = str(status or '').strip().lower()
        if not normalized:
            return None
        if normalized in {'paid', 'success', 'succeeded', 'confirmed'}:
            return 'paid'
        if normalized in {'cancelled', 'canceled', 'failed', 'error', 'expired', 'chargebacked'}:
            return 'cancelled'
        if normalized in {'pending', 'processing', 'created', 'new'}:
            return 'pending'
        return normalized

    @staticmethod
    def _provider_supports_transaction_snapshot(provider: PaymentProvider) -> bool:
        return callable(getattr(provider, 'get_transaction_snapshot', None))

    @classmethod
    def _provider_snapshot_status(cls, snapshot: Any) -> str | None:
        if snapshot is None:
            return None
        if isinstance(snapshot, dict):
            return cls._normalize_provider_status(snapshot.get('status'))
        return cls._normalize_provider_status(getattr(snapshot, 'status', None))

    @classmethod
    def _provider_snapshot_raw_status(cls, snapshot: Any) -> str | None:
        if snapshot is None:
            return None
        if isinstance(snapshot, dict):
            return cls._normalize_optional_str(snapshot.get('raw_status') or snapshot.get('status'))
        return cls._normalize_optional_str(
            getattr(snapshot, 'raw_status', None) or getattr(snapshot, 'status', None)
        )

    @classmethod
    def _provider_snapshot_transaction_id(cls, snapshot: Any) -> str | None:
        if snapshot is None:
            return None
        if isinstance(snapshot, dict):
            return cls._normalize_optional_str(
                snapshot.get('transaction_id')
                or snapshot.get('invoice_id')
                or snapshot.get('external_invoice_id')
                or snapshot.get('id')
            )
        return cls._normalize_optional_str(
            getattr(snapshot, 'transaction_id', None)
            or getattr(snapshot, 'invoice_id', None)
            or getattr(snapshot, 'external_invoice_id', None)
            or getattr(snapshot, 'id', None)
        )

    @classmethod
    def _provider_snapshot_payment_url(cls, snapshot: Any) -> str | None:
        if snapshot is None:
            return None
        if isinstance(snapshot, dict):
            return cls._normalize_optional_str(snapshot.get('payment_url') or snapshot.get('redirect') or snapshot.get('url'))
        return cls._normalize_optional_str(
            getattr(snapshot, 'payment_url', None)
            or getattr(snapshot, 'redirect', None)
            or getattr(snapshot, 'url', None)
        )

    def _apply_provider_snapshot_to_invoice(self, invoice: Invoice, snapshot: Any) -> None:
        if snapshot is None:
            return

        transaction_id = self._provider_snapshot_transaction_id(snapshot)
        if transaction_id:
            if invoice.external_invoice_id and invoice.external_invoice_id != transaction_id:
                raise PaymentProviderError(
                    'Платежный сервис вернул другой transactionId для уже созданного счета'
                )
            invoice.external_invoice_id = transaction_id

        payment_url = self._provider_snapshot_payment_url(snapshot)
        if payment_url:
            invoice.payment_url = payment_url

    async def _refresh_provider_invoice(self, invoice: Invoice, previous_amount: Decimal | None = None) -> None:
        if invoice.external_invoice_id and previous_amount is not None and previous_amount == invoice.payable_amount:
            return

        if invoice.payable_amount <= 0:
            invoice.external_invoice_id = None
            invoice.payment_url = None
            return

        description = None
        if isinstance(invoice.payload_json, dict):
            description = self._normalize_optional_str(invoice.payload_json.get('description'))

        if not description:
            snapshot = getattr(invoice, 'tariff_snapshot_json', None)
            if isinstance(snapshot, dict):
                tariff_title = self._normalize_optional_str(snapshot.get('tariff_title') or snapshot.get('title'))
                months = self._coerce_int(snapshot.get('months'))
                if tariff_title and months:
                    description = f'Оплата тарифа {tariff_title} на {months} мес.'
                elif tariff_title:
                    description = f'Оплата тарифа {tariff_title}'

        provider_invoice = await self.payments.create_invoice(
            self._money(invoice.payable_amount),
            payload={
                'invoice_id': invoice.id,
                'purpose': invoice.purpose.value,
                'user_id': invoice.user_id,
                'description': description,
                'tariff_plan_id': getattr(invoice, 'tariff_plan_id', None),
            },
        )
        normalized_external_id = self._normalize_optional_str(provider_invoice.invoice_id)
        if not normalized_external_id:
            raise PaymentProviderError('Платежный сервис не вернул идентификатор счета')
        if invoice.external_invoice_id and invoice.external_invoice_id != normalized_external_id:
            raise PaymentProviderError('Нельзя перезаписать внешний идентификатор уже созданного счета')

        invoice.external_invoice_id = normalized_external_id
        invoice.payment_url = self._normalize_optional_str(provider_invoice.payment_url)
        provider_status = self._normalize_provider_status(getattr(provider_invoice, 'status', None))
        if provider_status == 'cancelled':
            raise PaymentProviderError('Платежный сервис вернул сразу отмененный счет')

    async def _resolve_user_subscription(
        self,
        *,
        user: User,
        subscription_id: int | None,
        require_active: bool = False,
    ) -> Subscription | None:
        if subscription_id is not None:
            subscription = await self.subscriptions.get_by_id(subscription_id)
            if subscription is None or subscription.user_id != user.id:
                raise ValueError('Подписка не найдена')
            if require_active and not subscription.is_alive_local:
                raise ValueError('Активная подписка не найдена')
            return subscription

        subscription = await self.subscriptions.get_latest_active(user.id)
        if require_active and subscription is None:
            raise ValueError('Активная подписка не найдена')
        return subscription

    async def _validate_tariff_target(
        self,
        *,
        user: User,
        subscription_id: int | None,
        early_renewal: bool,
    ) -> int | None:
        if not early_renewal:
            if subscription_id is None:
                return None
            subscription = await self._resolve_user_subscription(
                user=user,
                subscription_id=subscription_id,
                require_active=False,
            )
            return subscription.id if subscription is not None else None

        if subscription_id is None:
            raise ValueError('Для досрочного продления нужно выбрать действующую услугу')

        subscription = await self._resolve_user_subscription(
            user=user,
            subscription_id=subscription_id,
            require_active=True,
        )
        if subscription is None:
            raise ValueError('Активная подписка не найдена')
        if self._subscription_is_trial(subscription):
            raise ValueError('Тестовую подписку нельзя продлить')

        return subscription.id

    async def _validate_topup_target(self, *, user: User, subscription_id: int | None) -> Subscription:
        subscription = await self._resolve_user_subscription(
            user=user,
            subscription_id=subscription_id,
            require_active=True,
        )
        if subscription is None:
            raise ValueError('У пользователя нет активной услуги для докупки трафика')
        if self._subscription_is_trial(subscription):
            raise ValueError('Для тестовой подписки докупка трафика недоступна.')
        if self._subscription_is_unlimited(subscription):
            raise ValueError('Для безлимитного тарифа докупка трафика не требуется и недоступна.')
        return subscription

    async def _mark_invoice_applying(self, invoice: Invoice) -> None:
        if invoice.status != InvoiceStatus.applying:
            invoice.status = InvoiceStatus.applying
            await self.session.flush()

    async def _create_invoice(
        self,
        *,
        user_id: int,
        purpose: InvoicePurpose,
        amount: Decimal,
        balance_used: Decimal,
        payable_amount: Decimal,
        provider: str,
        payload_json: dict[str, Any],
        currency: str = 'RUB',
        tariff_plan_id: int | None = None,
        tariff_snapshot_json: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Invoice:
        create_kwargs = {
            'user_id': user_id,
            'purpose': purpose,
            'amount': self._money(amount),
            'balance_used': self._money(balance_used),
            'payable_amount': self._money(payable_amount),
            'provider': provider,
            'payload_json': payload_json,
            'currency': currency,
            'idempotency_key': idempotency_key,
        }
        if tariff_plan_id is not None or tariff_snapshot_json is not None:
            create_kwargs['tariff_plan_id'] = tariff_plan_id
            create_kwargs['tariff_snapshot_json'] = tariff_snapshot_json

        try:
            invoice = await self.invoices.create(**create_kwargs)
        except IntegrityError as exc:
            if idempotency_key is not None and _IDEMPOTENCY_INDEX_NAME in str(exc.orig):
                await self.session.rollback()
                raise DuplicateInvoiceError(idempotency_key) from exc
            raise
        except TypeError:
            create_kwargs.pop('tariff_plan_id', None)
            create_kwargs.pop('tariff_snapshot_json', None)
            create_kwargs.pop('idempotency_key', None)
            invoice = await self.invoices.create(**create_kwargs)
            if tariff_plan_id is not None and hasattr(invoice, 'tariff_plan_id'):
                setattr(invoice, 'tariff_plan_id', tariff_plan_id)
            if tariff_snapshot_json is not None and hasattr(invoice, 'tariff_snapshot_json'):
                setattr(invoice, 'tariff_snapshot_json', dict(tariff_snapshot_json))
            await self.session.flush()

        return invoice

    async def _calculate_tariff_basket(
        self,
        *,
        package_code: str,
        months: int,
        user_balance: Decimal,
        use_balance: bool,
        device_mode: str,
        device_count: int,
        selected_traffic_gb: int | None,
    ):
        try:
            return await PricingService.calculate_tariff_basket(
                session=self.session,
                plan_code=package_code,
                months=months,
                user_balance=user_balance,
                use_balance=use_balance,
                device_mode=device_mode,
                device_count=device_count,
                selected_traffic_gb=selected_traffic_gb,
            )
        except TypeError:
            return await PricingService.calculate_tariff_basket(
                session=self.session,
                plan_code=package_code,
                months=months,
                user_balance=user_balance,
                use_balance=use_balance,
                device_mode=device_mode,
                device_count=device_count,
            )

    def _build_tariff_snapshot(self, basket: Any) -> tuple[int | None, dict[str, Any]]:
        snapshot_builder = getattr(PricingService, 'basket_snapshot', None)
        if callable(snapshot_builder):
            snapshot = snapshot_builder(basket)
            if not isinstance(snapshot, dict):
                snapshot = {}
        else:
            snapshot = {}

        plan = getattr(basket, 'plan', None)
        tariff_plan_id = self._coerce_int(
            self._snapshot_value(snapshot, 'tariff_plan_id')
            or getattr(plan, 'tariff_id', None)
            or getattr(plan, 'id', None)
        )

        plan_code = self._normalize_optional_str(
            self._snapshot_value(snapshot, 'tariff_code')
            or getattr(plan, 'code', None)
        )
        plan_title = self._normalize_optional_str(
            self._snapshot_value(snapshot, 'tariff_title', 'title')
            or getattr(plan, 'title', None)
        )

        effective_monthly_price = self._money(
            self._snapshot_value(snapshot, 'effective_monthly_price')
            or getattr(basket, 'effective_monthly_price', None)
            or getattr(basket, 'monthly_price', None)
            or Decimal('0')
        )
        total = self._money(
            self._snapshot_value(snapshot, 'total')
            or getattr(basket, 'total', None)
            or Decimal('0')
        )
        payable = self._money(
            self._snapshot_value(snapshot, 'payable')
            or getattr(basket, 'payable', None)
            or total
        )
        subtotal = self._money(
            self._snapshot_value(snapshot, 'subtotal')
            or getattr(basket, 'subtotal', None)
            or total
        )
        discount_percent = self._money(
            self._snapshot_value(snapshot, 'discount_percent')
            or getattr(basket, 'discount_percent', None)
            or Decimal('0')
        )

        normalized_snapshot: dict[str, Any] = dict(snapshot)
        normalized_snapshot.update(
            {
                'tariff_plan_id': tariff_plan_id,
                'tariff_code': plan_code,
                'tariff_title': plan_title,
                'description': self._normalize_optional_str(
                    self._snapshot_value(snapshot, 'description') or getattr(plan, 'description', None)
                ),
                'badge_text': self._normalize_optional_str(
                    self._snapshot_value(snapshot, 'badge_text') or getattr(plan, 'badge_text', None)
                ),
                'pricing_mode': self._normalize_optional_str(
                    self._snapshot_value(snapshot, 'pricing_mode') or getattr(plan, 'pricing_mode', None)
                ),
                'traffic_mode': self._normalize_optional_str(
                    self._snapshot_value(snapshot, 'traffic_mode') or getattr(plan, 'traffic_mode', None)
                ),
                'device_mode': self._normalize_optional_str(
                    self._snapshot_value(snapshot, 'device_mode') or getattr(plan, 'device_mode', None)
                ),
                'months': int(getattr(basket, 'months', self._coerce_int(self._snapshot_value(snapshot, 'months')) or 1)),
                'selected_device_mode': self._normalize_optional_str(
                    self._snapshot_value(snapshot, 'selected_device_mode') or getattr(basket, 'device_mode', None)
                ),
                'selected_device_count': int(
                    getattr(basket, 'device_count', self._coerce_int(self._snapshot_value(snapshot, 'selected_device_count')) or 0)
                ),
                'device_label': self._normalize_optional_str(
                    self._snapshot_value(snapshot, 'device_label') or getattr(basket, 'device_label', None)
                ),
                'online_limit': self._coerce_int(
                    getattr(basket, 'online_limit', None)
                    if getattr(basket, 'online_limit', None) is not None
                    else self._snapshot_value(snapshot, 'online_limit')
                ),
                'selected_traffic_gb': self._coerce_int(
                    self._snapshot_value(snapshot, 'selected_traffic_gb')
                    or getattr(basket, 'monthly_traffic_gb', None)
                ),
                'monthly_traffic_gb': self._coerce_int(
                    self._snapshot_value(snapshot, 'monthly_traffic_gb')
                    or getattr(basket, 'monthly_traffic_gb', None)
                ),
                'period_options': list(self._snapshot_value(snapshot, 'period_options') or getattr(plan, 'period_options', ()) or ()),
                'discount_percent': str(discount_percent),
                'subtotal': str(subtotal),
                'effective_monthly_price': str(effective_monthly_price),
                'monthly_price': str(effective_monthly_price),
                'total': str(total),
                'payable': str(payable),
            }
        )
        return tariff_plan_id, normalized_snapshot

    def _build_tariff_invoice_payload(
        self,
        *,
        basket: Any,
        package_code: str,
        months: int,
        device_mode: str,
        early_renewal: bool,
        target_subscription: Subscription | None,
        tariff_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        snapshot_monthly_price = self._money(
            tariff_snapshot.get('effective_monthly_price') or tariff_snapshot.get('monthly_price') or Decimal('0')
        )
        snapshot_subtotal = self._money(tariff_snapshot.get('subtotal') or getattr(basket, 'subtotal', Decimal('0')))
        snapshot_discount_percent = self._money(tariff_snapshot.get('discount_percent') or getattr(basket, 'discount_percent', Decimal('0')))
        snapshot_title = self._normalize_optional_str(tariff_snapshot.get('tariff_title') or tariff_snapshot.get('title'))
        snapshot_device_mode = self._normalize_optional_str(
            tariff_snapshot.get('selected_device_mode') or getattr(basket, 'device_mode', None) or device_mode
        )
        snapshot_device_count = int(
            tariff_snapshot.get('selected_device_count')
            or getattr(basket, 'device_count', 0)
            or 0
        )
        snapshot_traffic_gb = self._coerce_int(
            tariff_snapshot.get('selected_traffic_gb')
            or tariff_snapshot.get('monthly_traffic_gb')
            or getattr(basket, 'monthly_traffic_gb', None)
        )
        snapshot_online_limit = self._coerce_int(
            tariff_snapshot.get('online_limit')
            or getattr(basket, 'online_limit', None)
        )

        description = snapshot_title or package_code
        return {
            'kind': 'tariff',
            'description': f'Оплата тарифа {description} на {months} мес.',
            'package_code': package_code,
            'package_title': snapshot_title,
            'tariff_plan_id': tariff_snapshot.get('tariff_plan_id'),
            'tariff_code': tariff_snapshot.get('tariff_code') or package_code,
            'months': months,
            'device_mode': snapshot_device_mode,
            'device_count': snapshot_device_count,
            'device_label': self._normalize_optional_str(
                tariff_snapshot.get('device_label') or getattr(basket, 'device_label', None)
            ),
            'discount_percent': str(snapshot_discount_percent),
            'subtotal': str(snapshot_subtotal),
            'monthly_price': str(snapshot_monthly_price),
            'monthly_traffic_gb': snapshot_traffic_gb,
            'selected_traffic_gb': snapshot_traffic_gb,
            'online_limit': snapshot_online_limit,
            'early_renewal': early_renewal,
            'subscription_id': target_subscription.id if target_subscription is not None else None,
            'subscription_service_id': target_subscription.service_id if target_subscription is not None else None,
            'traffic_cycle_policy': 'subscription_cycle_anchor',
            'tariff_snapshot': tariff_snapshot,
        }

    async def create_tariff_invoice(
        self,
        *,
        user: User,
        package_code: str,
        months: int,
        device_mode: str,
        device_count: int = 0,
        early_renewal: bool = False,
        subscription_id: int | None = None,
        selected_traffic_gb: int | None = None,
    ) -> Invoice:
        normalized_months = self._require_positive_months(months)
        normalized_device_mode = self._normalize_device_mode(device_mode)
        normalized_device_count = self._normalize_device_count(
            device_mode=normalized_device_mode,
            device_count=device_count,
        )

        target_subscription_id = await self._validate_tariff_target(
            user=user,
            subscription_id=subscription_id,
            early_renewal=early_renewal,
        )
        target_subscription = None
        if target_subscription_id is not None:
            target_subscription = await self._resolve_user_subscription(
                user=user,
                subscription_id=target_subscription_id,
                require_active=False,
            )

        basket = await self._calculate_tariff_basket(
            package_code=package_code,
            months=normalized_months,
            user_balance=user.balance,
            use_balance=False,
            device_mode=normalized_device_mode,
            device_count=normalized_device_count,
            selected_traffic_gb=selected_traffic_gb,
        )
        tariff_plan_id, tariff_snapshot = self._build_tariff_snapshot(basket)
        payload = self._build_tariff_invoice_payload(
            basket=basket,
            package_code=package_code,
            months=normalized_months,
            device_mode=normalized_device_mode,
            early_renewal=early_renewal,
            target_subscription=target_subscription,
            tariff_snapshot=tariff_snapshot,
        )
        idempotency_key = build_invoice_idempotency_key(
            tg_id=user.tg_id,
            purpose=InvoicePurpose.tariff.value,
            code=package_code,
            units=normalized_months,
            extras={
                'device_mode': normalized_device_mode,
                'device_count': normalized_device_count,
                'subscription_id': target_subscription_id or 0,
                'early_renewal': int(bool(early_renewal)),
                'traffic_gb': selected_traffic_gb if selected_traffic_gb is not None else 0,
            },
        )
        invoice = await self._create_invoice(
            user_id=user.id,
            purpose=InvoicePurpose.tariff,
            amount=self._money(basket.total),
            balance_used=Decimal('0.00'),
            payable_amount=self._money(basket.payable),
            provider=self.payments.provider_name,
            payload_json=payload,
            tariff_plan_id=tariff_plan_id,
            tariff_snapshot_json=tariff_snapshot,
            idempotency_key=idempotency_key,
        )
        self._normalize_invoice_amounts(invoice)
        await self._refresh_provider_invoice(invoice)
        PAYMENTS_CREATED.labels(purpose=invoice.purpose.value).inc()
        return invoice

    async def create_topup_invoice(self, *, user: User, topup_code: str, subscription_id: int | None = None) -> Invoice:
        target_subscription = await self._validate_topup_target(user=user, subscription_id=subscription_id)

        basket = PricingService.calculate_topup_basket(topup_code, user.balance, use_balance=False)
        payload = {
            'kind': 'topup',
            'description': f'Докупка трафика {basket.topup.title} до конца текущего цикла',
            'topup_code': topup_code,
            'topup_title': basket.topup.title,
            'extra_traffic_gb': basket.topup.extra_traffic_gb,
            'applies_until': 'current_billing_period_end',
            'cycle_extra_traffic_only': True,
            **self._build_subscription_snapshot_payload(target_subscription),
        }
        idempotency_key = build_invoice_idempotency_key(
            tg_id=user.tg_id,
            purpose=InvoicePurpose.topup.value,
            code=topup_code,
            units=0,
            extras={
                'subscription_id': (target_subscription.id if target_subscription is not None else 0),
            },
        )
        invoice = await self._create_invoice(
            user_id=user.id,
            purpose=InvoicePurpose.topup,
            amount=self._money(basket.total),
            balance_used=Decimal('0.00'),
            payable_amount=self._money(basket.payable),
            provider=self.payments.provider_name,
            payload_json=payload,
            idempotency_key=idempotency_key,
        )
        self._normalize_invoice_amounts(invoice)
        await self._refresh_provider_invoice(invoice)
        PAYMENTS_CREATED.labels(purpose=invoice.purpose.value).inc()
        return invoice

    async def create_balance_topup_invoice(self, *, user: User, amount: Decimal) -> Invoice:
        amount = self._require_positive_amount(amount, field_label='Сумма пополнения')
        min_topup = await PricingService.min_topup_amount(self.session)
        if amount < min_topup:
            raise ValueError(f'Минимальная сумма пополнения — {min_topup.quantize(Decimal("1"))} ₽')

        payload = {
            'kind': 'balance_topup',
            'topup_amount': str(amount),
            'description': f'Пополнение баланса на {amount} ₽',
        }
        idempotency_key = build_invoice_idempotency_key(
            tg_id=user.tg_id,
            purpose=InvoicePurpose.balance_topup.value,
            code='',
            units=str(amount),
        )
        invoice = await self._create_invoice(
            user_id=user.id,
            purpose=InvoicePurpose.balance_topup,
            amount=amount,
            balance_used=Decimal('0.00'),
            payable_amount=amount,
            provider=self.payments.provider_name,
            payload_json=payload,
            idempotency_key=idempotency_key,
        )
        self._normalize_invoice_amounts(invoice)
        await self._refresh_provider_invoice(invoice)
        PAYMENTS_CREATED.labels(purpose=invoice.purpose.value).inc()
        return invoice

    async def toggle_balance(self, invoice_id: int, tg_user_id: int) -> Invoice:
        invoice = await self.invoices.get_by_id_for_update(invoice_id)
        if invoice is None:
            raise ValueError('Счет не найден')
        if invoice.purpose == InvoicePurpose.balance_topup:
            raise ValueError('Для пополнения баланса нельзя использовать баланс')
        if invoice.status is not InvoiceStatus.pending:
            raise ValueError('Баланс можно менять только у неоплаченного счета')

        user = await self.users.get_by_tg_id_for_update(tg_user_id)
        if user is None or user.id != invoice.user_id:
            raise ValueError('Счет принадлежит другому пользователю')

        new_balance_used = Decimal('0.00') if invoice.balance_used > 0 else min(user.balance, invoice.amount)
        new_balance_used = self._money(new_balance_used)
        new_payable_amount = self._money(invoice.amount - new_balance_used)
        previous_amount = self._money(invoice.payable_amount)

        if invoice.external_invoice_id and previous_amount != new_payable_amount:
            invoice.status = InvoiceStatus.cancelled
            invoice.idempotency_key = None
            await self.audit.create(
                action=AuditAction.invoice_cancelled,
                actor_type=AuditActorType.user,
                actor_tg_id=tg_user_id,
                entity_type='invoice',
                entity_id=str(invoice.id),
                details={
                    'reason': 'reissued_after_balance_toggle',
                    'previous_payable_amount': str(previous_amount),
                    'new_payable_amount': str(new_payable_amount),
                    'previous_balance_used': str(self._money(invoice.balance_used)),
                    'new_balance_used': str(new_balance_used),
                    'tariff_plan_id': getattr(invoice, 'tariff_plan_id', None),
                },
            )

            cloned = await self._create_invoice(
                user_id=invoice.user_id,
                purpose=invoice.purpose,
                amount=self._money(invoice.amount),
                balance_used=new_balance_used,
                payable_amount=new_payable_amount,
                provider=invoice.provider,
                payload_json=dict(invoice.payload_json or {}),
                currency=invoice.currency,
                tariff_plan_id=getattr(invoice, 'tariff_plan_id', None),
                tariff_snapshot_json=dict(getattr(invoice, 'tariff_snapshot_json', None) or {}) or None,
            )
            self._normalize_invoice_amounts(cloned)
            await self._refresh_provider_invoice(cloned)
            PAYMENTS_CREATED.labels(purpose=cloned.purpose.value).inc()
            await self.session.flush()
            return cloned

        invoice.balance_used = new_balance_used
        invoice.payable_amount = new_payable_amount
        self._normalize_invoice_amounts(invoice)
        await self._refresh_provider_invoice(invoice, previous_amount=previous_amount)
        await self.session.flush()
        return invoice

    @classmethod
    def _cancelled_result(cls, invoice: Invoice, *, status_text: str | None = None) -> ProcessInvoiceResult:
        return ProcessInvoiceResult(
            invoice=invoice,
            already_processed=True,
            status_text=status_text or cls._CANCELLED_RESULT_STATUS,
        )

    async def _transition_invoice_to_cancelled(
        self,
        invoice: Invoice,
        *,
        actor_type: AuditActorType,
        actor_tg_id: int | None,
        reason: str,
        provider_status: str | None = None,
    ) -> None:
        if invoice.status == InvoiceStatus.cancelled:
            return

        invoice.status = InvoiceStatus.cancelled
        invoice.idempotency_key = None
        await self.audit.create(
            action=AuditAction.invoice_cancelled,
            actor_type=actor_type,
            actor_tg_id=actor_tg_id,
            entity_type='invoice',
            entity_id=str(invoice.id),
            details={
                'purpose': invoice.purpose.value,
                'reason': reason,
                'provider': invoice.provider,
                'provider_status': provider_status,
                'external_invoice_id': invoice.external_invoice_id,
                'tariff_plan_id': getattr(invoice, 'tariff_plan_id', None),
            },
        )
        await self.session.flush()
        PAYMENTS_FAILED.labels(reason='cancelled').inc()

    async def _consume_paid_invoice(self, invoice: Invoice, user: User) -> ProcessInvoiceResult:
        if invoice.status == InvoiceStatus.consumed:
            return ProcessInvoiceResult(invoice=invoice, already_processed=True, status_text='already_processed')
        if invoice.status == InvoiceStatus.cancelled:
            return self._cancelled_result(invoice)

        await self._mark_invoice_applying(invoice)
        self._normalize_invoice_amounts(invoice)

        if invoice.balance_used > 0:
            if user.balance < invoice.balance_used:
                raise ValueError('На балансе недостаточно средств для этого счета. Пересоздайте оплату.')
            await self.users.subtract_balance(user, invoice.balance_used)
            await self.transactions.create(
                user.id,
                invoice.balance_used,
                TransactionType.outcome,
                f'Списание с баланса по счету #{invoice.id}',
            )

        if invoice.purpose == InvoicePurpose.balance_topup and invoice.payable_amount > 0:
            await self.users.add_balance(user, invoice.payable_amount)

        if invoice.payable_amount > 0:
            tx_type = TransactionType.income if invoice.purpose == InvoicePurpose.balance_topup else TransactionType.outcome
            description = (
                f'Пополнение баланса по счету #{invoice.id}'
                if invoice.purpose == InvoicePurpose.balance_topup
                else f'Оплата счета #{invoice.id} внешним способом'
            )
            await self.transactions.create(user.id, invoice.payable_amount, tx_type, description)

        first_paid_now = user.first_paid_at is None and invoice.purpose == InvoicePurpose.tariff
        if first_paid_now:
            user.first_paid_at = datetime.now(timezone.utc)
            await self.referrals.activate_if_first_paid(user.id)

        await self.subscription_service.apply_invoice(user, invoice)

        invoice.status = InvoiceStatus.consumed
        invoice.consumed_at = datetime.now(timezone.utc)
        await self.audit.create(
            action=AuditAction.invoice_paid,
            actor_type=AuditActorType.user,
            actor_tg_id=user.tg_id,
            entity_type='invoice',
            entity_id=str(invoice.id),
            details={
                'purpose': invoice.purpose.value,
                'status': invoice.status.value,
                'tariff_plan_id': getattr(invoice, 'tariff_plan_id', None),
            },
        )
        await self.session.flush()
        PAYMENTS_CONSUMED.labels(purpose=invoice.purpose.value).inc()
        return ProcessInvoiceResult(invoice=invoice, already_processed=False, status_text='consumed')

    async def _process_locked_invoice(
        self,
        invoice: Invoice,
        user: User,
        *,
        provider_status: str | None = None,
        mark_mock_paid: bool = False,
    ) -> ProcessInvoiceResult:
        if invoice.status == InvoiceStatus.consumed:
            return ProcessInvoiceResult(invoice=invoice, already_processed=True, status_text='already_processed')
        if invoice.status == InvoiceStatus.cancelled:
            return self._cancelled_result(invoice)

        if invoice.status == InvoiceStatus.applying:
            return await self._consume_paid_invoice(invoice, user)

        if invoice.payable_amount > 0:
            if not invoice.external_invoice_id:
                await self._refresh_provider_invoice(invoice)
            if not invoice.external_invoice_id:
                raise ValueError('Не удалось подготовить ссылку на оплату. Попробуйте еще раз.')

            if mark_mock_paid:
                await self.payments.mark_paid(invoice.external_invoice_id)

            normalized_provider_status = self._normalize_provider_status(provider_status)
            provider_status_for_audit = normalized_provider_status or self._normalize_optional_str(provider_status)

            if normalized_provider_status is None and self._provider_supports_transaction_snapshot(self.payments):
                snapshot = await getattr(self.payments, 'get_transaction_snapshot')(invoice.external_invoice_id)
                self._apply_provider_snapshot_to_invoice(invoice, snapshot)
                normalized_provider_status = self._provider_snapshot_status(snapshot)
                provider_status_for_audit = self._provider_snapshot_raw_status(snapshot) or normalized_provider_status

            if normalized_provider_status is None:
                normalized_provider_status = self._normalize_provider_status(
                    await self.payments.get_status(invoice.external_invoice_id)
                )
                provider_status_for_audit = provider_status_for_audit or normalized_provider_status

            if normalized_provider_status == 'cancelled':
                await self._transition_invoice_to_cancelled(
                    invoice,
                    actor_type=AuditActorType.user,
                    actor_tg_id=user.tg_id,
                    reason='provider_cancelled',
                    provider_status=provider_status_for_audit,
                )
                return self._cancelled_result(invoice, status_text='cancelled')
            if normalized_provider_status != 'paid':
                raise ValueError('Платеж еще не подтвержден. Завершите оплату и нажмите кнопку повторно.')

            invoice.status = InvoiceStatus.paid
            invoice.paid_at = invoice.paid_at or datetime.now(timezone.utc)
        else:
            invoice.status = InvoiceStatus.paid
            invoice.paid_at = invoice.paid_at or datetime.now(timezone.utc)

        return await self._consume_paid_invoice(invoice, user)

    async def process_invoice_for_user(self, invoice_id: int, tg_user_id: int) -> ProcessInvoiceResult:
        invoice = await self.invoices.get_by_id_for_update(invoice_id)
        if invoice is None:
            raise ValueError('Счет не найден')

        user = await self.users.get_by_tg_id_for_update(tg_user_id)
        if user is None or user.id != invoice.user_id:
            raise ValueError('Счет принадлежит другому пользователю')

        should_mark_paid = self.payments.provider_name == 'mock' and invoice.payable_amount > 0
        result = await self._process_locked_invoice(invoice, user, mark_mock_paid=should_mark_paid)

        await self.session.commit()
        if result.status_text in {'cancelled', self._CANCELLED_RESULT_STATUS}:
            raise ValueError('Платеж был отменен или истек. Создайте новый счет.')
        return result

    async def process_provider_callback(
        self,
        provider: str,
        external_invoice_id: str,
        provider_status: str,
    ) -> ProcessInvoiceResult | None:
        normalized_provider = self._normalize_optional_str(provider)
        normalized_external_id = self._normalize_optional_str(external_invoice_id)
        if not normalized_provider or not normalized_external_id:
            raise ValueError('Некорректные данные callback провайдера')

        invoice = await self.invoices.get_by_external_invoice_id_for_update(normalized_provider, normalized_external_id)
        if invoice is None:
            return None

        if self._normalize_optional_str(invoice.provider) != normalized_provider:
            raise ValueError('Счет привязан к другому платежному провайдеру')

        normalized_status = self._normalize_provider_status(provider_status) or 'pending'

        if invoice.status == InvoiceStatus.consumed:
            return ProcessInvoiceResult(invoice=invoice, already_processed=True, status_text='already_processed')

        if invoice.status == InvoiceStatus.cancelled:
            return ProcessInvoiceResult(invoice=invoice, already_processed=True, status_text='cancelled_ignored')

        if normalized_status == 'cancelled':
            await self._transition_invoice_to_cancelled(
                invoice,
                actor_type=AuditActorType.system,
                actor_tg_id=None,
                reason='provider_callback_cancelled',
                provider_status=normalized_status,
            )
            await self.session.commit()
            return ProcessInvoiceResult(invoice=invoice, already_processed=False, status_text='cancelled')

        if normalized_status != 'paid' and invoice.status != InvoiceStatus.applying:
            return ProcessInvoiceResult(invoice=invoice, already_processed=False, status_text='pending')

        user = await self.users.get_by_id_for_update(invoice.user_id)
        if user is None:
            raise ValueError('Пользователь для счета не найден')

        result = await self._process_locked_invoice(
            invoice,
            user,
            provider_status='paid' if normalized_status == 'paid' else None,
            mark_mock_paid=False,
        )
        await self.session.commit()
        return result

    async def cancel_invoice(self, invoice_id: int, tg_user_id: int) -> Invoice:
        invoice = await self.invoices.get_by_id_for_update(invoice_id)
        if invoice is None:
            raise ValueError('Счет не найден')

        user = await self.users.get_by_tg_id_for_update(tg_user_id)
        if user is None or user.id != invoice.user_id:
            raise ValueError('Счет принадлежит другому пользователю')

        if invoice.status in {InvoiceStatus.consumed, InvoiceStatus.applying}:
            raise ValueError('Счет уже в обработке или завершен — отмена недоступна')

        invoice.status = InvoiceStatus.cancelled
        invoice.idempotency_key = None
        await self.audit.create(
            action=AuditAction.invoice_cancelled,
            actor_type=AuditActorType.user,
            actor_tg_id=tg_user_id,
            entity_type='invoice',
            entity_id=str(invoice.id),
            details={
                'purpose': invoice.purpose.value,
                'tariff_plan_id': getattr(invoice, 'tariff_plan_id', None),
            },
        )
        await self.session.flush()
        return invoice

    async def approve_invoice_as_admin(self, invoice_id: int, admin_tg_id: int | None = None) -> ProcessInvoiceResult:
        invoice = await self.invoices.get_by_id_for_update(invoice_id)
        if invoice is None:
            raise ValueError('Счет не найден')
        if invoice.status == InvoiceStatus.cancelled:
            raise ValueError('Счет уже отменен')
        if invoice.status == InvoiceStatus.consumed:
            return ProcessInvoiceResult(invoice=invoice, already_processed=True, status_text='already_processed')

        user = await self.users.get_by_id_for_update(invoice.user_id)
        if user is None:
            raise ValueError('Пользователь для счета не найден')

        invoice.status = InvoiceStatus.paid
        invoice.paid_at = invoice.paid_at or datetime.now(timezone.utc)

        result = await self._consume_paid_invoice(invoice, user)
        await self.audit.create(
            action=AuditAction.admin_action,
            actor_type=AuditActorType.admin,
            actor_tg_id=admin_tg_id,
            entity_type='invoice',
            entity_id=str(invoice.id),
            details={
                'action': 'approve_invoice',
                'purpose': invoice.purpose.value,
                'status': result.invoice.status.value,
                'tariff_plan_id': getattr(invoice, 'tariff_plan_id', None),
            },
        )
        await self.session.commit()
        return result

    async def cancel_invoice_as_admin(self, invoice_id: int, admin_tg_id: int | None = None) -> Invoice:
        invoice = await self.invoices.get_by_id_for_update(invoice_id)
        if invoice is None:
            raise ValueError('Счет не найден')
        if invoice.status in {InvoiceStatus.consumed, InvoiceStatus.applying}:
            raise ValueError('Счет уже в обработке или завершен — отмена недоступна')
        if invoice.status == InvoiceStatus.cancelled:
            return invoice

        invoice.status = InvoiceStatus.cancelled
        invoice.idempotency_key = None
        await self.audit.create(
            action=AuditAction.admin_action,
            actor_type=AuditActorType.admin,
            actor_tg_id=admin_tg_id,
            entity_type='invoice',
            entity_id=str(invoice.id),
            details={
                'action': 'cancel_invoice',
                'purpose': invoice.purpose.value,
                'tariff_plan_id': getattr(invoice, 'tariff_plan_id', None),
            },
        )
        await self.session.commit()
        return invoice
