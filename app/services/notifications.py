from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.db.models import AuditAction, AuditActorType, SupportTicketStatus
from app.db.repositories import (
    AppSettingsRepository,
    AuditLogRepository,
    SubscriptionRepository,
    SupportTicketRepository,
    UserRepository,
)
from app.keyboards.inline import low_traffic_alert_keyboard
from app.observability.metrics import SUPPORT_TICKETS_CLOSED
from app.services.marzban import MarzbanAPIError, MarzbanClient, MarzbanUser
from app.services.notification_dispatcher import NotificationDispatcher
from app.services.subscription_urls import canonicalize_subscription_url_from_settings
from app.services.subscriptions import SubscriptionService
from app.utils.runtime_settings import effective_optional_int_from_row

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _SubscriptionTarget:
    subscription_id: int
    user_id: int
    user_tg_id: int
    marzban_username: str


async def _mark_bot_blocked(sessionmaker: async_sessionmaker, user_id: int, reason: str) -> None:
    async with sessionmaker.begin() as session:
        user_repo = UserRepository(session)
        user = await user_repo.get_by_id_for_update(user_id)
        if user is not None:
            await user_repo.set_bot_blocked(user, True, reason)


async def _mark_bot_unblocked(sessionmaker: async_sessionmaker, user_id: int) -> None:
    async with sessionmaker.begin() as session:
        user_repo = UserRepository(session)
        user = await user_repo.get_by_id_for_update(user_id)
        if user is not None and user.bot_blocked:
            await user_repo.set_bot_blocked(user, False, None)
            logger.info('User %s restored from bot_blocked after successful delivery.', user.tg_id)


async def _safe_send(
    bot,
    chat_id: int,
    text: str,
    *,
    sessionmaker: async_sessionmaker | None = None,
    user_id: int | None = None,
    **kwargs,
) -> bool:
    try:
        await bot.send_message(chat_id, text, **kwargs)
        if sessionmaker is not None and user_id is not None:
            await _mark_bot_unblocked(sessionmaker, user_id)
        return True
    except TelegramForbiddenError as exc:
        logger.info('Telegram recipient %s blocked bot: %s', chat_id, exc)
        if sessionmaker is not None and user_id is not None:
            await _mark_bot_blocked(sessionmaker, user_id, str(exc))
        return False
    except TelegramAPIError as exc:
        logger.warning('Failed to send telegram notification to %s: %s', chat_id, exc)
        return False


def _sync_local_from_remote(subscription, remote: MarzbanUser, settings: Settings) -> None:
    subscription.expire_date = remote.expire_datetime
    subscription.data_limit_bytes = remote.data_limit
    subscription.used_traffic_bytes = remote.used_traffic

    canonical_remote_url = canonicalize_subscription_url_from_settings(remote.subscription_url, settings)
    if remote.subscription_url and canonical_remote_url is None:
        logger.warning(
            'Ignoring non-canonical remote subscription_url for subscription_id=%s username=%s value=%s',
            getattr(subscription, 'id', None),
            getattr(subscription, 'marzban_username', None),
            remote.subscription_url,
        )

    canonical_existing_url = canonicalize_subscription_url_from_settings(
        getattr(subscription, 'subscription_url', None),
        settings,
    )
    subscription.subscription_url = canonical_remote_url or canonical_existing_url

    online_field = settings.marzban_online_limit_field
    if online_field:
        subscription.online_limit = remote.raw.get(online_field)

    subscription.is_active = remote.status not in {'expired', 'disabled'} and (
        remote.expire_datetime is None or remote.expire_datetime > datetime.now(timezone.utc)
    )


async def _load_active_targets(sessionmaker: async_sessionmaker) -> list[_SubscriptionTarget]:
    async with sessionmaker() as session:
        rows = await SubscriptionRepository(session).active_with_users()
        return [
            _SubscriptionTarget(
                subscription_id=subscription.id,
                user_id=user.id,
                user_tg_id=user.tg_id,
                marzban_username=subscription.marzban_username,
            )
            for subscription, user in rows
        ]


async def _load_support_chat_id(sessionmaker: async_sessionmaker, settings: Settings) -> int | None:
    try:
        async with sessionmaker() as session:
            repo = AppSettingsRepository(session)
            row = await repo.get()
            return effective_optional_int_from_row(row, 'support_chat_id', settings.support_chat_id)
    except Exception:
        logger.exception('Failed to load support_chat_id from AppSettings; falling back to env settings')

    if settings.support_chat_id is None:
        return None
    return int(settings.support_chat_id)


