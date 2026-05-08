from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from urllib.parse import urlparse

import aiohttp

from app.config import Settings
from app.observability.metrics import PAYMENT_REQUESTS
from app.services.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError
from app.services.payments.base import PaymentInvoice, PaymentProvider, PaymentProviderError


def _platega_breaker_is_failure(exc: BaseException) -> bool:
    if isinstance(exc, (aiohttp.ClientConnectionError, asyncio.TimeoutError)):
        return True
    if isinstance(exc, PaymentProviderError):
        msg = str(exc)
        return 'HTTP 5' in msg or 'невалидный JSON' in msg
    return False

logger = logging.getLogger(__name__)

_REQUEST_RETRYABLE_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
_REQUEST_BACKOFF_SECONDS: tuple[float, ...] = (0.5, 1.0)
_SUCCESS_STATUSES = frozenset({'CONFIRMED', 'PAID', 'SUCCESS', 'SUCCEEDED'})
_CANCELLED_STATUSES = frozenset({'CANCELED', 'CANCELLED', 'CHARGEBACKED', 'FAILED', 'ERROR', 'EXPIRED'})


@dataclass(slots=True, frozen=True)
class PlategaTransactionSnapshot:
    transaction_id: str | None
    status: str
    raw_status: str
    payment_url: str | None
    payload: dict[str, Any]


@dataclass(slots=True, frozen=True)
class PlategaCallbackSnapshot:
    transaction_id: str | None
    status: str
    raw_status: str
    merchant_header: str | None
    secret_header: str | None
    payload: dict[str, Any]


def _secret_to_str(value: Any) -> str | None:
    if value is None:
        return None

    getter = getattr(value, 'get_secret_value', None)
    if callable(getter):
        raw = getter()
        if raw is None:
            return None
        normalized = str(raw).strip()
        return normalized or None

    normalized = str(value).strip()
    return normalized or None

