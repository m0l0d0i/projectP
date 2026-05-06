from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.db.models import BroadcastDeliveryStatus, BroadcastJobStatus
from app.db.repositories import (
    BroadcastJobDeliveryRepository,
    BroadcastJobRepository,
    UserRepository,
)

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _retry_delay_for_attempt(*, attempt: int, base_delay: float, retry_after: float | None = None) -> float:
    delay = max(0.0, float(base_delay)) * (2 ** max(0, attempt - 1))
    if retry_after is not None:
        delay = max(delay, max(0.0, float(retry_after)))
    return delay


def _is_retryable_send_error(exc: Exception) -> bool:
    if isinstance(exc, (TelegramForbiddenError, TelegramBadRequest)):
        return False
    return isinstance(exc, (TelegramRetryAfter, TelegramNetworkError, TelegramAPIError, TimeoutError, OSError))


def _should_notify_creator(policy: str, *, status: BroadcastJobStatus, failed_count: int) -> bool:
    normalized = (policy or 'always').strip().lower()
    if normalized == 'never':
        return False
    if normalized == 'failures':
        return status in {BroadcastJobStatus.failed, BroadcastJobStatus.cancelled} or failed_count > 0
    return True


def _status_label(status: BroadcastJobStatus | str | None) -> str:
    normalized = getattr(status, 'value', status)
    return {
        BroadcastJobStatus.draft.value: 'черновик',
        BroadcastJobStatus.scheduled.value: 'запланирована',
        BroadcastJobStatus.running.value: 'в процессе',
        BroadcastJobStatus.completed.value: 'завершена',
        BroadcastJobStatus.failed.value: 'ошибка',
        BroadcastJobStatus.cancelled.value: 'отменена',
        'pending': 'запланирована',
    }.get(str(normalized), str(normalized))


def _broadcast_summary_text(job) -> str:
    lines = [
        '📣 Рассылка завершена',
        f'ID задачи: {job.id}',
        f'Статус: {_status_label(getattr(job, "status", BroadcastJobStatus.failed))}',
        f'Обработано: {int(getattr(job, "processed_users", 0) or 0)} / {int(getattr(job, "total_users", 0) or 0)}',
        f'Отправлено: {int(getattr(job, "sent_count", 0) or 0)}',
        f'Ошибок: {int(getattr(job, "failed_count", 0) or 0)}',
    ]

    if getattr(job, 'photo_file_id', None):
        lines.append('Контент: фото')
    elif (getattr(job, 'text', None) or '').strip():
        lines.append('Контент: текст')

    last_error = (getattr(job, 'last_error', None) or '').strip()
    if last_error:
        lines.append(f'Последняя ошибка: {last_error[:500]}')

    return '\n'.join(lines)


def _notification_target_tg_id(job) -> int | None:
    raw = getattr(job, 'created_by_tg_id', None)
    try:
        resolved = int(raw)
    except (TypeError, ValueError):
        return None
    return resolved if resolved > 0 else None


async def _notify_broadcast_result(bot, settings: Settings, job) -> None:
    target_tg_id = _notification_target_tg_id(job)
    if target_tg_id is None:
        return

    if not _should_notify_creator(
        settings.broadcast_notify_policy,
        status=getattr(job, 'status', BroadcastJobStatus.failed),
        failed_count=int(getattr(job, 'failed_count', 0) or 0),
    ):
        return

    try:
        await bot.send_message(target_tg_id, _broadcast_summary_text(job))
    except Exception as exc:  # pragma: no cover - defensive runtime handling
        logger.warning(
            'Failed to send broadcast summary for job=%s to tg_id=%s: %s',
            getattr(job, 'id', '?'),
            target_tg_id,
            exc,
        )


def _job_text(job) -> str | None:
    normalized = (getattr(job, 'text', None) or '').strip()
    return normalized or None


def _job_photo_file_id(job) -> str | None:
    normalized = (getattr(job, 'photo_file_id', None) or '').strip()
    return normalized or None


def _job_media_type(job) -> str | None:
    normalized = (getattr(job, 'media_type', None) or '').strip().lower()
    if not normalized and _job_photo_file_id(job):
        return 'photo'
    return normalized or None


def _job_has_content(job) -> bool:
    return bool(_job_text(job) or _job_photo_file_id(job))


