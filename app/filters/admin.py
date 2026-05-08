from __future__ import annotations

import logging

from aiogram.filters import BaseFilter
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.repositories import AppSettingsRepository
from app.utils.runtime_settings import coerce_int_set, effective_list_from_row

logger = logging.getLogger(__name__)


async def load_admin_ids(session: AsyncSession | None, settings: Settings) -> set[int]:
    env_ids = coerce_int_set(getattr(settings, 'admin_ids', None))
    if session is None:
        return env_ids
    try:
        row = await AppSettingsRepository(session).get()
        return coerce_int_set(effective_list_from_row(row, 'admin_ids', settings.admin_ids))
    except Exception:
        logger.exception('Failed to load admin_ids from AppSettings; falling back to env admin_ids')
        return env_ids


class IsAdminFilter(BaseFilter):
    async def __call__(
        self,
        event: TelegramObject,
        session: AsyncSession | None = None,
        settings: Settings | None = None,
    ) -> bool:
        from_user = getattr(event, 'from_user', None)
        if from_user is None or settings is None:
            return False
        admin_ids = await load_admin_ids(session, settings)
        return int(from_user.id) in admin_ids
