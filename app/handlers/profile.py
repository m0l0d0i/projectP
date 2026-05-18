from __future__ import annotations

import json
from collections.abc import Callable
from decimal import Decimal

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.text_decorations import html_decoration as fmt
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import AuditActorType
from app.db.repositories import SubscriptionRepository
from app.handlers.common import get_or_create_user
from app.i18n import all_translations
from app.keyboards.inline import (
    ProfileCallback,
    erase_confirm_keyboard,
    profile_keyboard,
    referral_list_back_keyboard,
    referral_program_keyboard,
)
from app.services.marzban import MarzbanClient
from app.services.privacy import PrivacyService
from app.services.promos import PromoService
from app.services.referrals import ReferralService
from app.states.profile import ProfileState
from app.utils.formatters import format_dt
from app.utils.telegram import safe_edit_message_text, safe_edit_reply_markup

router = Router(name='profile')


def _format_balance(balance: Decimal | int | float | None) -> str:
    value = Decimal(str(balance or 0))
    if value == value.to_integral_value():
        return f'{int(value)}'
    return format(value.normalize(), 'f').rstrip('0').rstrip('.')


def _yes_no(value: bool, _: Callable[[str], str]) -> str:
    return _('Да') if value else _('Нет')


def _profile_text(
    *,
    tg_id: int,
    balance: Decimal | int | float,
    total: int,
    active: int,
    inactive: int,
    invited_count: int,
    ref_balance: Decimal | int | float,
    can_use_referral_code: bool,
    _: Callable[[str], str],
) -> str:
    return (
        _('👤 <b>Мой профиль</b>') + '\n\n'
        + _('🆔 <b>ID аккаунта:</b> <code>{tg_id}</code>').format(tg_id=tg_id) + '\n'
        + _('💰 <b>Баланс:</b> {value} ₽').format(value=_format_balance(balance)) + '\n\n'
        + _('🌐 <b>Ваши услуги</b>') + '\n'
        + _('• Всего: <b>{value}</b>').format(value=total) + '\n'
        + _('• Активных: <b>{value}</b>').format(value=active) + '\n'
        + _('• Неактивных: <b>{value}</b>').format(value=inactive) + '\n\n'
        + _('🤝 <b>Реферальная программа</b>') + '\n'
        + _('• Приглашено: <b>{value}</b>').format(value=invited_count) + '\n'
        + _('• Реферальный баланс: <b>{value} ₽</b>').format(value=_format_balance(ref_balance)) + '\n'
        + _('• Можно ввести реферальный код: <b>{value}</b>').format(value=_yes_no(can_use_referral_code, _))
    )


