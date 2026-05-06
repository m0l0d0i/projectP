from __future__ import annotations

import re

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.handlers.common import get_or_create_user
from app.db.repositories import AppSettingsRepository
from app.keyboards.reply import main_menu
from app.services.referrals import ReferralService
from app.utils.runtime_settings import coerce_int_set, effective_list_from_row

router = Router(name='start')

REF_RE = re.compile(r'^ref(\d+)$', re.IGNORECASE)


async def _is_admin_user(session: AsyncSession, tg_id: int, settings: Settings) -> bool:
    try:
        app_settings = await AppSettingsRepository(session).get()
        admin_ids = coerce_int_set(effective_list_from_row(app_settings, 'admin_ids', settings.admin_ids))
        return tg_id in admin_ids
    except Exception:
        return tg_id in coerce_int_set(settings.admin_ids)


@router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession, settings: Settings) -> None:
    user = await get_or_create_user(message, session)

    # deep-link: /start ref123
    args = (message.text or '').split(maxsplit=1)
    if len(args) == 2:
        m = REF_RE.match(args[1].strip())
        if m:
            inviter_tg_id = int(m.group(1))
            bound, _ = await ReferralService(session).bind_inviter_by_link(user.tg_id, inviter_tg_id)
            if bound:
                await message.answer('🎉 Приглашение принято. Реферальный бонус будет начислен после вашей первой оплаты.')

    is_admin = await _is_admin_user(session, user.tg_id, settings)

    await message.answer(
        'Привет! 👋\n\nВыберите действие в меню ниже.',
        reply_markup=main_menu(
            show_trial=not bool(user.trial_issued_at),
            show_admin=is_admin,
        ),
    )