def _build_inline_keyboard(job) -> InlineKeyboardMarkup | None:
    keyboard_rows = getattr(job, 'keyboard_json', None) or []
    if not isinstance(keyboard_rows, list) or not keyboard_rows:
        return None

    inline_keyboard: list[list[InlineKeyboardButton]] = []
    for row in keyboard_rows:
        if not isinstance(row, list) or not row:
            continue
        buttons: list[InlineKeyboardButton] = []
        for raw_button in row:
            if not isinstance(raw_button, dict):
                continue
            text = str(raw_button.get('text', '') or '').strip()
            if not text:
                continue
            url = raw_button.get('url')
            callback_data = raw_button.get('callback_data')
            buttons.append(
                InlineKeyboardButton(
                    text=text,
                    url=str(url).strip() if url is not None else None,
                    callback_data=str(callback_data).strip() if callback_data is not None else None,
                )
            )
        if buttons:
            inline_keyboard.append(buttons)

    if not inline_keyboard:
        return None
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


async def _send_job_to_user(bot, *, user_tg_id: int, job) -> Any:
    markup = _build_inline_keyboard(job)
    photo_file_id = _job_photo_file_id(job)
    text = _job_text(job)
    media_type = _job_media_type(job)

    if photo_file_id is not None and media_type == 'photo':
        return await bot.send_photo(
            user_tg_id,
            photo_file_id,
            caption=text,
            reply_markup=markup,
        )

    return await bot.send_message(user_tg_id, text or '', reply_markup=markup)


async def _claim_next_job(sessionmaker: async_sessionmaker):
    async with sessionmaker.begin() as session:
        repo = BroadcastJobRepository(session)
        user_repo = UserRepository(session)

        job = await repo.claim_due_for_processing(now=_utcnow())
        if job is None:
            return None

        total_users = await user_repo.count_broadcast_recipients()
        job.total_users = total_users

        logger.info(
            'Claimed broadcast job=%s for processing: total_users=%s run_at=%s status=%s media=%s',
            job.id,
            total_users,
            getattr(job, 'run_at', None),
            getattr(getattr(job, 'status', None), 'value', getattr(job, 'status', None)),
            _job_media_type(job) or 'text',
        )
        return job


async def _cancel_running_job(sessionmaker: async_sessionmaker, *, job_id: int, reason: str):
    async with sessionmaker.begin() as session:
        repo = BroadcastJobRepository(session)
        job = await repo.get_by_id_for_update(job_id)
        if job is None:
            return None
        if job.status != BroadcastJobStatus.running:
            return job

        cancelled_by_tg_id = getattr(job, 'cancelled_by_tg_id', None)
        if hasattr(repo, 'cancel'):
            try:
                await repo.cancel(
                    job,
                    error=reason,
                    cancelled_by_tg_id=cancelled_by_tg_id,
                )
            except TypeError:
                await repo.cancel(job, error=reason)
        else:
            job.status = BroadcastJobStatus.cancelled
            job.finished_at = _utcnow()
            job.last_error = reason
        logger.info('Cancelled broadcast job=%s: %s', job_id, reason)
        return job


async def _load_job_chunk(
    sessionmaker: async_sessionmaker,
    *,
    job_id: int,
    batch_size: int,
):
    async with sessionmaker.begin() as session:
        repo = BroadcastJobRepository(session)
        user_repo = UserRepository(session)

        job = await repo.get_by_id_for_update(job_id)
        if job is None:
            return None, [], False

        if job.status != BroadcastJobStatus.running:
            return job, [], False

        if getattr(job, 'cancel_requested_at', None) is not None:
            reason = (getattr(job, 'last_error', None) or '').strip() or 'Broadcast cancelled by admin request'
            cancelled_job = await _cancel_running_job(sessionmaker, job_id=job_id, reason=reason)
            return cancelled_job, [], True

        after_id = job.last_user_id or 0
        users = await user_repo.list_broadcast_recipients_chunk(
            after_id=after_id,
            limit=batch_size,
        )

        if not users:
            await repo.complete(job)
            logger.info(
                'Completed broadcast job=%s: processed=%s sent=%s failed=%s total=%s',
                job.id,
                int(getattr(job, 'processed_users', 0) or 0),
                int(getattr(job, 'sent_count', 0) or 0),
                int(getattr(job, 'failed_count', 0) or 0),
                int(getattr(job, 'total_users', 0) or 0),
            )
            return job, [], True

        return job, users, False


