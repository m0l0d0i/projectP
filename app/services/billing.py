from __future__ import annotations

"""Backward-compatible billing facade.

This module keeps the old import path alive, but internally delegates to the
new split services: PricingService, PaymentService and SubscriptionService.
"""

from dataclasses import dataclass
from decimal import Decimal
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import InvoicePurpose, User
from app.services.marzban import MarzbanClient, MarzbanUser
from app.services.payment_engine import PaymentService
from app.services.payments.base import PaymentProvider
from app.services.subscriptions import SubscriptionService
from app.services.tariffs import PricingService


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PurchaseContext:
    amount: Decimal
    balance_used: Decimal
    payable: Decimal
    invoice_id: int


class BillingService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        marzban: MarzbanClient,
        payments: PaymentProvider,
    ) -> None:
        self.session = session
        self.settings = settings
        self.marzban = marzban
        self.payments = payments
        self.subscription_service = SubscriptionService(session, settings, marzban)
        self.payment_service = PaymentService(session, settings, payments, self.subscription_service)

    async def issue_trial(self, user: User) -> MarzbanUser:
        return await self.subscription_service.issue_trial(user)

    async def create_purchase_context(
        self,
        *,
        user: User,
        tariff_code: str,
        months: int,
        use_balance: bool,
        device_mode: str = 'unlimited',
        device_count: int = 0,
        early_renewal: bool = False,
    ) -> PurchaseContext:
        invoice = await self.payment_service.create_tariff_invoice(
            user=user,
            package_code=tariff_code,
            months=months,
            device_mode=device_mode,
            device_count=device_count,
            early_renewal=early_renewal,
        )
        if use_balance:
            invoice = await self.payment_service.toggle_balance(invoice.id, user.tg_id)
        return PurchaseContext(
            amount=Decimal(invoice.amount),
            balance_used=Decimal(invoice.balance_used),
            payable=Decimal(invoice.payable_amount),
            invoice_id=invoice.id,
        )

    async def _resolve_remote_after_processed_invoice(self, *, user: User, invoice) -> MarzbanUser | None:
        if invoice.purpose == InvoicePurpose.balance_topup:
            return None

        payload = dict(invoice.payload_json or {})
        subscription = None
        subscription_id = payload.get('subscription_id')
        if subscription_id is not None:
            try:
                subscription = await self.subscription_service.subscriptions.get_by_id(int(subscription_id))
            except (TypeError, ValueError):
                subscription = None

        if subscription is None:
            subscription = await self.subscription_service.subscriptions.get_latest_active(user.id)

        if subscription is None or subscription.user_id != user.id:
            return None

        try:
            synced = await self.subscription_service.sync_remote_state(subscription)
        except Exception:
            logger.exception('Failed to sync remote state after processed invoice=%s', getattr(invoice, 'id', None))
            return None
        return synced.remote

    async def finalize_purchase(self, *, user: User, context: PurchaseContext, early_renewal: bool = False) -> MarzbanUser | None:
        result = await self.payment_service.process_invoice_for_user(context.invoice_id, user.tg_id)
        return await self._resolve_remote_after_processed_invoice(user=user, invoice=result.invoice)

    async def purchase_topup(self, *, user: User, topup_code: str, use_balance: bool) -> tuple[MarzbanUser | None, Decimal, Decimal]:
        invoice = await self.payment_service.create_topup_invoice(user=user, topup_code=topup_code)
        if use_balance:
            invoice = await self.payment_service.toggle_balance(invoice.id, user.tg_id)
        result = await self.payment_service.process_invoice_for_user(invoice.id, user.tg_id)
        remote = await self._resolve_remote_after_processed_invoice(user=user, invoice=result.invoice)
        return remote, Decimal(invoice.balance_used), Decimal(invoice.payable_amount)
