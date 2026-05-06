from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.repositories import AppSettingsRepository
from app.keyboards.reply import main_menu
from app.services.anti_spam import AntiSpamService
from app.utils.runtime_settings import coerce_int_set, effective_list_from_row

logger = logging.getLogger(__name__)


class AntiSpamMiddleware(BaseMiddleware):
    def __init__(self, anti_spam: AntiSpamService, settings: Settings) -> None:
        self.anti_spam = anti_spam
        self.settings = settings

    async def _load_admin_ids(self, session: AsyncSession | None) -> set[int]:
        if session is not None:
            try:
                repo = AppSettingsRepository(session)
                row = await repo.get()
                return coerce_int_set(effective_list_from_row(row, 'admin_ids', self.settings.admin_ids))
            except Exception:
                logger.exception('Failed to load anti-spam admin bypass IDs from AppSettings; falling back to env')

        return coerce_int_set(getattr(self.settings, 'admin_ids', None))

    async def _is_admin_bypass(self, tg_id: int, session: AsyncSession | None) -> bool:
        admin_ids = await self._load_admin_ids(session)
        return int(tg_id) in admin_ids

    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        from_user = getattr(event, 'from_user', None)
        if from_user is None:
            return await handler(event, data)

        session = data.get('session')
        if session is not None and not isinstance(session, AsyncSession):
            session = None

        if await self._is_admin_bypass(from_user.id, session):
            return await handler(event, data)

        if isinstance(event, CallbackQuery):
            allowed, retry_after = await self.anti_spam.check(from_user.id, 'callback')
            if allowed:
                return await handler(event, data)

            await event.answer(
                f'Слишком много нажатий. Повторите через {retry_after} сек.',
                show_alert=True,
            )
            return None

        if isinstance(event, Message):
            allowed, retry_after = await self.anti_spam.check(from_user.id, 'message')
            if allowed:
                return await handler(event, data)

            await event.answer(
                f'⏳ Слишком много сообщений. Попробуйте снова через {retry_after} сек.',
                reply_markup=main_menu(show_trial=False),
            )
            return None

        return await handler(event, data)