def _json_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_non_empty_str(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        normalized = str(value).strip()
        if normalized:
            return normalized
    return None


def _validate_payment_url(value: str) -> str:
    normalized = str(value or '').strip()
    if not normalized:
        raise PaymentProviderError('Platega не вернула ссылку на оплату')

    parsed = urlparse(normalized)
    if parsed.scheme.lower() != 'https' or not parsed.netloc:
        raise PaymentProviderError('Platega вернула небезопасную ссылку на оплату')

    if parsed.username or parsed.password:
        raise PaymentProviderError('Platega вернула некорректную ссылку на оплату')

    hostname = (parsed.hostname or '').strip().lower()
    if not hostname or hostname == 'localhost':
        raise PaymentProviderError('Platega вернула небезопасную ссылку на оплату')

    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        return normalized

    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        raise PaymentProviderError('Platega вернула небезопасную ссылку на оплату')

    return normalized


class PlategaProvider(PaymentProvider):
    provider_name = 'platega'
    display_name = 'Platega'

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = (settings.platega_base_url or '').strip().rstrip('/')
        self._merchant_id = _secret_to_str(getattr(settings, 'platega_merchant_id', None))
        self._secret = settings.platega_secret_value

        self._timeout = aiohttp.ClientTimeout(total=settings.platega_timeout_seconds)
        self._default_headers: dict[str, str] = {'Content-Type': 'application/json'}
        if self._merchant_id:
            self._default_headers['X-MerchantId'] = self._merchant_id
        if self._secret:
            self._default_headers['X-Secret'] = self._secret

        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()
        self._breaker = CircuitBreaker(
            'platega',
            failure_threshold=int(getattr(settings, 'platega_circuit_failure_threshold', 5)),
            cooldown_seconds=float(getattr(settings, 'platega_circuit_cooldown_seconds', 30.0)),
            is_failure=_platega_breaker_is_failure,
        )

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is not None and not self._session.closed:
            return self._session
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    timeout=self._timeout,
                    headers=self._default_headers,
                )
            return self._session

    async def close(self) -> None:
        session = self._session
        if session is not None and not session.closed:
            await session.close()
        self._session = None

    @classmethod
    def normalize_status(cls, status: str | None) -> str:
        value = (status or '').strip().upper()
        if value in _SUCCESS_STATUSES:
            return 'paid'
        if value in _CANCELLED_STATUSES:
            return 'cancelled'
        return 'pending'

    @classmethod
    def _extract_raw_status(cls, payload: dict[str, Any]) -> str:
        if not payload:
            return ''
        nested_data = _json_dict(payload.get('data'))
        nested_transaction = _json_dict(payload.get('transaction'))
        return (
            _first_non_empty_str(
                payload.get('status'),
                nested_data.get('status'),
                nested_transaction.get('status'),
            )
            or ''
        ).upper()

    @classmethod
    def extract_transaction_id(cls, payload: dict[str, Any]) -> str | None:
        if not payload:
            return None
        nested_data = _json_dict(payload.get('data'))
        nested_transaction = _json_dict(payload.get('transaction'))
        return _first_non_empty_str(
            payload.get('transactionId'),
            payload.get('id'),
            payload.get('transaction_id'),
            payload.get('externalId'),
            nested_data.get('transactionId'),
            nested_data.get('id'),
            nested_data.get('externalId'),
            nested_transaction.get('transactionId'),
            nested_transaction.get('id'),
            nested_transaction.get('externalId'),
        )

    @classmethod
    def extract_redirect_url(cls, payload: dict[str, Any]) -> str | None:
        if not payload:
            return None
        nested_data = _json_dict(payload.get('data'))
        nested_transaction = _json_dict(payload.get('transaction'))
        candidate = _first_non_empty_str(
            payload.get('redirect'),
            payload.get('paymentUrl'),
            nested_data.get('redirect'),
            nested_data.get('paymentUrl'),
            nested_transaction.get('redirect'),
            nested_transaction.get('paymentUrl'),
        )
        if candidate is None:
            return None
        return _validate_payment_url(candidate)

    @classmethod
    def build_transaction_snapshot(cls, payload: dict[str, Any]) -> PlategaTransactionSnapshot:
        raw_payload = dict(payload or {})
        raw_status = cls._extract_raw_status(raw_payload)
        payment_url: str | None
        try:
            payment_url = cls.extract_redirect_url(raw_payload)
        except PaymentProviderError:
            payment_url = None
        return PlategaTransactionSnapshot(
            transaction_id=cls.extract_transaction_id(raw_payload),
            status=cls.normalize_status(raw_status),
            raw_status=raw_status,
            payment_url=payment_url,
            payload=raw_payload,
        )

    @classmethod
    def build_callback_snapshot(
        cls,
        payload: dict[str, Any],
        *,
        merchant_header: str | None,
        secret_header: str | None,
    ) -> PlategaCallbackSnapshot:
        raw_payload = dict(payload or {})
        raw_status = cls._extract_raw_status(raw_payload)
        return PlategaCallbackSnapshot(
            transaction_id=cls.extract_transaction_id(raw_payload),
            status=cls.normalize_status(raw_status),
            raw_status=raw_status,
            merchant_header=_secret_to_str(merchant_header),
            secret_header=_secret_to_str(secret_header),
            payload=raw_payload,
        )

    @staticmethod
    def _serialize_amount(amount: Decimal) -> int | float:
        normalized = amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        if normalized == normalized.to_integral_value():
            return int(normalized)
        return float(normalized)

    def _ensure_configured(self) -> None:
        if not self.base_url:
            raise PaymentProviderError('Platega не настроена: отсутствует base_url')
        if not self._merchant_id or not self._secret:
            raise PaymentProviderError('Platega не настроена: отсутствует merchant_id или secret')

    def headers_match(self, merchant_id: str | None, secret: str | None) -> bool:
        expected_merchant = _secret_to_str(self._merchant_id)
        expected_secret = _secret_to_str(self._secret)
        actual_merchant = _secret_to_str(merchant_id)
        actual_secret = _secret_to_str(secret)
        return expected_merchant == actual_merchant and expected_secret == actual_secret

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._ensure_configured()

        try:
            async with self._breaker:
                return await self._do_request(method, path, json_data=json_data)
        except CircuitBreakerOpenError as exc:
            PAYMENT_REQUESTS.labels(provider='platega', result='circuit_open').inc()
            raise PaymentProviderError(
                f'Платежный сервис временно недоступен (circuit breaker, retry in {exc.retry_after:.1f}s)'
            ) from exc

    async def _do_request(
        self,
        method: str,
        path: str,
        *,
        json_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_path = path if path.startswith('/') else f'/{path}'
        url = f'{self.base_url}{normalized_path}'
        max_attempts = 1 + len(_REQUEST_BACKOFF_SECONDS)
        session = await self._get_session()

        for attempt in range(1, max_attempts + 1):
            should_retry = False
            try:
                async with session.request(method, url, json=json_data) as response:
                    text = await response.text()
                    response_status = int(response.status)

                    if response_status in _REQUEST_RETRYABLE_STATUSES and attempt < max_attempts:
                        PAYMENT_REQUESTS.labels(provider='platega', result='retryable_http_error').inc()
                        logger.warning(
                            'Platega retryable HTTP status=%s method=%s path=%s attempt=%s/%s body=%s',
                            response_status,
                            method,
                            normalized_path,
                            attempt,
                            max_attempts,
                            text[:1000],
                        )
                        should_retry = True
                    elif response_status >= 400:
                        PAYMENT_REQUESTS.labels(provider='platega', result='http_error').inc()
                        logger.error(
                            'Platega HTTP error: status=%s method=%s path=%s body=%s',
                            response_status,
                            method,
                            normalized_path,
                            text[:1000],
                        )
                        raise PaymentProviderError(f'Platega вернула HTTP {response_status}: {text[:300]}')
                    else:
                        if not text.strip():
                            PAYMENT_REQUESTS.labels(provider='platega', result='empty_response').inc()
                            return {}

                        try:
                            data = json.loads(text)
                        except json.JSONDecodeError as exc:
                            PAYMENT_REQUESTS.labels(provider='platega', result='invalid_json').inc()
                            raise PaymentProviderError('Platega вернула невалидный JSON') from exc

                        PAYMENT_REQUESTS.labels(provider='platega', result='ok').inc()
                        if not isinstance(data, dict):
                            PAYMENT_REQUESTS.labels(provider='platega', result='invalid_payload').inc()
                            raise PaymentProviderError('Platega вернула неожиданный формат ответа')

                        return data

            except asyncio.TimeoutError as exc:
                if attempt < max_attempts:
                    PAYMENT_REQUESTS.labels(provider='platega', result='retryable_timeout').inc()
                    logger.warning(
                        'Platega timeout method=%s path=%s attempt=%s/%s',
                        method,
                        normalized_path,
                        attempt,
                        max_attempts,
                    )
                    should_retry = True
                else:
                    PAYMENT_REQUESTS.labels(provider='platega', result='timeout').inc()
                    logger.exception('Platega request timed out')
                    raise PaymentProviderError('Платежный сервис не ответил вовремя. Попробуйте еще раз чуть позже.') from exc
            except aiohttp.ClientError as exc:
                if attempt < max_attempts:
                    PAYMENT_REQUESTS.labels(provider='platega', result='retryable_network_error').inc()
                    logger.warning(
                        'Platega network error method=%s path=%s attempt=%s/%s error=%s',
                        method,
                        normalized_path,
                        attempt,
                        max_attempts,
                        exc,
                    )
                    should_retry = True
                else:
                    PAYMENT_REQUESTS.labels(provider='platega', result='network_error').inc()
                    logger.exception('Platega request failed')
                    raise PaymentProviderError('Не удалось связаться с платежным сервисом') from exc

            if not should_retry:
                break

            await asyncio.sleep(_REQUEST_BACKOFF_SECONDS[attempt - 1])

        raise PaymentProviderError('Не удалось выполнить запрос к Platega')

    def _build_payload_string(self, payload: dict[str, Any]) -> str:
        safe_payload = {k: v for k, v in payload.items() if k not in {'raw'}}
        return json.dumps(safe_payload, ensure_ascii=False, separators=(',', ':'))

    def _build_create_request(self, amount: Decimal, payload: dict[str, Any]) -> dict[str, Any]:
        description = (
            str(payload.get('description') or '').strip()
            or f'Оплата счета #{payload.get("invoice_id", "")}'.strip()
            or 'Оплата VPN-сервиса'
        )
        request_body: dict[str, Any] = {
            'paymentMethod': self.settings.platega_payment_method,
            'paymentDetails': {
                'amount': self._serialize_amount(amount),
                'currency': self.settings.platega_currency,
            },
            'description': description,
            'payload': self._build_payload_string(payload),
        }
        if self.settings.platega_return_url:
            request_body['return'] = self.settings.platega_return_url
        if self.settings.platega_failed_url:
            request_body['failedUrl'] = self.settings.platega_failed_url
        return request_body

    async def create_invoice(self, amount: Decimal, payload: dict[str, Any]) -> PaymentInvoice:
        self._ensure_configured()
        request_body = self._build_create_request(amount, payload)
        data = await self._request('POST', '/transaction/process', json_data=request_body)
        snapshot = self.build_transaction_snapshot(data)

        if not snapshot.transaction_id:
            raise PaymentProviderError('Platega не вернула transactionId')
        if not snapshot.payment_url:
            raise PaymentProviderError('Platega не вернула ссылку на оплату')

        return PaymentInvoice(
            invoice_id=snapshot.transaction_id,
            amount=amount,
            currency=self.settings.platega_currency,
            status=snapshot.status,
            payment_url=snapshot.payment_url,
            payload=payload,
            raw=data,
        )

    async def get_status(self, invoice_id: str) -> str:
        data = await self._request('GET', f'/transaction/{invoice_id}')
        snapshot = self.build_transaction_snapshot(data)
        return snapshot.status

    async def get_transaction_snapshot(self, invoice_id: str) -> PlategaTransactionSnapshot:
        data = await self._request('GET', f'/transaction/{invoice_id}')
        return self.build_transaction_snapshot(data)

    async def mark_paid(self, invoice_id: str) -> None:
        return None

    def confirm_button_text(self, payable_amount: Decimal) -> str:
        if payable_amount <= 0:
            return '✅ Подтвердить'
        return '🔄 Проверить оплату'