_FALLBACK_EXPIRING_3D = '⚠️ Ваша подписка истекает через 3 дня!'
_FALLBACK_EXPIRING_1D = '🔥 Ваша подписка истекает уже завтра!'
_FALLBACK_EXPIRED = '❌ Ваша подписка истекла. Доступ к VPN приостановлен.\nПожалуйста, продлите тариф.'
_FALLBACK_LOW_TRAFFIC = '⚠️ Осталось меньше 10% трафика. Вы можете докупить трафик или продлить тариф досрочно.'
_FALLBACK_EXHAUSTED = '📉 <b>Ваш трафик почти исчерпан!</b>\nVPN скоро перестанет работать. Вы можете докупить трафик или досрочно продлить тариф.'
_FALLBACK_TRIAL_MID = '🎁 Половина пробного периода уже позади! Попробуйте оформить полноценный тариф.'
_FALLBACK_TRIAL_LAST_DAY = '⏳ Триал заканчивается через 2 часа. Не теряйте доступ — оформите тариф.'
_FALLBACK_TRIAL_POST_EXPIRE = '👋 Триал закончился вчера. Возвращайтесь — у нас есть специальное предложение для вас.'
_FALLBACK_WEEKLY_USAGE_TEMPLATE = (
    '📊 <b>Еженедельный отчёт по подписке {service_id}</b>\n'
    'Использовано трафика: {used_gb} ГБ из {total_label}.\n'
    'Активна до: {expire_label}.'
)

# FEA-NOTIF: пороги для milestone-нотификаций (не зависят от длительности
# триала — она задаётся в admin-UI и может быть любой: 1 день / неделя /
# месяц).
_TRIAL_MID_AFTER = timedelta(hours=12)
_TRIAL_LAST_DAY_BEFORE = timedelta(hours=2)
_TRIAL_POST_EXPIRE_AFTER = timedelta(hours=24)
# Защита от слишком поздних post_expire (если job простаивал > 3д — не присылаем).
_TRIAL_POST_EXPIRE_MAX_LAG = timedelta(days=3)


async def check_expiring(
    bot,
    sessionmaker: async_sessionmaker,
    settings: Settings,
    marzban: MarzbanClient,
    dispatcher: NotificationDispatcher | None = None,
) -> None:
    dispatcher = dispatcher or NotificationDispatcher()
    now = datetime.now(timezone.utc)

    for target in await _load_active_targets(sessionmaker):
        try:
            remote = await marzban.get_user(target.marzban_username)
        except MarzbanAPIError as exc:
            logger.error('Failed to sync subscription for expiry notification', exc_info=exc)
            continue

        async with sessionmaker.begin() as session:
            subscription = await SubscriptionRepository(session).get_by_id_for_update(target.subscription_id)
            if not subscription:
                continue

            _sync_local_from_remote(subscription, remote, settings)

            expire_at = remote.expire_datetime
            if expire_at is None:
                continue

            time_left = expire_at - now
            rule_code: str | None = None
            fallback_text: str | None = None
            correlation_kind: str | None = None

            if time_left.total_seconds() <= 0:
                subscription.is_active = False
                if not subscription.notified_expired:
                    rule_code = 'expired'
                    fallback_text = _FALLBACK_EXPIRED
                    correlation_kind = 'expired'
                    subscription.notified_expired = True
            elif timedelta(days=2) < time_left <= timedelta(days=3) and not subscription.notified_3d:
                rule_code = 'expiring_3d'
                fallback_text = _FALLBACK_EXPIRING_3D
                correlation_kind = 'expiry_3d'
                subscription.notified_3d = True
            elif timedelta(days=0) < time_left <= timedelta(days=1) and not subscription.notified_1d:
                rule_code = 'expiring_1d'
                fallback_text = _FALLBACK_EXPIRING_1D
                correlation_kind = 'expiry_1d'
                subscription.notified_1d = True

            if rule_code and fallback_text and correlation_kind:
                await dispatcher.dispatch(
                    session=session,
                    code=rule_code,
                    chat_id=target.user_tg_id,
                    user_id=target.user_id,
                    default_text=fallback_text,
                    context={'subscription_id': target.subscription_id},
                    correlation_key=f'subscription:{target.subscription_id}:{correlation_kind}',
                )


