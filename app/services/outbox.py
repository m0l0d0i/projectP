from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import OutboxKind, OutboxMessage
from app.db.repositories import OutboxRepository, UserRepository

logger = logging.getLogger(__name__)


class OutboxDispatcher:
    """Worker that drains the outbox by calling the appropriate side effect.

    One tick per scheduler invocation; each tick claims up to `batch_size`
    rows via FOR UPDATE SKIP LOCKED, dispatches them, and records the result.
    """

    def __init__(
        self,
        bot: Bot,
        sessionmaker: async_sessionmaker,
        *,
        batch_size: int = 50,
    ) -> None:
        self.bot = bot
        self.sessionmaker = sessionmaker
        self.batch_size = batch_size

    async def tick(self) -> int:
        """Process one batch. Returns number of rows processed."""
        async with self.sessionmaker() as session:
            repo = OutboxRepository(session)
            try:
                rows = await repo.claim_due(limit=self.batch_size)
            except Exception:
                logger.exception('Outbox: failed to claim due rows')
                await session.rollback()
                return 0
            await session.commit()

        if not rows:
            return 0

        for row in rows:
            await self._dispatch_one(row)

        return len(rows)

    async def _dispatch_one(self, row: OutboxMessage) -> None:
        payload: dict[str, Any] = dict(row.payload_json or {})
        user_id = payload.get('user_id')
        try:
            if row.kind == OutboxKind.tg_message:
                await self._send_tg_message(row, payload)
            else:
                await self._record_failure(row.id, f'unknown kind {row.kind!r}', dead=True)
                return
        except TelegramForbiddenError as exc:
            # User blocked the bot; no point retrying.
            await self._record_failure(row.id, f'forbidden: {exc}', dead=True)
            if user_id is not None:
                await self._mark_user_bot_blocked(int(user_id), str(exc))
            return
        except TelegramRetryAfter as exc:
            # Backoff suggested by Telegram; treat as transient.
            await self._record_failure(row.id, f'retry_after={exc.retry_after}: {exc}')
            return
        except Exception as exc:
            await self._record_failure(row.id, f'{type(exc).__name__}: {exc}')
            return

        await self._record_success(row.id)
        if user_id is not None:
            await self._clear_user_bot_blocked(int(user_id))

    async def _send_tg_message(self, row: OutboxMessage, payload: dict[str, Any]) -> None:
        if row.target_chat_id is None:
            raise ValueError('tg_message outbox row has no target_chat_id')
        text = payload.get('text')
        if not text:
            raise ValueError('tg_message outbox row has empty text')
        kwargs: dict[str, Any] = {}
        if 'parse_mode' in payload:
            kwargs['parse_mode'] = payload['parse_mode']
        if 'disable_web_page_preview' in payload:
            kwargs['disable_web_page_preview'] = payload['disable_web_page_preview']
        if 'reply_markup' in payload and payload['reply_markup'] is not None:
            kwargs['reply_markup'] = InlineKeyboardMarkup.model_validate(payload['reply_markup'])
        await self.bot.send_message(row.target_chat_id, text, **kwargs)

    async def _mark_user_bot_blocked(self, user_id: int, reason: str) -> None:
        try:
            async with self.sessionmaker.begin() as session:
                user_repo = UserRepository(session)
                user = await user_repo.get_by_id_for_update(user_id)
                if user is not None and not user.bot_blocked:
                    await user_repo.set_bot_blocked(user, True, reason)
        except Exception:
            logger.exception('Outbox: failed to mark user %s bot_blocked', user_id)

    async def _clear_user_bot_blocked(self, user_id: int) -> None:
        try:
            async with self.sessionmaker.begin() as session:
                user_repo = UserRepository(session)
                user = await user_repo.get_by_id_for_update(user_id)
                if user is not None and user.bot_blocked:
                    await user_repo.set_bot_blocked(user, False, None)
                    logger.info('User %s restored from bot_blocked after successful outbox delivery', user.tg_id)
        except Exception:
            logger.exception('Outbox: failed to clear bot_blocked for user %s', user_id)

    async def _record_success(self, message_id: int) -> None:
        async with self.sessionmaker() as session:
            repo = OutboxRepository(session)
            try:
                await repo.mark_sent(message_id)
                await session.commit()
            except Exception:
                logger.exception('Outbox: failed to mark sent message_id=%s', message_id)
                await session.rollback()

    async def _record_failure(self, message_id: int, error: str, *, dead: bool = False) -> None:
        async with self.sessionmaker() as session:
            repo = OutboxRepository(session)
            try:
                await repo.mark_failed(message_id, error, dead=dead)
                await session.commit()
            except Exception:
                logger.exception('Outbox: failed to mark failed message_id=%s', message_id)
                await session.rollback()
