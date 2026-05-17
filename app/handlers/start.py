from __future__ import annotations

import re
from collections.abc import Callable

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import AuditAction, AuditActorType
from app.db.repositories import (
    AppSettingsRepository,
    AuditLogRepository,
    TariffRepository,
    UserRepository,
)
from app.handlers.common import get_or_create_user
from app.keyboards.reply import main_menu
from app.services.referrals import ReferralService
from app.utils.runtime_settings import coerce_int_set, effective_list_from_row

router = Router(name='start')

REF_RE = re.compile(r'^ref(\d+)$', re.IGNORECASE)
TARIFF_TOKEN_RE = re.compile(r'^tariff_([A-Za-z0-9_\-]{8,64})$')


async def _try_unlock_tariff_by_token(
    session: AsyncSession,
    user_tg_id: int,
    token: str,
    _: Callable[[str], str],
) -> tuple[bool, str | None]:
    """FEA-ADMIN-TARIFF-PLUS: deep-link `start=tariff_<token>` →
    разблокировать тариф для текущего пользователя.

    Возвращает (unlocked, message) — `unlocked=True` если тариф был
    добавлен в `User.unlocked_tariff_ids` сейчас или ранее (reply
    одинаковый, чтобы не утекать факт «уже unlocked» как side-channel).
    `message=None` если токен не нашёлся / тариф неактивен / архив —
    игнорим тихо, как обычный непонятный `/start <args>`.
    """
    plan = await TariffRepository(session).get_by_private_token(token)
    if plan is None or not getattr(plan, 'is_active', False) or getattr(plan, 'is_archived', False):
        return False, None

    user_repo = UserRepository(session)
    user = await user_repo.get_by_tg_id_for_update(user_tg_id)
    if user is None:
        return False, None

    added = await user_repo.add_unlocked_tariff(user, plan.id)
    if added:
        await AuditLogRepository(session).create(
            action=AuditAction.tariff_unlock_granted,
            actor_type=AuditActorType.user,
            actor_tg_id=user_tg_id,
            entity_type='tariff_plan',
            entity_id=str(plan.id),
            details={
                'source': 'deep_link',
                'tariff_code': plan.code,
                'visibility': plan.visibility.value if hasattr(plan.visibility, 'value') else str(plan.visibility),
            },
        )
    return True, (
        _('🔓 Тариф «{title}» разблокирован для вашего аккаунта.').format(title=plan.title)
        + '\n'
        + _('Откройте «Купить VPN», чтобы оформить подписку по нему.')
    )


async def _is_admin_user(session: AsyncSession, tg_id: int, settings: Settings) -> bool:
    try:
        app_settings = await AppSettingsRepository(session).get()
        admin_ids = coerce_int_set(effective_list_from_row(app_settings, 'admin_ids', settings.admin_ids))
        return tg_id in admin_ids
    except Exception:
        return tg_id in coerce_int_set(settings.admin_ids)


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    _: Callable[[str], str],
) -> None:
    user = await get_or_create_user(message, session)

    # deep-link: /start ref123 | /start tariff_<token>
    args = (message.text or '').split(maxsplit=1)
    if len(args) == 2:
        payload = args[1].strip()
        m_ref = REF_RE.match(payload)
        if m_ref:
            inviter_tg_id = int(m_ref.group(1))
            bound, _bind_msg = await ReferralService(session).bind_inviter_by_link(user.tg_id, inviter_tg_id)
            if bound:
                await message.answer(_('🎉 Приглашение принято. Реферальный бонус будет начислен после вашей первой оплаты.'))
        else:
            m_tariff = TARIFF_TOKEN_RE.match(payload)
            if m_tariff:
                _unlocked, unlock_msg = await _try_unlock_tariff_by_token(
                    session, user.tg_id, m_tariff.group(1), _
                )
                if unlock_msg:
                    await message.answer(unlock_msg)

    is_admin = await _is_admin_user(session, user.tg_id, settings)

    await message.answer(
        _('Привет! 👋\n\nВыберите действие в меню ниже.'),
        reply_markup=main_menu(
            show_trial=not bool(user.trial_issued_at),
            show_admin=is_admin,
            translator=_,
        ),
    )
