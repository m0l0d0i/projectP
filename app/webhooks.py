from __future__ import annotations

import hmac
import logging
from contextlib import suppress
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.observability.metrics import REQUEST_LATENCY, metrics_response_text
from app.services.marzban import MarzbanClient
from app.services.payment_engine import PaymentService
from app.services.payments import PaymentProvider
from app.services.subscriptions import SubscriptionService

logger = logging.getLogger(__name__)

_MAX_CALLBACK_BODY_BYTES = 64 * 1024

try:
    from redis.asyncio import Redis
except Exception:  # pragma: no cover - optional dependency in tests
    Redis = None


@dataclass(slots=True)
class PlategaCallbackEnvelope:
    candidate_external_ids: tuple[str, ...]
    normalized_status: str
    raw_status: str
    payload: dict[str, Any]


_PLATEGA_PAID_STATUSES = {'CONFIRMED', 'PAID', 'SUCCESS', 'SUCCEEDED'}
_PLATEGA_CANCELLED_STATUSES = {'CANCELED', 'CANCELLED', 'CHARGEBACKED', 'FAILED', 'ERROR', 'EXPIRED'}


def _metrics_label_path(request: web.Request) -> str:
    route = request.match_info.route if request.match_info is not None else None
    resource = getattr(route, 'resource', None)
    canonical = getattr(resource, 'canonical', None)
    if canonical:
        return str(canonical)
    return request.path


@web.middleware
async def metrics_middleware(request: web.Request, handler):
    start = perf_counter()
    try:
        return await handler(request)
    finally:
        REQUEST_LATENCY.labels(path=_metrics_label_path(request)).observe(perf_counter() - start)


async def healthcheck(_: web.Request) -> web.Response:
    return web.json_response({'ok': True})


async def readycheck(request: web.Request) -> web.Response:
    sessionmaker = request.app.get('sessionmaker')
    settings: Settings | None = request.app.get('settings')
    app_logger = request.app.get('logger', logger)
    cache = request.app.get('cache')

    if sessionmaker:
        try:
            async with sessionmaker() as session:
                await session.execute(text('SELECT 1'))
        except Exception as exc:
            app_logger.error('Readiness failed: DB error - %s', exc)
            return web.json_response({'ok': False, 'error': 'db_unreachable'}, status=500)

    if settings and settings.redis_url:
        if cache and hasattr(cache, 'redis'):
            try:
                pong = await cache.redis.ping()
                if not pong:
                    return web.json_response({'ok': False, 'error': 'redis_unreachable'}, status=500)
            except Exception as exc:
                app_logger.error('Readiness failed: Redis error - %s', exc)
                return web.json_response({'ok': False, 'error': 'redis_unreachable'}, status=500)
        else:
            app_logger.error('Readiness failed: Redis configured but global cache instance not provided')
            return web.json_response({'ok': False, 'error': 'redis_driver_unavailable'}, status=500)

    return web.json_response(
        {
            'ok': True,
            'db': 'connected',
            'redis': 'connected' if settings and settings.redis_url else 'disabled',
        }
    )


async def metrics(_: web.Request) -> web.Response:
    payload, content_type = metrics_response_text()
    if 'charset=' in content_type:
        mime, _, charset_part = content_type.partition(';')
        charset = charset_part.split('charset=', 1)[1].strip() if 'charset=' in charset_part else None
        return web.Response(body=payload, content_type=mime.strip(), charset=charset or None)
    return web.Response(body=payload, content_type=content_type)


def _is_json_content_type(content_type: str | None) -> bool:
    normalized = (content_type or '').strip().lower()
    return normalized == 'application/json' or normalized.endswith('+json')


def _header_matches(expected: str | None, actual: str | None) -> bool:
    if expected is None and actual is None:
        return True
    if expected is None or actual is None:
        return False
    return hmac.compare_digest(str(expected), str(actual))


