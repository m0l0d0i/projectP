from __future__ import annotations

import asyncio
import logging
import queue
from contextlib import suppress
from logging.handlers import QueueHandler, QueueListener, TimedRotatingFileHandler
from pathlib import Path
from typing import Any

import sentry_sdk
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError

from app.config import get_settings
from app.db.repositories import (
    AppLinkRepository,
    AppSettingsRepository,
    PricingRuleRepository,
)
from app.db.session import create_engine_and_sessionmaker
from app.handlers import admin_panel, errors, fallback, profile, purchase, rules, start, support, vpn
from app.middlewares.anti_spam import AntiSpamMiddleware
from app.middlewares.blocked import BlockedUserMiddleware
from app.middlewares.db import DbSessionMiddleware
from app.middlewares.services import ServicesMiddleware
from app.observability.metrics import BOT_UP
from app.scheduler import SchedulerLeader, build_scheduler
from app.services.anti_spam import AntiSpamService
from app.services.cache import CacheService
from app.services.marzban import MarzbanClient
from app.services.payments import MockPaymentProvider, PaymentProvider, PlategaProvider
from app.utils.runtime_settings import effective_list_from_row, effective_optional_int_from_row
from app.web.app import start_fastapi_server, stop_fastapi_server
from app.webhooks import start_web_server, stop_web_server

logger = logging.getLogger(__name__)

_BOOTSTRAP_WEB_ADMIN_USERNAME = 'admin'
_BOOTSTRAP_WEB_ADMIN_PASSWORD = 'admin'
_PRODUCTION_ENVIRONMENTS = frozenset({'production', 'prod'})


def _is_production(settings) -> bool:
    env = str(getattr(settings, 'sentry_environment', '') or '').strip().lower()
    return env in _PRODUCTION_ENVIRONMENTS


async def _watch_scheduler_leadership(leader: SchedulerLeader, scheduler, admin_server=None) -> None:
    await leader.wait_until_lost()
    if scheduler is None:
        return
    with suppress(Exception):
        if getattr(scheduler, 'running', False):
            scheduler.shutdown(wait=False)
    if admin_server is not None:
        _attach_admin_runtime(admin_server, scheduler=None, scheduler_state_info=_scheduler_runtime_state(running=False, state='leader_lock_lost', message='Scheduler stopped because leader lock was lost', leader_lock_enabled=True))
    logger.error('Scheduler stopped because leader lock was lost')


def _sentry_before_send(event, hint):
    exc_info = hint.get('exc_info') if hint else None
    if exc_info:
        exc = exc_info[1]
        name = exc.__class__.__name__
        msg = str(exc)
        if 'message is not modified' in msg:
            return None
        if name in {'CancelledError', 'TimeoutError'}:
            return None
    return event


def setup_logging(level: str, log_dir: str = 'logs') -> tuple[QueueListener, QueueListener]:
    root = logging.getLogger()
    root.setLevel(level.upper())

    formatter = logging.Formatter('%(asctime)s | %(levelname)s | %(name)s | %(message)s')
    root.handlers.clear()

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    file_handler = TimedRotatingFileHandler(
        log_path / 'bot.log',
        when='midnight',
        backupCount=7,
        encoding='utf-8',
    )
    file_handler.setFormatter(formatter)

    # Используем QueueHandler для асинхронной записи в консоль и файлы без блокировки Event Loop
    log_queue = queue.Queue(-1)
    queue_handler = QueueHandler(log_queue)
    root.addHandler(queue_handler)

    listener = QueueListener(log_queue, stream_handler, file_handler, respect_handler_level=True)
    listener.start()

    audit_handler = TimedRotatingFileHandler(
        log_path / 'audit.log',
        when='midnight',
        backupCount=30,
        encoding='utf-8',
    )
    audit_handler.setFormatter(formatter)

    audit_logger = logging.getLogger('app.audit')
    audit_logger.setLevel(level.upper())
    audit_logger.handlers.clear()
    audit_logger.propagate = False

    audit_queue = queue.Queue(-1)
    audit_queue_handler = QueueHandler(audit_queue)
    audit_logger.addHandler(audit_queue_handler)

    audit_listener = QueueListener(audit_queue, audit_handler, respect_handler_level=True)
    audit_listener.start()

    return listener, audit_listener


