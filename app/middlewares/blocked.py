from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.repositories import AppSettingsRepository, UserRepository
from app.keyboards.reply import main_menu
from app.services.cache import CacheService
from app.utils.runtime_settings import coerce_int_set, effective_list_from_row

logger = logging.getLogger(__name__)


class BlockedUserMiddleware(BaseMiddleware):
    def __init__(self, settings: Settings, cache: CacheService) -> None:
        self.settings = settings
        self.cache = cache

    async def _load_admin_ids(self, session: AsyncSession) -> set[int]:
        try:
            repo = AppSettingsRepository(session)
            row = await repo.get()
            return coerce_int_set(effective_list_from_row(row, 'admin_ids', self.settings.admin_ids))
        except Exception:
            logger.exception('Failed to load blocked middleware admin IDs from AppSettings; falling back to env')

        return coerce_int_set(getattr(self.settings, 'admin_ids', None))

    async def _is_admin_bypass(self, session: AsyncSession, tg_id: int) -> bool:
        admin_ids = await self._load_admin_ids(session)
        return int(tg_id) in admin_ids

    async def _load_user_block_state(self, session: AsyncSession, tg_id: int) -> dict[str, Any]:
        cache_key = f'user:{tg_id}'

        try:
            cached = await self.cache.get_json(cache_key)
        except Exception:
            logger.exception('Failed to read blocked-user cache for tg_id=%s', tg_id)
            cached = None

        if cached is not None:
            return {
                'is_blocked': bool(cached.get('is_blocked')),
                'blocked_reason': cached.get('blocked_reason'),
            }

        user = await UserRepository(session).get_by_tg_id(tg_id)
        payload = {
            'is_blocked': bool(user and getattr(user, 'is_blocked', False)),
            'blocked_reason': getattr(user, 'blocked_reason', None) if user else None,
        }

        try:
            await self.cache.set_json(cache_key, payload, self.settings.user_cache_ttl_seconds)
        except Exception:
            logger.exception('Failed to write blocked-user cache for tg_id=%s', tg_id)

        return payload

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        session: AsyncSession | None = data.get('session')
        if session is None:
            return await handler(event, data)

        from_user = None
        if isinstance(event, Message):
            from_user = event.from_user
        elif isinstance(event, CallbackQuery):
            from_user = event.from_user

        if not from_user:
            return await handler(event, data)

        if await self._is_admin_bypass(session, from_user.id):
            return await handler(event, data)

        blocked_state = await self._load_user_block_state(session, from_user.id)
        if blocked_state.get('is_blocked'):
            reason = blocked_state.get('blocked_reason')
            reason_suffix = f'\nПричина: {reason}' if reason else ''

            if isinstance(event, Message):
                await event.answer(
                    f'⛔ Ваш аккаунт заблокирован. Если это ошибка — напишите в поддержку.{reason_suffix}',
                    reply_markup=main_menu(show_trial=False),
                )
            elif isinstance(event, CallbackQuery):
                await event.answer('⛔ Ваш аккаунт заблокирован', show_alert=True)
            return None

        return await handler(event, data)