async def check_low_traffic(
    bot,
    sessionmaker: async_sessionmaker,
    settings: Settings,
    marzban: MarzbanClient,
    dispatcher: NotificationDispatcher | None = None,
) -> None:
    dispatcher = dispatcher or NotificationDispatcher()
    now = datetime.now(timezone.utc)

    for target in await _load_active_targets(sessionmaker):
        try:
            remote = await marzban.get_user(target.marzban_username)
        except MarzbanAPIError as exc:
            logger.error('Failed to sync subscription for traffic notification', exc_info=exc)
            continue

        async with sessionmaker.begin() as session:
            subscription = await SubscriptionRepository(session).get_by_id_for_update(target.subscription_id)
            if not subscription:
                continue

            _sync_local_from_remote(subscription, remote, settings)

            if remote.data_limit in (None, 0):
                continue
            if remote.expire_datetime is not None and remote.expire_datetime <= now:
                continue

            ratio = (remote.used_traffic / remote.data_limit) if remote.data_limit > 0 else 0
            if not (0.9 <= ratio < 0.99 and not subscription.notified_low_traffic):
                continue

            subscription.notified_low_traffic = True
            await dispatcher.dispatch(
                session=session,
                code='low_traffic_90',
                chat_id=target.user_tg_id,
                user_id=target.user_id,
                default_text=_FALLBACK_LOW_TRAFFIC,
                default_reply_markup=low_traffic_alert_keyboard(
                    target.subscription_id,
                    notification_code='low_traffic_90',
                ),
                context={'subscription_id': target.subscription_id},
                correlation_key=f'subscription:{target.subscription_id}:low_traffic',
            )


async def check_traffic_exhaustion(
    bot,
    sessionmaker: async_sessionmaker,
    settings: Settings,
    marzban: MarzbanClient,
    dispatcher: NotificationDispatcher | None = None,
) -> None:
    dispatcher = dispatcher or NotificationDispatcher()
    now = datetime.now(timezone.utc)

    for target in await _load_active_targets(sessionmaker):
        try:
            remote = await marzban.get_user(target.marzban_username)
        except MarzbanAPIError as exc:
            logger.error('Failed to sync subscription for traffic exhaustion notification', exc_info=exc)
            continue

        async with sessionmaker.begin() as session:
            subscription = await SubscriptionRepository(session).get_by_id_for_update(target.subscription_id)
            if not subscription:
                continue

            _sync_local_from_remote(subscription, remote, settings)

            if remote.data_limit in (None, 0):
                continue
            if remote.expire_datetime is not None and remote.expire_datetime <= now:
                continue

            if not (remote.used_traffic >= int(remote.data_limit * 0.99) and not subscription.notified_exhausted):
                continue

            subscription.notified_exhausted = True
            await dispatcher.dispatch(
                session=session,
                code='traffic_exhausted',
                chat_id=target.user_tg_id,
                user_id=target.user_id,
                default_text=_FALLBACK_EXHAUSTED,
                default_parse_mode='HTML',
                default_reply_markup=low_traffic_alert_keyboard(
                    target.subscription_id,
                    notification_code='traffic_exhausted',
                ),
                context={'subscription_id': target.subscription_id},
                correlation_key=f'subscription:{target.subscription_id}:traffic_exhausted',
            )


