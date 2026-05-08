from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass(slots=True)
class PaymentInvoice:
    invoice_id: str
    amount: Decimal
    currency: str = 'RUB'
    status: str = 'pending'
    payment_url: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


class PaymentProviderError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class PaymentProvider(ABC):
    provider_name: str = 'unknown'
    display_name: str = 'Платежная система'

    @abstractmethod
    async def create_invoice(self, amount: Decimal, payload: dict[str, Any]) -> PaymentInvoice:
        raise NotImplementedError

    @abstractmethod
    async def get_status(self, invoice_id: str) -> str:
        raise NotImplementedError

    @abstractmethod
    async def mark_paid(self, invoice_id: str) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        return None

    def confirm_button_text(self, payable_amount: Decimal) -> str:
        if payable_amount <= 0:
            return '✅ Подтвердить'
        return '✅ Я оплатил'
