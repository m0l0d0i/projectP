from __future__ import annotations

from decimal import Decimal

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.text_decorations import html_decoration as fmt
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import SubscriptionRepository
from app.handlers.common import get_or_create_user
from app.keyboards.inline import ProfileCallback, profile_back_keyboard, profile_keyboard
from app.services.promos import PromoService
from app.services.referrals import ReferralService
from app.states.profile import ProfileState
from app.utils.telegram import safe_edit_message_text, safe_edit_reply_markup

router = Router(name='profile')


def _format_balance(balance: Decimal | int | float | None) -> str:
    value = Decimal(str(balance or 0))
    if value == value.to_integral_value():
        return f'{int(value)}'
    return format(value.normalize(), 'f').rstrip('0').rstrip('.')


def _yes_no(value: bool) -> str:
    return 'Да' if value else 'Нет'


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
) -> str:
    return (
        '👤 <b>Мой профиль</b>\n\n'
        f'🆔 <b>ID аккаунта:</b> <code>{tg_id}</code>\n'
        f'💰 <b>Баланс:</b> {_format_balance(balance)} ₽\n\n'
        '🌐 <b>Ваши услуги</b>\n'
        f'• Всего: <b>{total}</b>\n'
        f'• Активных: <b>{active}</b>\n'
        f'• Неактивных: <b>{inactive}</b>\n\n'
        '🤝 <b>Реферальная программа</b>\n'
        f'• Приглашено: <b>{invited_count}</b>\n'
        f'• Реферальный баланс: <b>{_format_balance(ref_balance)} ₽</b>\n'
        f'• Можно ввести реферальный код: <b>{_yes_no(can_use_referral_code)}</b>'
    )


async def _show_profile(
    target,
    session: AsyncSession,
    state: FSMContext | None = None,
    *,
    edit: bool = False,
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
    )
    markup = profile_keyboard(show_referral_code=show_referral_code)

    if edit:
        await safe_edit_message_text(target.message, text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


@router.message(F.text == '👤 Мой профиль')
async def profile_home(message: Message, session: AsyncSession, state: FSMContext) -> None:
    await _show_profile(message, session, state)


@router.callback_query(ProfileCallback.filter(F.action == 'back'))
async def profile_back(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await _show_profile(callback, session, state, edit=True)
    await callback.answer()


@router.callback_query(ProfileCallback.filter(F.action == 'ref_program'))
async def profile_ref_program(callback: CallbackQuery, session: AsyncSession) -> None:
    user = await get_or_create_user(callback, session)
    me = await callback.bot.get_me()
    invited_count, ref_balance = await ReferralService(session).stats_for_inviter(user.id)
    ref_link = f'https://t.me/{me.username}?start=ref{user.tg_id}'

    text = (
        '🤝 <b>Реферальная программа</b>\n\n'
        '🔗 <b>Ваша реферальная ссылка:</b>\n'
        f'<code>{fmt.quote(ref_link)}</code>\n\n'
        f'🎟️ <b>Ваш реферальный код:</b> <code>{fmt.quote(user.referral_code)}</code>\n\n'
        '📊 <b>Статистика:</b>\n'
        f'👥 Приглашённые: <b>{invited_count}</b>\n'
        f'💸 Реферальный баланс: <b>{_format_balance(ref_balance)} ₽</b>\n\n'
        'Ссылку и код удобно копировать прямо из блока выше.'
    )
    await safe_edit_message_text(
        callback.message,
        text,
        reply_markup=profile_back_keyboard(),
        disable_web_page_preview=True,
    )
    await callback.answer()


@router.callback_query(ProfileCallback.filter(F.action == 'enter_promo'))
async def profile_enter_promo(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ProfileState.waiting_promo_code)
    await safe_edit_reply_markup(callback.message, reply_markup=None)
    await callback.message.answer('🎁 Введите промокод сообщением:')
    await callback.answer()


@router.message(ProfileState.waiting_promo_code, ~F.text.startswith('/'))
async def profile_promo_input(message: Message, session: AsyncSession, state: FSMContext) -> None:
    user = await get_or_create_user(message, session)
    code = (message.text or '').strip()
    _ok, msg = await PromoService(session).redeem(user.tg_id, code)
    await message.answer(msg)
    await state.clear()


@router.callback_query(ProfileCallback.filter(F.action == 'referral_code'))
async def profile_enter_referral_code(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    user = await get_or_create_user(callback, session)
    allowed = await ReferralService(session).can_use_referral_code(user.tg_id)
    if not allowed:
        await callback.answer('Промокод реферала уже нельзя использовать.', show_alert=True)
        return

    await state.set_state(ProfileState.waiting_referral_code)
    await safe_edit_reply_markup(callback.message, reply_markup=None)
    await callback.message.answer('🎟️ Введите промокод реферала сообщением:')
    await callback.answer()


@router.message(ProfileState.waiting_referral_code, ~F.text.startswith('/'))
async def profile_referral_code_input(message: Message, session: AsyncSession, state: FSMContext) -> None:
    user = await get_or_create_user(message, session)
    code = (message.text or '').strip()
    _ok, msg = await ReferralService(session).redeem_referral_code(user.tg_id, code)
    await message.answer(msg)
    await state.clear()