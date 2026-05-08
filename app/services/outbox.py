from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import OutboxKind, OutboxMessage
from app.db.repositories import OutboxRepository

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
        try:
            if row.kind == OutboxKind.tg_message:
                await self._send_tg_message(row)
            else:
                await self._record_failure(row.id, f'unknown kind {row.kind!r}', dead=True)
                return
        except TelegramForbiddenError as exc:
            # User blocked the bot; no point retrying.
            await self._record_failure(row.id, f'forbidden: {exc}', dead=True)
            return
        except TelegramRetryAfter as exc:
            # Backoff suggested by Telegram; treat as transient.
            await self._record_failure(row.id, f'retry_after={exc.retry_after}: {exc}')
            return
        except Exception as exc:
            await self._record_failure(row.id, f'{type(exc).__name__}: {exc}')
            return

        await self._record_success(row.id)

    async def _send_tg_message(self, row: OutboxMessage) -> None:
        if row.target_chat_id is None:
            raise ValueError('tg_message outbox row has no target_chat_id')
        payload: dict[str, Any] = dict(row.payload_json or {})
        text = payload.get('text')
        if not text:
            raise ValueError('tg_message outbox row has empty text')
        kwargs: dict[str, Any] = {}
        if 'parse_mode' in payload:
            kwargs['parse_mode'] = payload['parse_mode']
        if 'disable_web_page_preview' in payload:
            kwargs['disable_web_page_preview'] = payload['disable_web_page_preview']
        await self.bot.send_message(row.target_chat_id, text, **kwargs)

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