async def check_trial_milestones(
    bot,
    sessionmaker: async_sessionmaker,
    settings: Settings,
    marzban: MarzbanClient,
    dispatcher: NotificationDispatcher | None = None,
) -> None:
    """FEA-NOTIF: trial_mid / trial_last_day / trial_post_expire_rescue.

    Идемпотентность через колонки `notified_trial_*` на Subscription —
    Redis cooldown в правилах нужен только как дополнительная защита.
    `created_at` подписки используется как момент старта триала.
    """
    dispatcher = dispatcher or NotificationDispatcher()
    now = datetime.now(timezone.utc)
    # Окно — все триалы, у которых post_expire-сценарий ещё актуален (или
    # триал ещё активен). Работает для любой длительности триала.
    expire_after = now - _TRIAL_POST_EXPIRE_MAX_LAG

    async with sessionmaker() as session:
        rows = await SubscriptionRepository(session).trial_pending_milestones(
            expire_after=expire_after,
        )
        targets = [
            (sub.id, user.id, user.tg_id) for sub, user in rows
        ]

    for subscription_id, user_id, user_tg_id in targets:
        async with sessionmaker.begin() as session:
            subscription = await SubscriptionRepository(session).get_by_id_for_update(subscription_id)
            if not subscription or not subscription.is_trial:
                continue

            trial_started_at = subscription.created_at
            trial_expire_at = subscription.expire_date
            if trial_started_at is None or trial_expire_at is None:
                continue

            if trial_started_at.tzinfo is None:
                trial_started_at = trial_started_at.replace(tzinfo=timezone.utc)
            if trial_expire_at.tzinfo is None:
                trial_expire_at = trial_expire_at.replace(tzinfo=timezone.utc)

            triggered: list[tuple[str, str, str]] = []
            # 1) trial_mid: через 12ч после старта, пока триал ещё активен.
            if (
                not subscription.notified_trial_mid
                and now - trial_started_at >= _TRIAL_MID_AFTER
                and now < trial_expire_at
            ):
                subscription.notified_trial_mid = True
                triggered.append(('trial_mid', _FALLBACK_TRIAL_MID, 'trial_mid'))

            # 2) trial_last_day: за 2ч до окончания, пока триал ещё активен.
            if (
                not subscription.notified_trial_last_day
                and timedelta(0) < trial_expire_at - now <= _TRIAL_LAST_DAY_BEFORE
            ):
                subscription.notified_trial_last_day = True
                triggered.append(('trial_last_day', _FALLBACK_TRIAL_LAST_DAY, 'trial_last_day'))

            # 3) trial_post_expire_rescue: через 24ч после окончания (но не позже
            # _TRIAL_POST_EXPIRE_MAX_LAG, чтобы не присылать неактуальное при
            # затянувшемся простое job).
            lag = now - trial_expire_at
            if (
                not subscription.notified_trial_post_expire
                and lag >= _TRIAL_POST_EXPIRE_AFTER
                and lag <= _TRIAL_POST_EXPIRE_MAX_LAG
            ):
                subscription.notified_trial_post_expire = True
                triggered.append((
                    'trial_post_expire_rescue',
                    _FALLBACK_TRIAL_POST_EXPIRE,
                    'trial_post_expire',
                ))

            for code, fallback_text, correlation_kind in triggered:
                await dispatcher.dispatch(
                    session=session,
                    code=code,
                    chat_id=user_tg_id,
                    user_id=user_id,
                    default_text=fallback_text,
                    context={'subscription_id': subscription_id},
                    correlation_key=f'subscription:{subscription_id}:{correlation_kind}',
                )


async def check_weekly_usage_report(
    bot,
    sessionmaker: async_sessionmaker,
    settings: Settings,
    marzban: MarzbanClient,
    dispatcher: NotificationDispatcher | None = None,
) -> None:
    """FEA-NOTIF: weekly_usage_report.

    Идемпотентность держится на cooldown_seconds правила (по умолчанию 6 дней
    в seed). Job можно безопасно запускать ежедневно — повторные диспатчи
    будут отсечены SET NX EX в Redis.
    """
    dispatcher = dispatcher or NotificationDispatcher()
    now = datetime.now(timezone.utc)

    for target in await _load_active_targets(sessionmaker):
        try:
            remote = await marzban.get_user(target.marzban_username)
        except MarzbanAPIError as exc:
            logger.error('Failed to sync subscription for weekly usage report', exc_info=exc)
            continue

        async with sessionmaker.begin() as session:
            subscription = await SubscriptionRepository(session).get_by_id_for_update(target.subscription_id)
            if not subscription:
                continue

            _sync_local_from_remote(subscription, remote, settings)

            # Не отправляем отчёт по уже истёкшим/выключенным подпискам.
            if remote.expire_datetime is not None and remote.expire_datetime <= now:
                continue

            data_limit = remote.data_limit
            used_bytes = max(0, int(remote.used_traffic or 0))
            used_gb = round(used_bytes / (1024 ** 3), 1)
            if data_limit in (None, 0):
                total_label = '♾️ безлимит'
            else:
                total_label = f'{round(int(data_limit) / (1024 ** 3), 1)} ГБ'

            expire_label = (
                remote.expire_datetime.strftime('%d.%m.%Y')
                if remote.expire_datetime is not None else 'без срока'
            )
            fallback_text = _FALLBACK_WEEKLY_USAGE_TEMPLATE.format(
                service_id=subscription.service_id,
                used_gb=used_gb,
                total_label=total_label,
                expire_label=expire_label,
            )

            await dispatcher.dispatch(
                session=session,
                code='weekly_usage',
                chat_id=target.user_tg_id,
                user_id=target.user_id,
                default_text=fallback_text,
                default_parse_mode='HTML',
                context={
                    'subscription_id': target.subscription_id,
                    'service_id': subscription.service_id,
                    'used_gb': used_gb,
                    'total_label': total_label,
                    'expire_label': expire_label,
                },
                # correlation_key специально не задаём: dedup-окно держится
                # cooldown'ом в Redis, а correlation_key привязал бы отчёт
                # навсегда к первой неделе.
                correlation_key=None,
            )


