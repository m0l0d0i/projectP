from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.db.models import InvoiceStatus
from app.db.repositories import InvoiceRepository
from app.services.marzban import MarzbanClient
from app.services.payment_engine import PaymentService
from app.services.payments import PaymentProviderError, PlategaProvider
from app.services.subscriptions import SubscriptionService

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ProviderPollSnapshot:
    external_invoice_id: str
    normalized_status: str
    raw_status: str | None = None
    payload: dict[str, Any] | None = None


def _normalize_provider_status(value: str | None) -> str:
    normalized = (value or '').strip().lower()
    if normalized in {'paid', 'success', 'succeeded', 'confirmed'}:
        return 'paid'
    if normalized in {'cancelled', 'canceled', 'failed', 'expired', 'chargebacked', 'error'}:
        return 'cancelled'
    return 'pending'


async def _fetch_provider_snapshot(provider: PlategaProvider, external_invoice_id: str) -> ProviderPollSnapshot:
    snapshot_getter = getattr(provider, 'get_transaction_snapshot', None)
    if callable(snapshot_getter):
        snapshot = await snapshot_getter(external_invoice_id)
        normalized_status = _normalize_provider_status(getattr(snapshot, 'normalized_status', None) or getattr(snapshot, 'status', None))
        raw_status = getattr(snapshot, 'raw_status', None) or getattr(snapshot, 'status', None)
        payload = getattr(snapshot, 'payload', None) or getattr(snapshot, 'raw', None)
        resolved_invoice_id = (
            str(getattr(snapshot, 'transaction_id', '') or '').strip()
            or str(getattr(snapshot, 'external_invoice_id', '') or '').strip()
            or external_invoice_id
        )
        return ProviderPollSnapshot(
            external_invoice_id=resolved_invoice_id,
            normalized_status=normalized_status,
            raw_status=str(raw_status).strip() or None,
            payload=payload if isinstance(payload, dict) else None,
        )

    status = await provider.get_status(external_invoice_id)
    return ProviderPollSnapshot(
        external_invoice_id=external_invoice_id,
        normalized_status=_normalize_provider_status(status),
        raw_status=status,
        payload=None,
    )


async def process_pending_platega_invoices(bot, sessionmaker: async_sessionmaker, settings: Settings) -> None:
    if settings.payment_provider != 'platega':
        return

    merchant_id = settings.platega_merchant_id
    secret = settings.platega_secret_value
    if not merchant_id or not secret:
        logger.warning('Platega polling skipped: merchant_id or secret is not configured')
        return

    provider = PlategaProvider(settings)
    marzban = MarzbanClient(settings)
    try:
        async with sessionmaker() as session:
            pending_invoices = await InvoiceRepository(session).list_pending_by_provider('platega', limit=100)

        for invoice in pending_invoices:
            external_id = (invoice.external_invoice_id or '').strip()
            if not external_id:
                logger.warning('Platega polling skipped invoice=%s: missing external_invoice_id', invoice.id)
                continue

            try:
                provider_snapshot = await _fetch_provider_snapshot(provider, external_id)
            except PaymentProviderError as exc:
                logger.warning(
                    'Platega polling failed for invoice=%s external_id=%s: %s',
                    invoice.id,
                    external_id,
                    exc,
                )
                continue
            except Exception:
                logger.exception(
                    'Unexpected Platega polling error for invoice=%s external_id=%s',
                    invoice.id,
                    external_id,
                )
                continue

            if provider_snapshot.normalized_status not in {'paid', 'cancelled'}:
                logger.debug(
                    'Platega polling: invoice=%s external_id=%s status=%s raw_status=%s',
                    invoice.id,
                    external_id,
                    provider_snapshot.normalized_status,
                    provider_snapshot.raw_status,
                )
                continue

            try:
                async with sessionmaker() as session:
                    service = PaymentService(
                        session,
                        settings,
                        provider,
                        SubscriptionService(session, settings, marzban),
                    )
                    result = await service.process_provider_callback(
                        'platega',
                        provider_snapshot.external_invoice_id,
                        provider_snapshot.normalized_status,
                    )
                    if result is None and provider_snapshot.external_invoice_id != external_id:
                        logger.warning(
                            'Platega polling: invoice lookup missed with provider transaction id=%s; retrying original external_id=%s',
                            provider_snapshot.external_invoice_id,
                            external_id,
                        )
                        result = await service.process_provider_callback(
                            'platega',
                            external_id,
                            provider_snapshot.normalized_status,
                        )
                    if result is None:
                        logger.warning(
                            'Platega polling: callback result missing for external_id=%s resolved_external_id=%s',
                            external_id,
                            provider_snapshot.external_invoice_id,
                        )
                        continue

                    invoice_after = result.invoice
                    # Уведомление пользователя об успешной оплате теперь
                    # ставится в outbox в `_consume_paid_invoice` (OPS-4),
                    # в той же транзакции что и `invoice.status = consumed`.
                    # См. app/services/payment_engine.py.

                    logger.info(
                        'Platega polling processed invoice=%s external_id=%s resolved_external_id=%s provider_status=%s raw_status=%s result_status=%s invoice_status=%s already_processed=%s',
                        invoice.id,
                        external_id,
                        provider_snapshot.external_invoice_id,
                        provider_snapshot.normalized_status,
                        provider_snapshot.raw_status,
                        result.status_text,
                        invoice_after.status.value,
                        result.already_processed,
                    )
            except Exception:
                logger.exception(
                    'Failed to process pending Platega invoice=%s external_id=%s provider_status=%s raw_status=%s',
                    invoice.id,
                    external_id,
                    provider_snapshot.normalized_status,
                    provider_snapshot.raw_status,
                )
                continue
    finally:
        await marzban.close()
        await provider.close()
