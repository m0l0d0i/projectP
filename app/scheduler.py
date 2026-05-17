from __future__ import annotations

import asyncio
import logging
import socket
from contextlib import suppress
from datetime import datetime, timezone
from uuid import uuid4

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import Settings
from app.services.broadcast_polling import process_scheduled_broadcasts
from app.services.marzban import MarzbanClient
from app.services.notification_dispatcher import NotificationDispatcher
from app.services.node_probe import cleanup_node_health_samples, probe_nodes_health
from app.services.notifications import (
    check_expiring,
    check_low_traffic,
    check_monthly_traffic_reset,
    check_support_ticket_auto_close,
    check_traffic_exhaustion,
    check_trial_milestones,
    check_weekly_usage_report,
)
from app.services.outbox import OutboxDispatcher
from app.services.payment_polling import process_pending_platega_invoices

logger = logging.getLogger(__name__)

try:
    from redis.asyncio import Redis
except Exception:  # pragma: no cover - optional dependency for tests/local env
    Redis = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SchedulerLeader:
    _REFRESH_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
end
return 0
"""

    _RELEASE_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""

    def __init__(self, redis_url: str | None, *, key: str, ttl_seconds: int = 90) -> None:
        self.redis_url = redis_url
        self.key = key
        self.ttl_seconds = max(10, int(ttl_seconds))
        self.token = f'{socket.gethostname()}:{uuid4().hex}'
        self._redis = Redis.from_url(redis_url, encoding='utf-8', decode_responses=True) if redis_url and Redis is not None else None
        self._refresh_task: asyncio.Task | None = None
        self._lost_event = asyncio.Event()
        self.last_seen_owner: str | None = None
        self.last_seen_ttl_seconds: int | None = None
        self.reclaimed_stale_lock: bool = False

    async def start(self) -> bool:
        if self.redis_url and Redis is None:
            logger.warning('Redis leader lock disabled: REDIS_URL is configured but redis.asyncio is unavailable')
            self._lost_event.clear()
            self.last_seen_owner = None
            self.last_seen_ttl_seconds = None
            self.reclaimed_stale_lock = False
            return True

        if self._redis is None:
            logger.info('Redis leader lock disabled: REDIS_URL is not configured')
            self._lost_event.clear()
            self.last_seen_owner = None
            self.last_seen_ttl_seconds = None
            self.reclaimed_stale_lock = False
            return True

        self.reclaimed_stale_lock = False
        acquired = await self._redis.set(self.key, self.token, ex=self.ttl_seconds, nx=True)
        if not acquired:
            owner = await self._redis.get(self.key)
            ttl = await self._redis.ttl(self.key)
            self.last_seen_owner = owner
            self.last_seen_ttl_seconds = None if ttl is None else int(ttl)

            # Defensive stale-lock recovery: reclaim keys that somehow exist without TTL.
            if ttl is not None and int(ttl) < 0:
                logger.warning(
                    'Scheduler leader lock looks stale (ttl=%s, owner=%s). Reclaiming key=%s',
                    ttl,
                    owner,
                    self.key,
                )
                await self._redis.delete(self.key)
                self.reclaimed_stale_lock = True
                acquired = await self._redis.set(self.key, self.token, ex=self.ttl_seconds, nx=True)
                if acquired:
                    self.last_seen_owner = self.token
                    self.last_seen_ttl_seconds = self.ttl_seconds

        if not acquired:
            logger.info(
                'Scheduler leader lock is already held by another instance: key=%s owner=%s ttl=%s',
                self.key,
                self.last_seen_owner,
                self.last_seen_ttl_seconds,
            )
            return False

        self._lost_event.clear()
        self.last_seen_owner = self.token
        self.last_seen_ttl_seconds = self.ttl_seconds
        self._refresh_task = asyncio.create_task(self._refresh_loop(), name='scheduler-leader-refresh')
        logger.info('Scheduler leader lock acquired: key=%s', self.key)
        return True

    async def _refresh_loop(self) -> None:
        refresh_interval = max(5, self.ttl_seconds // 6)

        try:
            while True:
                await asyncio.sleep(refresh_interval)

                if self._redis is None:
                    return

                refreshed = await self._redis.eval(
                    self._REFRESH_LOCK_SCRIPT,
                    1,
                    self.key,
                    self.token,
                    str(self.ttl_seconds),
                )
                self.last_seen_owner = self.token
                self.last_seen_ttl_seconds = self.ttl_seconds
                if int(refreshed or 0) != 1:
                    logger.warning('Scheduler leader lock lost for key=%s', self.key)
                    self._lost_event.set()
                    return

        except asyncio.CancelledError:  # pragma: no cover - normal shutdown path
            raise
        except Exception:  # pragma: no cover - defensive logging
            self._lost_event.set()
            logger.exception('Scheduler leader refresh failed')

    async def wait_until_lost(self) -> None:
        await self._lost_event.wait()

    @property
    def is_external_lock_enabled(self) -> bool:
        return self._redis is not None

    async def close(self) -> None:
        self._lost_event.set()

        if self._refresh_task is not None:
            self._refresh_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._refresh_task

        if self._redis is not None:
            with suppress(Exception):
                await self._redis.eval(
                    self._RELEASE_LOCK_SCRIPT,
                    1,
                    self.key,
                    self.token,
                )

            with suppress(Exception):
                await self._redis.aclose()


def build_scheduler(
    bot,
    sessionmaker,
    settings: Settings,
    marzban: MarzbanClient,
    notification_dispatcher: NotificationDispatcher | None = None,
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(
        timezone='UTC',
        job_defaults={
            'coalesce': True,
            'max_instances': 1,
            'misfire_grace_time': 300,
        },
    )

    notif_kwargs: dict = {
        'bot': bot,
        'sessionmaker': sessionmaker,
        'settings': settings,
        'marzban': marzban,
    }
    if notification_dispatcher is not None:
        notif_kwargs['dispatcher'] = notification_dispatcher

    scheduler.add_job(
        check_expiring,
        'interval',
        hours=12,
        kwargs=notif_kwargs,
        id='check_expiring',
        name='check_expiring',
        replace_existing=True,
    )

    scheduler.add_job(
        check_low_traffic,
        'interval',
        hours=6,
        kwargs=notif_kwargs,
        id='check_low_traffic',
        name='check_low_traffic',
        replace_existing=True,
    )

    scheduler.add_job(
        check_traffic_exhaustion,
        'interval',
        hours=6,
        kwargs=notif_kwargs,
        id='check_traffic_exhaustion',
        name='check_traffic_exhaustion',
        replace_existing=True,
    )

    scheduler.add_job(
        check_trial_milestones,
        'interval',
        hours=1,
        kwargs=notif_kwargs,
        id='check_trial_milestones',
        name='check_trial_milestones',
        replace_existing=True,
    )

    scheduler.add_job(
        check_weekly_usage_report,
        'interval',
        hours=24,
        kwargs=notif_kwargs,
        id='check_weekly_usage_report',
        name='check_weekly_usage_report',
        replace_existing=True,
    )

    scheduler.add_job(
        check_monthly_traffic_reset,
        'interval',
        hours=3,
        kwargs={'bot': bot, 'sessionmaker': sessionmaker, 'settings': settings, 'marzban': marzban},
        id='check_monthly_traffic_reset',
        name='check_monthly_traffic_reset',
        replace_existing=True,
    )

    scheduler.add_job(
        check_support_ticket_auto_close,
        'interval',
        minutes=30,
        kwargs={'bot': bot, 'sessionmaker': sessionmaker, 'settings': settings},
        id='check_support_ticket_auto_close',
        name='check_support_ticket_auto_close',
        replace_existing=True,
    )

    scheduler.add_job(
        process_scheduled_broadcasts,
        'interval',
        seconds=15,
        next_run_time=_utcnow(),
        kwargs={'bot': bot, 'sessionmaker': sessionmaker, 'settings': settings},
        id='process_scheduled_broadcasts',
        name='process_scheduled_broadcasts',
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=60,
    )

    if settings.payment_provider == 'platega':
        scheduler.add_job(
            process_pending_platega_invoices,
            'interval',
            seconds=30,
            next_run_time=_utcnow(),
            kwargs={'bot': bot, 'sessionmaker': sessionmaker, 'settings': settings},
            id='process_pending_platega_invoices',
            name='process_pending_platega_invoices',
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=60,
        )

    # FEA-ADMIN-NODE-MONITOR: probe нод каждые 90 сек. Используем notif_kwargs
    # (тот же набор: bot/sessionmaker/settings/marzban + dispatcher), чтобы алерт
    # node_down мог быть отправлен через NotificationDispatcher.
    scheduler.add_job(
        probe_nodes_health,
        'interval',
        seconds=90,
        next_run_time=_utcnow(),
        kwargs=notif_kwargs,
        id='probe_nodes_health',
        name='probe_nodes_health',
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=60,
    )

    scheduler.add_job(
        cleanup_node_health_samples,
        'interval',
        hours=24,
        kwargs={'sessionmaker': sessionmaker},
        id='cleanup_node_health_samples',
        name='cleanup_node_health_samples',
        replace_existing=True,
    )

    outbox_dispatcher = OutboxDispatcher(bot=bot, sessionmaker=sessionmaker)
    scheduler.add_job(
        outbox_dispatcher.tick,
        'interval',
        seconds=int(getattr(settings, 'outbox_dispatcher_interval_seconds', 5)),
        next_run_time=_utcnow(),
        id='outbox_dispatcher',
        name='outbox_dispatcher',
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=30,
    )

    logger.info(
        'Scheduler configured: jobs=%s',
        [job.id for job in scheduler.get_jobs()],
    )
    return scheduler