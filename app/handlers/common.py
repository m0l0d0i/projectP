from __future__ import annotations

import logging

from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User
from app.db.repositories import UserRepository

logger = logging.getLogger(__name__)


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _normalize_username(value: str | None) -> str | None:
    normalized = _normalize_optional_text(value)
    if normalized is None:
        return None
    return normalized.lower()


async def get_or_create_user(event: Message | CallbackQuery, session: AsyncSession) -> User:
    tg_user = event.from_user
    if tg_user is None:
        raise ValueError('Telegram user is missing')

    user_repo = UserRepository(session)

    normalized_username = _normalize_username(tg_user.username)
    normalized_first_name = _normalize_optional_text(tg_user.first_name)
    normalized_last_name = _normalize_optional_text(tg_user.last_name)

    user, _created = await user_repo.create_or_get(
        tg_id=tg_user.id,
        username=normalized_username,
        first_name=normalized_first_name,
        last_name=normalized_last_name,
    )

    locked_user = await user_repo.get_by_id_for_update(user.id)
    if locked_user is None:
        raise ValueError(f'User disappeared during update: id={user.id}')

    changed = False

    if getattr(locked_user, 'username', None) != normalized_username:
        locked_user.username = normalized_username
        changed = True

    if getattr(locked_user, 'first_name', None) != normalized_first_name:
        locked_user.first_name = normalized_first_name
        changed = True

    if getattr(locked_user, 'last_name', None) != normalized_last_name:
        locked_user.last_name = normalized_last_name
        changed = True

    if locked_user.bot_blocked:
        locked_user.bot_blocked = False
        locked_user.bot_blocked_at = None
        locked_user.bot_blocked_reason = None
        changed = True
        logger.info('User %s unblocked the bot. Delivery status restored.', locked_user.tg_id)

    if changed:
        await session.flush()

    return locked_user