def _normalize_recipient_ids(values: list[int | str] | tuple[int | str, ...] | set[int | str] | None) -> list[int]:
    result: list[int] = []
    seen: set[int] = set()

    for value in values or []:
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            continue

        if normalized in seen:
            continue

        seen.add(normalized)
        result.append(normalized)

    return result


def _mask_identifier(value: str | None) -> str:
    normalized = str(value or '').strip()
    if not normalized:
        return 'missing'
    if len(normalized) <= 4:
        return '*' * len(normalized)
    return f"{normalized[:2]}***{normalized[-2:]}"


def _build_payment_provider(settings) -> PaymentProvider:
    if getattr(settings, 'is_platega_payment_provider', False):
        provider = PlategaProvider(settings)
        logger.info(
            'Payment provider initialized: provider=platega base_url=%s callback_url=%s merchant_id=%s return_url=%s failed_url=%s',
            settings.platega_base_url,
            settings.platega_callback_url,
            _mask_identifier(getattr(settings, 'platega_merchant_id', None)),
            bool(settings.platega_return_url),
            bool(settings.platega_failed_url),
        )
        return provider

    if getattr(settings, 'is_mock_payment_provider', False) or str(getattr(settings, 'payment_provider', '') or '').strip().lower() == 'mock':
        logger.critical(
            'Payment provider is MOCK. Any payable invoice can be marked paid locally without a real acquiring flow. '
            'Use PAYMENT_PROVIDER=platega in production.'
        )
        return MockPaymentProvider()

    raise RuntimeError(f"Unsupported payment provider: {settings.payment_provider!r}")


def _log_web_admin_bootstrap_credentials_warning(settings) -> None:
    username = (settings.web_admin_username or '').strip()
    password = settings.web_admin_password_value

    is_bootstrap_username = username == _BOOTSTRAP_WEB_ADMIN_USERNAME
    is_bootstrap_password = password == _BOOTSTRAP_WEB_ADMIN_PASSWORD

    if is_bootstrap_username and is_bootstrap_password:
        message = (
            f'Web admin is configured with default bootstrap credentials '
            f'{_BOOTSTRAP_WEB_ADMIN_USERNAME!r}/{_BOOTSTRAP_WEB_ADMIN_PASSWORD!r} on '
            f'{settings.web_admin_host}:{settings.web_admin_port}. '
            'Set explicit WEB_ADMIN_USERNAME and WEB_ADMIN_PASSWORD before starting.'
        )
        if _is_production(settings):
            raise RuntimeError(message)
        logger.critical(message)
        return

    if is_bootstrap_username or is_bootstrap_password:
        logger.warning(
            'Web admin is starting with partially bootstrap credentials on %s:%s '
            '(username_is_default=%s, password_is_default=%s). '
            'Set explicit WEB_ADMIN_USERNAME and WEB_ADMIN_PASSWORD.',
            settings.web_admin_host,
            settings.web_admin_port,
            is_bootstrap_username,
            is_bootstrap_password,
        )