async def check_monthly_traffic_reset(bot, sessionmaker: async_sessionmaker, settings: Settings, marzban: MarzbanClient) -> None:
    now = datetime.now(timezone.utc)

    async with sessionmaker() as session:
        rows = await SubscriptionRepository(session).due_monthly_resets(now)
        targets = [row.id for row in rows]

    for subscription_id in targets:
        notify_tg_id = None
        notify_user_id = None

        async with sessionmaker.begin() as session:
            subscriptions = SubscriptionRepository(session)
            subscription = await subscriptions.get_by_id_for_update(subscription_id)
            if not subscription:
                continue

            service = SubscriptionService(session, settings, marzban)
            try:
                remote = await service.process_monthly_reset(subscription)
            except MarzbanAPIError as exc:
                logger.error('Failed to reset monthly traffic', exc_info=exc)
                continue

            if remote is None:
                continue

            user = await UserRepository(session).get_by_id(subscription.user_id)
            if user and subscription.monthly_traffic_bytes:
                notify_tg_id = user.tg_id
                notify_user_id = user.id

        if notify_tg_id and notify_user_id is not None:
            await _safe_send(
                bot,
                notify_tg_id,
                '🔄 Месячный пакет трафика обновлен. Можете продолжать пользоваться VPN.',
                sessionmaker=sessionmaker,
                user_id=notify_user_id,
            )


async def check_support_ticket_auto_close(bot, sessionmaker: async_sessionmaker, settings: Settings) -> None:
    hours = getattr(settings, 'support_ticket_auto_close_hours', 48)
    threshold = datetime.now(timezone.utc) - timedelta(hours=hours)
    reason = f'timeout_{hours}h'
    batch_size = 500
    after_id = 0
    support_chat_id = await _load_support_chat_id(sessionmaker, settings)

    while True:
        async with sessionmaker() as session:
            ticket_repo = SupportTicketRepository(session)

            tickets_to_close = await ticket_repo.due_auto_close(
                threshold,
                after_id=after_id,
                limit=batch_size,
            )

            ticket_payloads: list[tuple[int, int | None, int | None]] = []
            for ticket in tickets_to_close:
                user = ticket.user
                ticket_payloads.append((ticket.id, user.id if user else None, user.tg_id if user else None))

        if not ticket_payloads:
            break

        after_id = ticket_payloads[-1][0]

        closed_payloads: list[tuple[int, int | None, int | None]] = []
        for ticket_id, user_id, user_tg_id in ticket_payloads:
            async with sessionmaker.begin() as session:
                ticket_repo = SupportTicketRepository(session)
                audit = AuditLogRepository(session)
                db_ticket = await ticket_repo.get_by_id_for_update(ticket_id)
                if not db_ticket or not db_ticket.is_active:
                    continue

                previous_status = db_ticket.status.value
                closed = await ticket_repo.close(db_ticket, reason=reason)
                if not closed:
                    continue

                await audit.create(
                    action=AuditAction.ticket_closed,
                    actor_type=AuditActorType.system,
                    actor_tg_id=None,
                    entity_type='support_ticket',
                    entity_id=str(ticket_id),
                    details={
                        'reason': reason,
                        'previous_status': previous_status,
                        'new_status': SupportTicketStatus.closed.value,
                        'closed_via': 'auto_close_job',
                    },
                )
                closed_payloads.append((ticket_id, user_id, user_tg_id))

        for ticket_id, user_id, user_tg_id in closed_payloads:
            SUPPORT_TICKETS_CLOSED.labels(reason=reason).inc()
            try:
                if user_tg_id and user_id is not None:
                    await _safe_send(
                        bot,
                        user_tg_id,
                        '💤 Ваша заявка была автоматически закрыта из-за отсутствия активности.',
                        sessionmaker=sessionmaker,
                        user_id=user_id,
                    )
                if support_chat_id:
                    await _safe_send(
                        bot,
                        support_chat_id,
                        f'💤 Заявка #{ticket_id} #ticket{ticket_id} закрыта автоматически.',
                    )
            except Exception:
                logger.exception('Failed to send auto-close notifications for ticket %s', ticket_id)