async def _show_profile(
    target,
    session: AsyncSession,
    state: FSMContext | None = None,
    *,
    edit: bool = False,
    _: Callable[[str], str] = lambda s: s,
) -> None:
    if state:
        await state.clear()

    user = await get_or_create_user(target, session)
    subs = await SubscriptionRepository(session).list_by_user_id(user.id)

    active = sum(1 for sub in subs if sub.is_alive_local)
    total = len(subs)
    inactive = total - active

    show_referral_code = await ReferralService(session).can_use_referral_code(user.tg_id)
    invited_count, ref_balance = await ReferralService(session).stats_for_inviter(user.id)

    text = _profile_text(
        tg_id=user.tg_id,
        balance=user.balance,
        total=total,
        active=active,
        inactive=inactive,
        invited_count=invited_count,
        ref_balance=ref_balance,
        can_use_referral_code=show_referral_code,
        _=_,
    )
    markup = profile_keyboard(show_referral_code=show_referral_code)

    if edit:
        await safe_edit_message_text(target.message, text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


@router.message(F.text.in_(all_translations('👤 Мой профиль')))
async def profile_home(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
    _: Callable[[str], str] = lambda s: s,
) -> None:
    await _show_profile(message, session, state, _=_)


@router.callback_query(ProfileCallback.filter(F.action == 'back'))
async def profile_back(
    callback: CallbackQuery,
    session: AsyncSession,
    state: FSMContext,
    _: Callable[[str], str],
) -> None:
    await _show_profile(callback, session, state, edit=True, _=_)
    await callback.answer()


@router.callback_query(ProfileCallback.filter(F.action == 'ref_program'))
async def profile_ref_program(
    callback: CallbackQuery,
    session: AsyncSession,
    _: Callable[[str], str],
) -> None:
    user = await get_or_create_user(callback, session)
    me = await callback.bot.get_me()
    invited_count, ref_balance = await ReferralService(session).stats_for_inviter(user.id)
    ref_link = f'https://t.me/{me.username}?start=ref{user.tg_id}'

    text = (
        _('🤝 <b>Реферальная программа</b>') + '\n\n'
        + _('🔗 <b>Ваша реферальная ссылка:</b>') + '\n'
        + f'<code>{fmt.quote(ref_link)}</code>\n\n'
        + _('🎟️ <b>Ваш реферальный код:</b> <code>{value}</code>').format(value=fmt.quote(user.referral_code)) + '\n\n'
        + _('📊 <b>Статистика:</b>') + '\n'
        + _('👥 Приглашённые: <b>{value}</b>').format(value=invited_count) + '\n'
        + _('💸 Реферальный баланс: <b>{value} ₽</b>').format(value=_format_balance(ref_balance)) + '\n\n'
        + _('Ссылку и код удобно копировать прямо из блока выше.') + '\n'
        + _('Нажмите «Мои рефералы», чтобы посмотреть список приглашённых и статус активации.')
    )
    await safe_edit_message_text(
        callback.message,
        text,
        reply_markup=referral_program_keyboard(),
        disable_web_page_preview=True,
    )
    await callback.answer()


_MY_REFERRALS_LIMIT = 15


def _format_referral_row(idx: int, item: dict, _: Callable[[str], str]) -> str:
    if item['invited_username']:
        who = f'@{fmt.quote(item["invited_username"])}'
    elif item['invited_first_name']:
        who = f'{fmt.quote(item["invited_first_name"])} (tg <code>{item["invited_tg_id"]}</code>)'
    else:
        who = f'tg <code>{item["invited_tg_id"]}</code>'

    if item['is_activated']:
        when = format_dt(item['activated_at']) if item['activated_at'] else format_dt(item['created_at'])
        status = _('✅ активирован · {value}').format(value=when)
    else:
        status = _('⏳ ждёт первой оплаты')

    source_label = _('по ссылке') if item['source'] == 'link' else _('промокод')
    return f'{idx}. {who} — {status} · {source_label}'


@router.callback_query(ProfileCallback.filter(F.action == 'my_referrals'))
async def profile_my_referrals(
    callback: CallbackQuery,
    session: AsyncSession,
    _: Callable[[str], str],
) -> None:
    user = await get_or_create_user(callback, session)
    service = ReferralService(session)
    invited_count, ref_balance = await service.stats_for_inviter(user.id)
    items = await service.list_referrals_for_inviter(user.id, limit=_MY_REFERRALS_LIMIT)
    activated_count = sum(1 for it in items if it['is_activated'])

    if not items:
        body = _('У вас пока нет приглашённых. Поделитесь ссылкой или кодом из экрана реферальной программы.')
    else:
        rows = [_format_referral_row(idx, item, _) for idx, item in enumerate(items, 1)]
        body = '\n'.join(rows)
        if invited_count > len(items):
            body += '\n\n' + _('<i>Показаны последние {shown} из {total}. Старые приглашения скрыты.</i>').format(
                shown=len(items), total=invited_count,
            )

    text = (
        _('👥 <b>Мои рефералы</b>') + '\n\n'
        + _('📊 Всего приглашено: <b>{value}</b>').format(value=invited_count) + '\n'
        + _('✅ Активировано: <b>{value}</b>').format(value=activated_count) + '\n'
        + _('💸 Реферальный баланс: <b>{value} ₽</b>').format(value=_format_balance(ref_balance)) + '\n\n'
        + f'{body}'
    )
    await safe_edit_message_text(
        callback.message,
        text,
        reply_markup=referral_list_back_keyboard(),
        disable_web_page_preview=True,
    )
    await callback.answer()


@router.callback_query(ProfileCallback.filter(F.action == 'enter_promo'))
async def profile_enter_promo(
    callback: CallbackQuery,
    state: FSMContext,
    _: Callable[[str], str],
) -> None:
    await state.set_state(ProfileState.waiting_promo_code)
    await safe_edit_reply_markup(callback.message, reply_markup=None)
    await callback.message.answer(_('🎁 Введите промокод сообщением:'))
    await callback.answer()


@router.message(ProfileState.waiting_promo_code, ~F.text.startswith('/'))
async def profile_promo_input(message: Message, session: AsyncSession, state: FSMContext) -> None:
    user = await get_or_create_user(message, session)
    code = (message.text or '').strip()
    _ok, msg = await PromoService(session).redeem(user.tg_id, code)
    await message.answer(msg)
    await state.clear()


@router.callback_query(ProfileCallback.filter(F.action == 'referral_code'))
async def profile_enter_referral_code(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    _: Callable[[str], str],
) -> None:
    user = await get_or_create_user(callback, session)
    allowed = await ReferralService(session).can_use_referral_code(user.tg_id)
    if not allowed:
        await callback.answer(_('Промокод реферала уже нельзя использовать.'), show_alert=True)
        return

    await state.set_state(ProfileState.waiting_referral_code)
    await safe_edit_reply_markup(callback.message, reply_markup=None)
    await callback.message.answer(_('🎟️ Введите промокод реферала сообщением:'))
    await callback.answer()


@router.message(ProfileState.waiting_referral_code, ~F.text.startswith('/'))
async def profile_referral_code_input(message: Message, session: AsyncSession, state: FSMContext) -> None:
    user = await get_or_create_user(message, session)
    code = (message.text or '').strip()
    _ok, msg = await ReferralService(session).redeem_referral_code(user.tg_id, code)
    await message.answer(msg)
    await state.clear()


# CMP-1 GDPR: self-service export + erase из бота.

@router.callback_query(ProfileCallback.filter(F.action == 'export_data'))
async def profile_export_data(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    _: Callable[[str], str],
) -> None:
    from app.db.models import AuditAction
    from app.db.repositories import AuditLogRepository

    user = await get_or_create_user(callback, session)
    privacy = PrivacyService(session, settings)
    data = await privacy.export_user_data(user)
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')
    document = BufferedInputFile(payload, filename=f'vpn_bot_export_{user.id}.json')

    await AuditLogRepository(session).create(
        action=AuditAction.user_data_exported,
        actor_type=AuditActorType.user,
        actor_tg_id=user.tg_id,
        entity_type='user',
        entity_id=str(user.id),
        details={'source': 'bot_self_service'},
    )

    await callback.message.answer_document(
        document,
        caption=_('📦 Экспорт ваших данных (JSON). Файл содержит все PII, известные сервису.'),
    )
    await callback.answer()


@router.callback_query(ProfileCallback.filter(F.action == 'erase_request'))
async def profile_erase_request(
    callback: CallbackQuery,
    _: Callable[[str], str],
) -> None:
    text = (
        _('🗑 <b>Удаление аккаунта</b>') + '\n\n'
        + _('Будут <b>безвозвратно</b> удалены ваши имя/username, обнулён баланс, '
            'отключены все активные подписки (с deactivate в Marzban) и закрыты тикеты поддержки.') + '\n\n'
        + _('История оплаченных счетов остаётся для бухгалтерской отчётности '
            '(анонимизированно — без вашего имени), это требование законодательства РФ и AML.') + '\n\n'
        + _('Восстановить аккаунт после удаления нельзя. Подтверждаете?')
    )
    await safe_edit_message_text(callback.message, text, reply_markup=erase_confirm_keyboard())
    await callback.answer()


@router.callback_query(ProfileCallback.filter(F.action == 'erase_confirm'))
async def profile_erase_confirm(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    marzban: MarzbanClient,
    _: Callable[[str], str],
) -> None:
    user = await get_or_create_user(callback, session)
    if user.anonymized_at is not None:
        await callback.answer(_('Аккаунт уже удалён.'), show_alert=True)
        return

    privacy = PrivacyService(session, settings, marzban=marzban)
    await privacy.erase_user(
        user,
        actor_tg_id=user.tg_id,
        actor_type=AuditActorType.user,
    )
    await safe_edit_reply_markup(callback.message, reply_markup=None)
    await callback.message.answer(
        _('✅ Ваши данные удалены. Спасибо, что пользовались сервисом.'),
    )
    await callback.answer()