def _log_payment_provider_warning(settings) -> None:
    provider = str(getattr(settings, 'payment_provider', '') or '').strip().lower()

    if provider == 'mock':
        message = (
            'Payment provider is MOCK. Any payable invoice can be marked paid locally '
            'without a real acquiring flow. Use PAYMENT_PROVIDER=platega in production.'
        )
        if _is_production(settings):
            raise RuntimeError(message)
        logger.critical(message)
        return

    if provider == 'platega':
        logger.info(
            'Payment provider configured: provider=platega base_url=%s callback_url=%s merchant_id=%s',
            settings.platega_base_url,
            settings.platega_callback_url,
            _mask_identifier(getattr(settings, 'platega_merchant_id', None)),
        )

        if not getattr(settings, 'platega_configured', False):
            logger.critical('Platega provider is selected but credentials are incomplete')
            return

        if not settings.platega_return_url or not settings.platega_failed_url:
            logger.warning(
                'Platega is enabled but one or both public redirect URLs are empty '
                '(PLATEGA_RETURN_URL=%s, PLATEGA_FAILED_URL=%s). '
                'Payments can still work, but success/failure UX will be degraded.',
                bool(settings.platega_return_url),
                bool(settings.platega_failed_url),
            )
            return

        logger.info('Platega public redirect URLs are configured')
        return

    logger.warning('Unknown payment provider configured: %r', provider)


async def _load_startup_recipients(sessionmaker, settings) -> list[int]:
    try:
        async with sessionmaker.begin() as session:
            app_settings = await AppSettingsRepository(session).get()

            recipients = _normalize_recipient_ids(effective_list_from_row(app_settings, 'startup_notify_ids', settings.startup_notify_ids))
            if recipients:
                return recipients

            admin_ids = _normalize_recipient_ids(effective_list_from_row(app_settings, 'admin_ids', settings.admin_ids))
            if admin_ids:
                return admin_ids

            support_chat_id = effective_optional_int_from_row(app_settings, 'support_chat_id', settings.support_chat_id)
            if support_chat_id is not None:
                return [int(support_chat_id)]
    except Exception:
        logger.exception('Failed to load startup recipients from AppSettings; falling back to env settings')

    recipients = _normalize_recipient_ids(settings.startup_notify_ids or [])
    if recipients:
        return recipients

    recipients = _normalize_recipient_ids(settings.admin_ids or [])
    if recipients:
        return recipients

    if settings.support_chat_id is not None:
        return [int(settings.support_chat_id)]

    return []


async def _safe_startup_ping(bot: Bot, settings, sessionmaker) -> None:
    last_exc: Exception | None = None

    for attempt in range(3):
        try:
            me = await bot.get_me(request_timeout=30)
            logger.info('Бот авторизован как @%s (%s)', me.username, me.id)
            break
        except (TelegramNetworkError, TelegramAPIError) as exc:
            last_exc = exc
            if attempt == 2:
                raise
            await asyncio.sleep(1 + attempt)
    else:
        if last_exc:
            raise last_exc

    recipients = await _load_startup_recipients(sessionmaker, settings)

    for recipient_id in recipients:
        try:
            await bot.send_message(recipient_id, 'Бот запущен.')
        except Exception as exc:
            logger.warning('Не удалось отправить startup message получателю %s: %s', recipient_id, exc)


def _build_dispatcher(settings, marzban, payments, sessionmaker, cache: CacheService) -> tuple[Dispatcher, AntiSpamService]:
    dp = Dispatcher()

    db_middleware = DbSessionMiddleware(sessionmaker)
    blocked_middleware = BlockedUserMiddleware(settings, cache)
    anti_spam = AntiSpamService(
        cache=cache,
        session_factory=sessionmaker,
        settings_cache_ttl_seconds=max(1, int(getattr(settings, 'anti_spam_settings_cache_ttl_seconds', 30))),
        enabled=settings.anti_spam_enabled,
        message_limit=settings.anti_spam_message_limit,
        message_window_seconds=settings.anti_spam_message_window_seconds,
        callback_limit=settings.anti_spam_callback_limit,
        callback_window_seconds=settings.anti_spam_callback_window_seconds,
        block_seconds=settings.anti_spam_block_seconds,
        min_interval_seconds=settings.anti_spam_min_interval_seconds,
    )
    anti_spam_middleware = AntiSpamMiddleware(anti_spam, settings)
    services_middleware = ServicesMiddleware(
        settings=settings,
        marzban=marzban,
        payments=payments,
        cache=cache,
        sessionmaker=sessionmaker,
    )

    dp.message.middleware(db_middleware)
    dp.callback_query.middleware(db_middleware)

    dp.message.middleware(blocked_middleware)
    dp.callback_query.middleware(blocked_middleware)

    dp.message.middleware(anti_spam_middleware)
    dp.callback_query.middleware(anti_spam_middleware)

    dp.message.middleware(services_middleware)
    dp.callback_query.middleware(services_middleware)

    dp.include_router(start.router)
    dp.include_router(admin_panel.router)
    dp.include_router(vpn.router)
    dp.include_router(purchase.router)
    dp.include_router(profile.router)
    dp.include_router(rules.router)
    dp.include_router(support.router)
    dp.include_router(fallback.router)
    dp.include_router(errors.router)

    return dp, anti_spam