def _coerce_non_empty_str(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _coerce_dict(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _candidate_external_ids_from_payload(payload: dict[str, Any]) -> tuple[str, ...]:
    candidates: list[str] = []

    def add(value: Any) -> None:
        normalized = _coerce_non_empty_str(value)
        if normalized and normalized not in candidates:
            candidates.append(normalized)

    add(payload.get('id'))
    add(payload.get('transactionId'))
    add(payload.get('externalId'))

    transaction = _coerce_dict(payload.get('transaction'))
    if transaction:
        add(transaction.get('id'))
        add(transaction.get('transactionId'))
        add(transaction.get('externalId'))

    data = _coerce_dict(payload.get('data'))
    if data:
        add(data.get('id'))
        add(data.get('transactionId'))
        add(data.get('externalId'))

    return tuple(candidates)


def _normalize_platega_status(status: Any) -> tuple[str, str]:
    raw_status = _coerce_non_empty_str(status)
    value = (raw_status or '').upper()
    if value in _PLATEGA_PAID_STATUSES:
        return 'paid', value
    if value in _PLATEGA_CANCELLED_STATUSES:
        return 'cancelled', value
    return 'pending', value


def _parse_platega_callback_payload(payload: Any) -> PlategaCallbackEnvelope:
    if not isinstance(payload, dict):
        raise web.HTTPBadRequest(text='invalid json payload')

    candidate_external_ids = _candidate_external_ids_from_payload(payload)
    if not candidate_external_ids:
        raise web.HTTPBadRequest(text='missing id')

    normalized_status, raw_status = _normalize_platega_status(payload.get('status'))
    return PlategaCallbackEnvelope(
        candidate_external_ids=candidate_external_ids,
        normalized_status=normalized_status,
        raw_status=raw_status,
        payload=payload,
    )


async def _read_callback_json(request: web.Request) -> dict[str, Any]:
    if request.content_length is not None and request.content_length > _MAX_CALLBACK_BODY_BYTES:
        raise web.HTTPRequestEntityTooLarge(max_size=_MAX_CALLBACK_BODY_BYTES, actual_size=request.content_length)
    if not _is_json_content_type(request.content_type):
        raise web.HTTPUnsupportedMediaType(text='content type must be application/json')

    try:
        payload = await request.json()
    except web.HTTPRequestEntityTooLarge:
        raise
    except Exception as exc:
        raise web.HTTPBadRequest(text='invalid json') from exc

    if not isinstance(payload, dict):
        raise web.HTTPBadRequest(text='invalid json payload')
    return payload


async def platega_callback(request: web.Request) -> web.Response:
    settings: Settings = request.app['settings']
    sessionmaker: async_sessionmaker = request.app['sessionmaker']
    marzban: MarzbanClient = request.app['marzban']
    payments: PaymentProvider = request.app['payments']

    if settings.payment_provider != 'platega':
        logger.warning('Platega callback rejected: payment provider is %s', settings.payment_provider)
        return web.Response(status=404, text='provider disabled')

    platega_secret = settings.platega_secret_value
    if not settings.platega_merchant_id or not platega_secret:
        logger.error('Platega callback rejected: provider credentials are not configured')
        return web.Response(status=503, text='provider not configured')

    merchant_id = request.headers.get('X-MerchantId')
    secret = request.headers.get('X-Secret')

    if not _header_matches(settings.platega_merchant_id, merchant_id):
        logger.warning('Platega callback rejected: invalid merchant header=%s', merchant_id)
        return web.Response(status=401, text='invalid merchant')

    if not _header_matches(platega_secret, secret):
        logger.warning('Platega callback rejected: invalid secret for merchant=%s', merchant_id)
        return web.Response(status=401, text='invalid secret')

    payload: dict[str, Any] | None = None
    try:
        payload = await _read_callback_json(request)
        envelope = _parse_platega_callback_payload(payload)
    except web.HTTPRequestEntityTooLarge:
        logger.warning('Platega callback rejected: payload too large')
        return web.Response(status=413, text='payload too large')
    except web.HTTPException as exc:
        logger.warning('Platega callback rejected: %s payload=%s', exc.text, payload)
        return web.Response(status=exc.status, text=exc.text)

    processed_result = None
    resolved_external_id: str | None = None

    try:
        async with sessionmaker() as session:
            service = PaymentService(session, settings, payments, SubscriptionService(session, settings, marzban))
            for candidate_external_id in envelope.candidate_external_ids:
                processed_result = await service.process_provider_callback(
                    'platega',
                    candidate_external_id,
                    envelope.normalized_status,
                )
                if processed_result is not None:
                    resolved_external_id = candidate_external_id
                    break

            if processed_result is None:
                logger.warning(
                    'Platega callback for unknown transaction ids=%s raw_status=%s normalized=%s payload=%s',
                    envelope.candidate_external_ids,
                    envelope.raw_status,
                    envelope.normalized_status,
                    envelope.payload,
                )
                await session.rollback()
                return web.Response(status=200)

            logger.info(
                'Platega callback processed ids=%s resolved_external_id=%s raw_status=%s normalized=%s result=%s',
                envelope.candidate_external_ids,
                resolved_external_id,
                envelope.raw_status,
                envelope.normalized_status,
                getattr(processed_result, 'status_text', None),
            )
    except Exception:
        logger.exception(
            'Failed to process Platega callback ids=%s raw_status=%s normalized=%s',
            envelope.candidate_external_ids,
            envelope.raw_status,
            envelope.normalized_status,
        )
        return web.Response(status=500, text='failed')

    return web.Response(status=200)


def build_web_app(
    *,
    settings: Settings,
    sessionmaker: async_sessionmaker,
    marzban: MarzbanClient,
    payments: PaymentProvider,
    dp: Dispatcher,
    bot: Bot,
    cache: Any | None = None,
) -> web.Application:
    app = web.Application(client_max_size=_MAX_CALLBACK_BODY_BYTES, middlewares=[metrics_middleware])
    app['settings'] = settings
    app['sessionmaker'] = sessionmaker
    app['marzban'] = marzban
    app['payments'] = payments
    app['logger'] = logger
    app['cache'] = cache

    app.router.add_get('/healthz', healthcheck)
    app.router.add_get('/readyz', readycheck)

    if settings.metrics_enabled:
        app.router.add_get('/metrics', metrics)

    app.router.add_post(settings.platega_callback_path, platega_callback)

    telegram_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=settings.telegram_webhook_secret,
    )
    telegram_handler.register(app, path=settings.telegram_webhook_path)
    setup_application(app, dp, bot=bot)
    return app


async def start_web_server(
    *,
    settings: Settings,
    sessionmaker: async_sessionmaker,
    marzban: MarzbanClient,
    payments: PaymentProvider,
    dp: Dispatcher,
    bot: Bot,
    cache: Any | None = None,
) -> tuple[web.AppRunner, web.BaseSite]:
    app = build_web_app(
        settings=settings,
        sessionmaker=sessionmaker,
        marzban=marzban,
        payments=payments,
        dp=dp,
        bot=bot,
        cache=cache,
    )
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host=settings.app_host, port=settings.app_port)
    await site.start()
    logger.info('HTTP server started on %s:%s', settings.app_host, settings.app_port)
    return runner, site


async def stop_web_server(runner: web.AppRunner | None) -> None:
    if runner is None:
        return
    with suppress(Exception):
        await runner.cleanup()
