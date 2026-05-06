from __future__ import annotations

from aiogram import Router
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.repositories import AppSettingsRepository
from app.handlers.common import get_or_create_user
from app.keyboards.reply import main_menu
from app.utils.runtime_settings import coerce_int_set, effective_list_from_row

router = Router(name='fallback')


async def _is_admin_user(session: AsyncSession, tg_id: int, settings: Settings) -> bool:
    try:
        app_settings = await AppSettingsRepository(session).get()
        admin_ids = coerce_int_set(effective_list_from_row(app_settings, 'admin_ids', settings.admin_ids))
        return tg_id in admin_ids
    except Exception:
        return tg_id in coerce_int_set(settings.admin_ids)


@router.message()
async def fallback_message(message: Message, session: AsyncSession, settings: Settings) -> None:
    if message.chat.type != 'private':
        return

    user = await get_or_create_user(message, session)
    is_admin = await _is_admin_user(session, user.tg_id, settings)

    await message.answer(
        'Не совсем понял сообщение 🤔\nПожалуйста, выберите нужное действие кнопками ниже.',
        reply_markup=main_menu(
            show_trial=not bool(user.trial_issued_at),
            show_admin=is_admin,
        ),
    )