def _resolve_admin_runtime_target(target: Any) -> Any | None:
    if target is None:
        return None

    direct_state = getattr(target, 'state', None)
    if direct_state is not None:
        return target

    config = getattr(target, 'config', None)
    config_app = getattr(config, 'app', None)
    if getattr(config_app, 'state', None) is not None:
        return config_app

    loaded_app = getattr(target, 'loaded_app', None)
    if getattr(loaded_app, 'state', None) is not None:
        return loaded_app

    return None


def _attach_admin_runtime(target: Any, **values: Any) -> None:
    resolved_target = _resolve_admin_runtime_target(target)
    state = getattr(resolved_target, 'state', None)
    if state is None:
        logger.warning('Unable to attach admin runtime: FastAPI app.state is not available on target=%r', target)
        return

    for key, value in values.items():
        setattr(state, key, value)


def _scheduler_runtime_state(
    *,
    running: bool,
    state: str,
    message: str,
    leader_lock_enabled: bool | None = None,
    lock_owner: str | None = None,
    lock_ttl_seconds: int | None = None,
    reclaimed_stale_lock: bool = False,
) -> dict[str, Any]:
    return {
        'running': bool(running),
        'state': state,
        'message': message,
        'leader_lock_enabled': leader_lock_enabled,
        'lock_owner': lock_owner,
        'lock_ttl_seconds': lock_ttl_seconds,
        'reclaimed_stale_lock': bool(reclaimed_stale_lock),
    }


