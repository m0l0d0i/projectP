from __future__ import annotations

import uuid
from decimal import Decimal, ROUND_HALF_UP

from app.services.payments.base import PaymentInvoice, PaymentProvider


class MockPaymentProvider(PaymentProvider):
    provider_name = 'mock'
    display_name = 'MockPay'
    _BASE_PAYMENT_URL = 'https://mock-pay.local/invoice'

    def __init__(self) -> None:
        self._invoices: dict[str, PaymentInvoice] = {}

    @classmethod
    def _normalize_amount(cls, amount: Decimal) -> Decimal:
        return Decimal(str(amount)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    @classmethod
    def _build_payment_url(cls, invoice_id: str) -> str:
        normalized_invoice_id = str(invoice_id).strip()
        return f'{cls._BASE_PAYMENT_URL}/{normalized_invoice_id}'

    async def create_invoice(self, amount: Decimal, payload: dict) -> PaymentInvoice:
        invoice_id = uuid.uuid4().hex
        invoice = PaymentInvoice(
            invoice_id=invoice_id,
            amount=self._normalize_amount(amount),
            status='pending',
            payment_url=self._build_payment_url(invoice_id),
            payload=dict(payload or {}),
        )
        self._invoices[invoice_id] = invoice
        return invoice

    async def get_status(self, invoice_id: str) -> str:
        normalized_invoice_id = str(invoice_id).strip()
        invoice = self._invoices.get(normalized_invoice_id)
        if invoice is None:
            return 'pending'
        return invoice.status

    async def mark_paid(self, invoice_id: str) -> None:
        normalized_invoice_id = str(invoice_id).strip()
        invoice = self._invoices.get(normalized_invoice_id)
        if invoice is None:
            self._invoices[normalized_invoice_id] = PaymentInvoice(
                invoice_id=normalized_invoice_id,
                amount=Decimal('0.00'),
                status='paid',
                payment_url=self._build_payment_url(normalized_invoice_id),
                payload={},
            )
            return

        invoice.status = 'paid'

    def confirm_button_text(self, payable_amount: Decimal) -> str:
        if payable_amount <= 0:
            return '✅ Подтвердить'
        return '✅ Я оплатил (Mock)'