async def _deliver_to_user(bot, sessionmaker: async_sessionmaker, settings: Settings, *, job_id: int, user, job) -> dict:
    attempt_count = 0
    delivery_status = BroadcastDeliveryStatus.failed
    last_error: str | None = None
    telegram_message_id: int | None = None
    delivered_at: datetime | None = None

    max_attempts = max(1, int(settings.broadcast_retry_attempts))
    retry_base_delay = max(0.0, float(settings.broadcast_retry_base_delay_seconds))

    while attempt_count < max_attempts:
        attempt_count += 1
        try:
            sent_message = await _send_job_to_user(bot, user_tg_id=user.tg_id, job=job)
            telegram_message_id = getattr(sent_message, 'message_id', None)
            delivered_at = _utcnow()
            delivery_status = BroadcastDeliveryStatus.sent
            break
        except TelegramForbiddenError as exc:
            logger.info(
                'Broadcast job=%s skipped tg_id=%s: bot forbidden',
                job_id,
                user.tg_id,
            )
            delivery_status = BroadcastDeliveryStatus.bot_blocked
            last_error = str(exc)

            async with sessionmaker.begin() as mark_session:
                mark_user_repo = UserRepository(mark_session)
                db_user = await mark_user_repo.get_by_id_for_update(user.id)
                if db_user is not None:
                    await mark_user_repo.set_bot_blocked(db_user, True, str(exc))
            break
        except Exception as exc:  # pragma: no cover - defensive runtime handling
            delivery_status = BroadcastDeliveryStatus.failed
            last_error = str(exc)
            retry_after = None

            if isinstance(exc, TelegramRetryAfter):
                retry_after = float(getattr(exc, 'retry_after', 1.0))
                last_error = f'retry_after:{retry_after}'

            is_retryable = _is_retryable_send_error(exc)
            exhausted = attempt_count >= max_attempts

            if not is_retryable or exhausted:
                logger.warning(
                    'Broadcast job=%s failed for tg_id=%s on attempt %s/%s: %s',
                    job_id,
                    user.tg_id,
                    attempt_count,
                    max_attempts,
                    exc,
                )
                break

            delay = _retry_delay_for_attempt(
                attempt=attempt_count,
                base_delay=retry_base_delay,
                retry_after=retry_after,
            )
            logger.info(
                'Broadcast job=%s retrying tg_id=%s in %.2fs after attempt %s/%s due to: %s',
                job_id,
                user.tg_id,
                delay,
                attempt_count,
                max_attempts,
                exc,
            )
            await asyncio.sleep(delay)

    return {
        'user': user,
        'status': delivery_status,
        'attempt_count': attempt_count,
        'last_error': last_error,
        'telegram_message_id': telegram_message_id,
        'delivered_at': delivered_at,
    }


async def _persist_chunk_results(
    sessionmaker: async_sessionmaker,
    *,
    job_id: int,
    users: list,
    results: list[dict],
):
    async with sessionmaker.begin() as session:
        repo = BroadcastJobRepository(session)
        delivery_repo = BroadcastJobDeliveryRepository(session)

        job = await repo.get_by_id_for_update(job_id)
        if job is None:
            return None

        if job.status != BroadcastJobStatus.running:
            return job

        chunk_processed = 0
        chunk_sent = 0
        chunk_failed = 0

        for result in results:
            await delivery_repo.upsert_result(
                job_id=job.id,
                user=result['user'],
                status=result['status'],
                attempt_count=result['attempt_count'],
                last_error=result['last_error'],
                telegram_message_id=result['telegram_message_id'],
                delivered_at=result['delivered_at'],
            )

            chunk_processed += 1
            if result['status'] == BroadcastDeliveryStatus.sent:
                chunk_sent += 1
            else:
                chunk_failed += 1

        await repo.advance(
            job,
            processed_inc=chunk_processed,
            sent_inc=chunk_sent,
            failed_inc=chunk_failed,
        )
        job.last_user_id = users[-1].id

        if getattr(job, 'cancel_requested_at', None) is not None:
            reason = (getattr(job, 'last_error', None) or '').strip() or 'Broadcast cancelled by admin request'
            if hasattr(repo, 'cancel'):
                try:
                    await repo.cancel(
                        job,
                        error=reason,
                        cancelled_by_tg_id=getattr(job, 'cancelled_by_tg_id', None),
                    )
                except TypeError:
                    await repo.cancel(job, error=reason)
            else:
                job.status = BroadcastJobStatus.cancelled
                job.finished_at = _utcnow()
                job.last_error = reason

        logger.info(
            'Broadcast job=%s chunk persisted: chunk_processed=%s chunk_sent=%s chunk_failed=%s last_user_id=%s totals=%s/%s/%s status=%s',
            job.id,
            chunk_processed,
            chunk_sent,
            chunk_failed,
            job.last_user_id,
            int(getattr(job, 'processed_users', 0) or 0),
            int(getattr(job, 'sent_count', 0) or 0),
            int(getattr(job, 'failed_count', 0) or 0),
            getattr(getattr(job, 'status', None), 'value', getattr(job, 'status', None)),
        )
        return job