def _admin_runtime_payload(
    *,
    bot: Bot,
    anti_spam_service: AntiSpamService,
    cache: CacheService,
    scheduler,
    marzban: MarzbanClient,
    payments: PaymentProvider,
    scheduler_state_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    redis_client = getattr(cache, 'redis', None) or getattr(cache, 'client', None) or cache
    return {
        'bot': bot,
        'telegram_bot': bot,
        'main_bot': bot,
        'anti_spam_service': anti_spam_service,
        'cache': cache,
        'redis': redis_client,
        'redis_client': redis_client,
        'scheduler': scheduler,
        'scheduler_state_info': scheduler_state_info or _scheduler_runtime_state(running=bool(getattr(scheduler, 'running', False)), state='running' if getattr(scheduler, 'running', False) else 'stopped', message='Running' if getattr(scheduler, 'running', False) else 'Stopped'),
        'marzban': marzban,
        'payments': payments,
        'payment_provider': payments,
    }


async def main() -> None:
    settings = get_settings()
    log_listener, audit_listener = setup_logging(settings.log_level, settings.log_dir)

    engine = None
    sessionmaker = None
    bot = None
    cache = None
    marzban = None
    payments: PaymentProvider | None = None
    dp = None
    anti_spam_service = None
    scheduler = None
    scheduler_leader = None
    scheduler_watchdog = None
    scheduler_state_info = _scheduler_runtime_state(
        running=False,
        state='startup',
        message='Scheduler startup pending',
        leader_lock_enabled=bool(settings.redis_url),
    )
    runner = None
    admin_server = None
    admin_server_task = None

    try:
        _log_web_admin_bootstrap_credentials_warning(settings)
        _log_payment_provider_warning(settings)

        if settings.sentry_dsn:
            sentry_sdk.init(
                dsn=settings.sentry_dsn,
                environment=settings.sentry_environment,
                before_send=_sentry_before_send,
            )

        engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url)

        async with sessionmaker.begin() as session:
            await AppLinkRepository(session).ensure_defaults()
            # Do not pre-create AppSettings on startup. A missing row must keep
            # env/bootstrap values effective until an admin explicitly saves
            # runtime overrides into the singleton record.
            await PricingRuleRepository(session).ensure()

        session = AiohttpSession(proxy=settings.telegram_proxy_url) if settings.telegram_proxy_url else None
        bot = Bot(
            settings.bot_token,
            session=session,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )

        cache = CacheService(settings.redis_url, settings.redis_prefix)
        marzban = MarzbanClient(settings)
        payments = _build_payment_provider(settings)
        dp, anti_spam_service = _build_dispatcher(settings, marzban, payments, sessionmaker, cache)

        BOT_UP.set(1)

        runner, _ = await start_web_server(
            settings=settings,
            sessionmaker=sessionmaker,
            marzban=marzban,
            payments=payments,
            dp=dp,
            bot=bot,
            cache=cache,
        )

        admin_server, admin_server_task = await start_fastapi_server(
            sessionmaker=sessionmaker,
            settings=settings,
        )

        _attach_admin_runtime(
            admin_server,
            **_admin_runtime_payload(
                bot=bot,
                anti_spam_service=anti_spam_service,
                cache=cache,
                scheduler=scheduler,
                marzban=marzban,
                payments=payments,
                scheduler_state_info=scheduler_state_info,
            ),
        )

        await asyncio.sleep(0.5)
        if admin_server_task.done():
            exc = admin_server_task.exception()
            if exc is not None:
                logger.error(
                    'FastAPI admin server failed to start',
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
                raise RuntimeError('FastAPI admin server failed to start') from exc
            logger.error('FastAPI admin server task finished unexpectedly during startup')
            raise RuntimeError('FastAPI admin server exited during startup')

        if settings.scheduler_enabled:
            scheduler_leader = SchedulerLeader(
                settings.redis_url,
                key=f'{settings.redis_prefix}:scheduler:leader',
                ttl_seconds=settings.scheduler_leader_lock_ttl_seconds,
            )
            if await scheduler_leader.start():
                scheduler = build_scheduler(bot, sessionmaker, settings, marzban)
                scheduler.start()
                scheduler_state_info = _scheduler_runtime_state(
                    running=True,
                    state='running',
                    message='Running',
                    leader_lock_enabled=scheduler_leader.is_external_lock_enabled,
                    lock_owner=scheduler_leader.last_seen_owner,
                    lock_ttl_seconds=scheduler_leader.last_seen_ttl_seconds,
                    reclaimed_stale_lock=scheduler_leader.reclaimed_stale_lock,
                )
                _attach_admin_runtime(
                    admin_server,
                    **_admin_runtime_payload(
                        bot=bot,
                        anti_spam_service=anti_spam_service,
                        cache=cache,
                        scheduler=scheduler,
                        marzban=marzban,
                        payments=payments,
                        scheduler_state_info=scheduler_state_info,
                    ),
                )
                if scheduler_leader.is_external_lock_enabled:
                    scheduler_watchdog = asyncio.create_task(
                        _watch_scheduler_leadership(scheduler_leader, scheduler, admin_server)
                    )
                logger.info('Scheduler started on this instance')
            else:
                owner_suffix = f" owner={scheduler_leader.last_seen_owner}" if scheduler_leader.last_seen_owner else ''
                ttl_suffix = (
                    f" ttl={scheduler_leader.last_seen_ttl_seconds}s"
                    if scheduler_leader.last_seen_ttl_seconds is not None
                    else ''
                )
                scheduler_state_info = _scheduler_runtime_state(
                    running=False,
                    state='leader_lock_not_acquired',
                    message=(
                        'Scheduler skipped: leader lock is held by another instance or stale Redis key'
                        f'{owner_suffix}{ttl_suffix}'
                    ),
                    leader_lock_enabled=scheduler_leader.is_external_lock_enabled,
                    lock_owner=scheduler_leader.last_seen_owner,
                    lock_ttl_seconds=scheduler_leader.last_seen_ttl_seconds,
                    reclaimed_stale_lock=scheduler_leader.reclaimed_stale_lock,
                )
                _attach_admin_runtime(
                    admin_server,
                    **_admin_runtime_payload(
                        bot=bot,
                        anti_spam_service=anti_spam_service,
                        cache=cache,
                        scheduler=None,
                        marzban=marzban,
                        payments=payments,
                        scheduler_state_info=scheduler_state_info,
                    ),
                )
                logger.info('Scheduler skipped: another instance already owns the leader lock')
        else:
            scheduler_state_info = _scheduler_runtime_state(
                running=False,
                state='disabled',
                message='Scheduler disabled in settings',
                leader_lock_enabled=False,
            )
            _attach_admin_runtime(
                admin_server,
                **_admin_runtime_payload(
                    bot=bot,
                    anti_spam_service=anti_spam_service,
                    cache=cache,
                    scheduler=None,
                    marzban=marzban,
                    payments=payments,
                    scheduler_state_info=scheduler_state_info,
                ),
            )
            logger.info('Scheduler disabled via settings')

        if settings.webhook_enabled:
            if settings.webhook_base_url:
                webhook_url = f"{settings.webhook_base_url.rstrip('/')}{settings.telegram_webhook_path}"
                await bot.set_webhook(
                    webhook_url,
                    secret_token=settings.telegram_webhook_secret,
                    allowed_updates=dp.resolve_used_update_types(),
                    drop_pending_updates=True,
                )
                logger.info('Webhook enabled: %s', webhook_url)
            else:
                logger.warning(
                    'WEBHOOK_BASE_URL not configured. Webhook route is running but Telegram webhook was not set.'
                )

            await _safe_startup_ping(bot, settings, sessionmaker)
            await asyncio.Event().wait()
        else:
            with suppress(Exception):
                await bot.delete_webhook(drop_pending_updates=True)

            logger.info('Starting polling mode...')
            await _safe_startup_ping(bot, settings, sessionmaker)
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

    finally:
        BOT_UP.set(0)

        if scheduler_watchdog is not None:
            scheduler_watchdog.cancel()
            with suppress(asyncio.CancelledError):
                await scheduler_watchdog

        if scheduler is not None:
            with suppress(Exception):
                scheduler.shutdown(wait=False)

        if scheduler_leader is not None:
            with suppress(Exception):
                await scheduler_leader.close()

        if bot is not None and settings.webhook_enabled:
            with suppress(Exception):
                await bot.delete_webhook(drop_pending_updates=False)

        if runner is not None:
            await stop_web_server(runner)

        if admin_server is not None:
            await stop_fastapi_server(admin_server)
        if admin_server_task and not admin_server_task.done():
            admin_server_task.cancel()
            with suppress(asyncio.CancelledError):
                await admin_server_task

        if marzban is not None:
            with suppress(Exception):
                await marzban.close()

        if cache is not None:
            with suppress(Exception):
                await cache.close()

        if payments is not None:
            close = getattr(payments, 'close', None)
            if callable(close):
                with suppress(Exception):
                    await close()

        if bot is not None:
            with suppress(Exception):
                await bot.session.close()

        if engine is not None:
            with suppress(Exception):
                await engine.dispose()

        log_listener.stop()
        audit_listener.stop()


if __name__ == '__main__':
    asyncio.run(main())