async def process_scheduled_broadcasts(bot, sessionmaker: async_sessionmaker, settings: Settings) -> None:
    batch_size = max(1, int(settings.broadcast_batch_size))
    send_delay = max(0.0, float(settings.broadcast_send_delay_seconds))

    while True:
        claimed_job = await _claim_next_job(sessionmaker)
        if claimed_job is None:
            logger.debug('No due broadcast jobs found')
            return

        job_id = claimed_job.id
        if not _job_has_content(claimed_job):
            logger.warning('Broadcast job=%s has no content; failing job', job_id)
            failed_job = None
            async with sessionmaker.begin() as session:
                repo = BroadcastJobRepository(session)
                job = await repo.get_by_id_for_update(job_id)
                if job is not None and job.status == BroadcastJobStatus.running:
                    await repo.fail(job, error='Broadcast content is empty')
                    failed_job = job
            if failed_job is not None:
                await _notify_broadcast_result(bot, settings, failed_job)
            continue

        try:
            while True:
                job, users, completed = await _load_job_chunk(
                    sessionmaker,
                    job_id=job_id,
                    batch_size=batch_size,
                )

                if job is None:
                    logger.warning('Broadcast job=%s disappeared during processing', job_id)
                    break

                if completed:
                    await _notify_broadcast_result(bot, settings, job)
                    break

                if job.status != BroadcastJobStatus.running:
                    logger.info(
                        'Broadcast job=%s is no longer running (status=%s)',
                        job_id,
                        getattr(job, 'status', None),
                    )
                    await _notify_broadcast_result(bot, settings, job)
                    break

                results: list[dict] = []

                max_concurrent = max(1, int(getattr(settings, 'broadcast_max_concurrent', 15)))
                sem = asyncio.Semaphore(max_concurrent)
                rate_lock = asyncio.Lock()
                min_interval = send_delay
                last_send_ts: float = 0.0

                async def _rate_limited_deliver(user_item):
                    nonlocal last_send_ts
                    async with sem:
                        # Global rate-limiter: enforce minimum interval between sends
                        if min_interval > 0:
                            async with rate_lock:
                                loop = asyncio.get_event_loop()
                                now = loop.time()
                                wait = last_send_ts + min_interval - now
                                if wait > 0:
                                    await asyncio.sleep(wait)
                                last_send_ts = loop.time()
                        return await _deliver_to_user(
                            bot, sessionmaker, settings,
                            job_id=job_id, user=user_item, job=job,
                        )

                gathered = await asyncio.gather(
                    *[_rate_limited_deliver(u) for u in users],
                    return_exceptions=True,
                )

                for idx, item in enumerate(gathered):
                    if isinstance(item, BaseException):
                        logger.warning(
                            'Broadcast job=%s unexpected error for user_id=%s: %s',
                            job_id, users[idx].id, item,
                        )
                        results.append({
                            'user': users[idx],
                            'status': BroadcastDeliveryStatus.failed,
                            'attempt_count': 0,
                            'last_error': str(item),
                            'telegram_message_id': None,
                            'delivered_at': None,
                        })
                    else:
                        results.append(item)

                persisted_job = await _persist_chunk_results(
                    sessionmaker,
                    job_id=job_id,
                    users=users,
                    results=results,
                )

                if persisted_job is None:
                    logger.warning('Broadcast job=%s disappeared before persist', job_id)
                    break

                if persisted_job.status in {BroadcastJobStatus.completed, BroadcastJobStatus.failed, BroadcastJobStatus.cancelled}:
                    await _notify_broadcast_result(bot, settings, persisted_job)
                    break

        except Exception as exc:  # pragma: no cover - defensive runtime handling
            logger.exception('Scheduled broadcast job=%s failed: %s', job_id, exc)
            failed_job = None
            async with sessionmaker.begin() as session:
                repo = BroadcastJobRepository(session)
                job = await repo.get_by_id_for_update(job_id)
                if job is not None and job.status == BroadcastJobStatus.running:
                    await repo.fail(job, error=str(exc))
                    failed_job = job
            if failed_job is not None:
                await _notify_broadcast_result(bot, settings, failed_job)
