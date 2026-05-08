from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.text_decorations import html_decoration as fmt
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.services.cache import CacheService
from app.db.models import (
    AuditAction,
    AuditActorType,
    BroadcastJobStatus,
    PromoCode,
    SupportTicketStatus,
    TransactionType,
)
from app.db.repositories import (
    AuditLogRepository,
    BroadcastJobRepository,
    PricingRuleRepository,
    PromoRepository,
    SupportMessageRepository,
    SupportTicketRepository,
    TransactionRepository,
    UserRepository,
)
from app.filters.admin import IsAdminFilter
from app.keyboards.inline import (
    AdminCallback,
    admin_back_keyboard,
    admin_broadcast_confirm_keyboard,
    admin_broadcast_job_keyboard,
    admin_broadcast_keyboard,
    admin_broadcast_schedule_keyboard,
    admin_main_keyboard,
    admin_price_keyboard,
    admin_promo_keyboard,
    admin_promos_keyboard,
    admin_ticket_keyboard,
    admin_tickets_keyboard,
    admin_user_keyboard,
    admin_users_keyboard,
)
from app.services.promos import PromoService
from app.services.tariffs import PricingService
from app.states.admin import AdminState
from app.utils.telegram import safe_edit_message_text

router = Router(name='admin_panel')
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())
logger = logging.getLogger('app.audit')

PAGE_SIZE = 8
PRICE_EDITABLE_FIELDS = {
    'base_price',
    'traffic_step_price',
    'device_step_price',
    'unlimited_devices_price',
    'unlimited_combo_price',
    'min_topup_amount',
    'max_discount_percent',
    'max_months',
    'base_traffic_gb',
    'traffic_step_gb',
}

ADMIN_UI_TZ = ZoneInfo('Europe/Moscow')
ADMIN_UI_TZ_LABEL = 'МСК'


async def _show_root(target, session: AsyncSession, settings: Settings, *, edit: bool = False) -> None:
    users_count = await UserRepository(session).count()
    text = (
        '⚙️ <b>Админ-панель</b>\n\n'
        f'👤 Пользователей в базе: {users_count}\n'
        'Выберите раздел ниже.'
    )
    if edit:
        await safe_edit_message_text(target.message, text, reply_markup=admin_main_keyboard())
    else:
        await target.answer(text, reply_markup=admin_main_keyboard())



def _normalize_admin_dt(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(ADMIN_UI_TZ)



def _format_admin_dt(value: datetime | None) -> str:
    normalized = _normalize_admin_dt(value)
    if normalized is None:
        return '—'
    return normalized.strftime(f'%d.%m.%Y %H:%M {ADMIN_UI_TZ_LABEL}')



def _format_admin_dt_short(value: datetime | None) -> str:
    normalized = _normalize_admin_dt(value)
    if normalized is None:
        return '—'
    return normalized.strftime('%Y-%m-%d %H:%M')



def _user_card(user) -> str:
    username = f'@{fmt.quote(user.username)}' if user.username else '—'
    full_name = ' '.join(
        part for part in [getattr(user, 'first_name', None), getattr(user, 'last_name', None)] if part
    ) or '—'
    return (
        '👤 <b>Пользователь</b>\n\n'
        f'ID: <code>{user.tg_id}</code>\n'
        f'Username: {username}\n'
        f'Имя: {fmt.quote(full_name)}\n'
        f'Баланс: {user.balance} ₽\n'
        f'Статус: {"🔒 Заблокирован" if user.is_blocked else "🟢 Активен"}\n'
        f'Рефкод: <code>{fmt.quote(user.referral_code)}</code>'
    )



def _promo_card(promo: PromoCode) -> str:
    exp = '∞' if promo.expires_at is None else _format_admin_dt(promo.expires_at)
    status_key = PromoService.resolve_admin_status(promo)
    status_label = {
        'active': '🟢 Активен',
        'archived': '⚪ Архивный',
        'expired': '🟠 Истёк',
        'exhausted': '🟡 Лимит исчерпан',
    }.get(status_key, '• Неизвестно')
    return (
        f'🎟 <b>{fmt.quote(promo.code)}</b>\n\n'
        f'Бонус: {promo.bonus_amount} ₽\n'
        f'Использований: {promo.used_count}/{promo.max_uses or "∞"}\n'
        f'Истекает: {exp}\n'
        f'Статус: {status_label}'
    )


def _ticket_status_label(status: SupportTicketStatus | str) -> str:
    normalized = status.value if isinstance(status, SupportTicketStatus) else str(status)
    return {
        SupportTicketStatus.waiting_operator.value: '🟠 Ждёт оператора',
        SupportTicketStatus.waiting_user.value: '🟡 Ждёт пользователя',
        SupportTicketStatus.closed.value: '🔴 Закрыт',
    }.get(normalized, f'• {fmt.quote(normalized)}')


def _ticket_is_active(ticket) -> bool:
    return getattr(ticket, 'status', None) in {
        SupportTicketStatus.waiting_operator,
        SupportTicketStatus.waiting_user,
    }


def _ticket_card(ticket) -> str:
    return (
        f'🎧 <b>Тикет #{ticket.id}</b>\n'
        f'Статус: {_ticket_status_label(ticket.status)}\n'
        f'Пользователь ID: {ticket.user_id}'
    )



def _admin_now_local() -> datetime:
    return datetime.now(ADMIN_UI_TZ)



def _parse_admin_run_at(raw: str) -> datetime:
    cleaned = (raw or '').strip()
    if not cleaned:
        raise ValueError('Введите дату в формате ДД.ММ ЧЧ:ММ')

    now_local = _admin_now_local()
    try:
        parsed = datetime.strptime(cleaned, '%d.%m %H:%M')
    except ValueError as exc:
        raise ValueError('Неверный формат. Используйте ДД.ММ ЧЧ:ММ') from exc

    try:
        candidate_local = datetime(
            year=now_local.year,
            month=parsed.month,
            day=parsed.day,
            hour=parsed.hour,
            minute=parsed.minute,
            tzinfo=ADMIN_UI_TZ,
        )
    except ValueError as exc:
        raise ValueError('Неверная дата. Используйте существующую дату в формате ДД.ММ ЧЧ:ММ') from exc

    if candidate_local <= now_local:
        if candidate_local.month < now_local.month:
            candidate_local = candidate_local.replace(year=now_local.year + 1)
        else:
            raise ValueError('Указанное время уже прошло. Выберите будущее время.')

    return candidate_local.astimezone(timezone.utc)



def _broadcast_status_label(status: BroadcastJobStatus | str) -> str:
    normalized = status.value if isinstance(status, BroadcastJobStatus) else str(status)
    if normalized == 'pending':
        normalized = BroadcastJobStatus.scheduled.value
    return {
        BroadcastJobStatus.draft.value: '📝 Черновик',
        BroadcastJobStatus.scheduled.value: '🕓 Запланирована',
        BroadcastJobStatus.running.value: '🚀 Выполняется',
        BroadcastJobStatus.completed.value: '✅ Завершена',
        BroadcastJobStatus.failed.value: '❌ Ошибка',
        BroadcastJobStatus.cancelled.value: '⛔ Отменена',
    }.get(normalized, f'• {fmt.quote(normalized)}')



def _broadcast_preview_text(text: str, run_at: datetime) -> str:
    preview = (text or '').strip()
    if len(preview) > 1000:
        preview = preview[:1000] + '…'
    return (
        '📢 <b>Превью рассылки</b>\n\n'
        f'Время отправки: {_format_admin_dt(run_at)}\n\n'
        f'Текст:\n<blockquote>{fmt.quote(preview)}</blockquote>\n\n'
        'Запланировать?'
    )



def _broadcast_card(job) -> str:
    preview = (job.text or '').strip()
    if len(preview) > 1000:
        preview = preview[:1000] + '…'
    return (
        f'📢 <b>Рассылка #{job.id}</b>\n\n'
        f'Статус: {_broadcast_status_label(job.status)}\n'
        f'Время отправки: {_format_admin_dt(job.run_at)}\n\n'
        f'Текст:\n<blockquote>{fmt.quote(preview or "—")}</blockquote>'
    )


async def _show_broadcasts(target, session: AsyncSession, *, edit: bool = False) -> None:
    jobs = await BroadcastJobRepository(session).list_recent(limit=20, status_filter=BroadcastJobRepository.STATUS_ALL)
    text = (
        '📢 <b>Запланированные рассылки</b>\n\n'
        'Здесь отображаются последние черновики, запланированные и завершённые рассылки.\n'
        'Выберите рассылку для просмотра или создайте новую.'
    )
    if edit:
        await safe_edit_message_text(target.message, text, reply_markup=admin_broadcast_keyboard(jobs))
    else:
        await target.answer(text, reply_markup=admin_broadcast_keyboard(jobs))


@router.message(Command('admin'))
@router.message(F.text == '⚙️ Админка')
async def admin_root(message: Message, session: AsyncSession, settings: Settings, state: FSMContext) -> None:
    await state.clear()
    await _show_root(message, session, settings)


@router.callback_query(AdminCallback.filter(F.section == 'root'))
async def admin_root_cb(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
) -> None:
    await state.clear()
    await _show_root(callback, session, settings, edit=True)
    await callback.answer()


@router.callback_query(AdminCallback.filter(F.section == 'noop'))
async def admin_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(AdminCallback.filter(F.section == 'users'))
async def admin_users(
    callback: CallbackQuery,
    callback_data: AdminCallback,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
    cache: CacheService,
) -> None:
    await state.clear()
    repo = UserRepository(session)
    audit_repo = AuditLogRepository(session)

    if callback_data.action == 'search':
        await state.set_state(AdminState.waiting_user_query)
        await callback.message.answer('🔎 Введите tg_id или username пользователя:')
        await callback.answer()
        return

    if callback_data.action == 'open' and callback_data.item_id:
        user = await repo.get_by_id(callback_data.item_id)
        if not user:
            await callback.answer('Пользователь не найден', show_alert=True)
            return
        await safe_edit_message_text(callback.message, _user_card(user), reply_markup=admin_user_keyboard(user))
        await callback.answer()
        return

    if callback_data.action in {'add_balance', 'remove_balance'} and callback_data.item_id:
        user = await repo.get_by_id(callback_data.item_id)
        if not user:
            await callback.answer('Пользователь не найден', show_alert=True)
            return
        await state.set_state(AdminState.waiting_balance_amount)
        await state.update_data(target_user_id=user.id, balance_action=callback_data.action)
        await callback.message.answer(
            f'Введите сумму для действия: {"начислить" if callback_data.action == "add_balance" else "списать"} пользователю {user.tg_id}'
        )
        await callback.answer()
        return

    if callback_data.action == 'toggle_block' and callback_data.item_id:
        user = await repo.get_by_id_for_update(callback_data.item_id)
        if not user:
            await callback.answer('Пользователь не найден', show_alert=True)
            return

        new_blocked_state = not user.is_blocked
        await repo.set_blocked(user, new_blocked_state, 'admin_toggle' if new_blocked_state else None)
        await session.flush()
        await cache.delete(f'user:{user.tg_id}')

        await audit_repo.create(
            action=AuditAction.admin_action,
            actor_type=AuditActorType.admin,
            actor_tg_id=callback.from_user.id,
            entity_type='user',
            entity_id=str(user.id),
            details={
                'action': 'toggle_block',
                'blocked': user.is_blocked,
                'target_tg_id': user.tg_id,
            },
        )
        logger.info(
            'admin_action tg_id=%s action=toggle_block target=%s blocked=%s',
            callback.from_user.id,
            user.tg_id,
            user.is_blocked,
        )
        await safe_edit_message_text(callback.message, _user_card(user), reply_markup=admin_user_keyboard(user))
        await callback.answer('Обновлено')
        return

    page = max(0, callback_data.page)
    users = await repo.list_recent(limit=PAGE_SIZE + 1, offset=page * PAGE_SIZE)
    has_next = len(users) > PAGE_SIZE
    users = users[:PAGE_SIZE]
    await safe_edit_message_text(
        callback.message,
        '👤 <b>Пользователи</b>',
        reply_markup=admin_users_keyboard(users, page, page > 0, has_next),
    )
    await callback.answer()


@router.message(AdminState.waiting_user_query)
async def admin_users_query(message: Message, session: AsyncSession, settings: Settings, state: FSMContext) -> None:
    users = await UserRepository(session).search(message.text or '', limit=20)
    await state.clear()
    if not users:
        await message.answer('Ничего не найдено.', reply_markup=admin_main_keyboard())
        return
    await message.answer('Результаты поиска:', reply_markup=admin_users_keyboard(users, 0, False, False))


@router.message(AdminState.waiting_balance_amount)
async def admin_balance_amount(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
    cache: CacheService,
) -> None:
    data = await state.get_data()
    try:
        amount = Decimal((message.text or '').replace(',', '.').strip())
        if amount <= 0:
            raise ValueError
    except Exception:
        await message.answer('Введите положительную сумму, например: 100')
        return

    user = await UserRepository(session).get_by_id_for_update(int(data['target_user_id']))
    if not user:
        await message.answer('Пользователь не найден')
        await state.clear()
        return

    user_repo = UserRepository(session)
    tx_repo = TransactionRepository(session)
    audit_repo = AuditLogRepository(session)

    if data['balance_action'] == 'add_balance':
        await user_repo.add_balance(user, amount)
        tx_type = TransactionType.income
        tx_desc = 'Начисление баланса через bot admin'
    else:
        if Decimal(str(user.balance)) < amount:
            await message.answer('Недостаточно средств на балансе пользователя.')
            return
        await user_repo.subtract_balance(user, amount)
        tx_type = TransactionType.outcome
        tx_desc = 'Списание баланса через bot admin'

    await tx_repo.create(user.id, amount, tx_type, tx_desc)
    await audit_repo.create(
        action=AuditAction.balance_adjusted,
        actor_type=AuditActorType.admin,
        actor_tg_id=message.from_user.id,
        entity_type='user',
        entity_id=str(user.id),
        details={
            'direction': data['balance_action'],
            'amount': str(amount),
            'target_tg_id': user.tg_id,
            'result_balance': str(user.balance),
        },
    )

    await session.flush()
    await cache.delete(f'user:{user.tg_id}')
    logger.info(
        'admin_action tg_id=%s action=%s target=%s amount=%s',
        message.from_user.id,
        data['balance_action'],
        user.tg_id,
        amount,
    )
    await state.clear()
    await message.answer('Баланс обновлен.', reply_markup=admin_user_keyboard(user))


@router.callback_query(AdminCallback.filter(F.section == 'promos'))
async def admin_promos(
    callback: CallbackQuery,
    callback_data: AdminCallback,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
) -> None:
    await state.clear()
    repo = PromoRepository(session)
    audit_repo = AuditLogRepository(session)

    if callback_data.action == 'create':
        await state.set_state(AdminState.waiting_promo_create)
        await callback.message.answer(
            'Введите: <code>CODE_OR_AUTO;BONUS;MAX_USES_OR_0;MINUTES_OR_0</code>\n'
            'Пример: <code>AUTO;50;100;1440</code>'
        )
        await callback.answer()
        return

    if callback_data.action == 'open' and callback_data.item_id:
        promo = await repo.get_by_id(callback_data.item_id)
        if not promo:
            await callback.answer('Промокод не найден', show_alert=True)
            return
        await safe_edit_message_text(callback.message, _promo_card(promo), reply_markup=admin_promo_keyboard(promo))
        await callback.answer()
        return

    if callback_data.action == 'toggle_active' and callback_data.item_id:
        promo = await repo.get_by_id_for_update(callback_data.item_id)
        if not promo:
            await callback.answer('Промокод не найден', show_alert=True)
            return

        promo = await PromoService(session).set_active(promo_id=promo.id, is_active=not promo.is_active)
        await session.flush()

        await audit_repo.create(
            action=AuditAction.admin_action,
            actor_type=AuditActorType.admin,
            actor_tg_id=callback.from_user.id,
            entity_type='promo_code',
            entity_id=str(promo.id),
            details={
                'action': 'toggle_active',
                'code': promo.code,
                'is_active': promo.is_active,
            },
        )
        logger.info(
            'admin_action tg_id=%s action=promo_toggle target=%s active=%s',
            callback.from_user.id,
            promo.code,
            promo.is_active,
        )
        await callback.answer('Обновлено')
        await admin_promos(
            callback,
            AdminCallback(section='promos', action='open', item_id=promo.id),
            session,
            settings,
            state,
        )
        return

    if callback_data.action == 'delete' and callback_data.item_id:
        promo = await repo.get_by_id_for_update(callback_data.item_id)
        if not promo:
            await callback.answer('Промокод не найден', show_alert=True)
            return

        code = promo.code
        promo_id = promo.id
        try:
            await PromoService(session).delete_promo(promo.id)
        except ValueError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        await session.flush()

        await audit_repo.create(
            action=AuditAction.admin_action,
            actor_type=AuditActorType.admin,
            actor_tg_id=callback.from_user.id,
            entity_type='promo_code',
            entity_id=str(promo_id),
            details={
                'action': 'delete',
                'code': code,
            },
        )
        logger.info('admin_action tg_id=%s action=promo_delete target=%s', callback.from_user.id, code)
        await callback.answer('Удалено')

    if callback_data.action == 'edit' and callback_data.item_id:
        await state.set_state(AdminState.waiting_promo_edit)
        await state.update_data(promo_id=callback_data.item_id)
        await callback.message.answer('Введите: <code>bonus;max_uses_or_0;minutes_or_0;active(0|1)</code>')
        await callback.answer()
        return

    page = max(0, callback_data.page)
    offset = page * PAGE_SIZE
    promos = await repo.list_recent(limit=PAGE_SIZE, offset=offset)
    total = await repo.count()
    await safe_edit_message_text(
        callback.message,
        '🎟 <b>Промокоды</b>',
        reply_markup=admin_promos_keyboard(promos, page, page > 0, offset + PAGE_SIZE < total),
    )
    await callback.answer()


@router.message(AdminState.waiting_promo_create)
async def admin_promo_create_input(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
) -> None:
    try:
        code, bonus, max_uses, minutes = [part.strip() for part in (message.text or '').split(';')]
        final_code = None if code.upper() == 'AUTO' else code.upper()
        bonus_amount = Decimal(bonus)
        resolved_max_uses = None if int(max_uses) == 0 else int(max_uses)
        duration_minutes = None if int(minutes) == 0 else int(minutes)
    except (ValueError, InvalidOperation):
        await message.answer('Неверный формат. Используй: CODE_OR_AUTO;BONUS;MAX_USES_OR_0;MINUTES_OR_0')
        return

    try:
        created = await PromoService(session).create_promo(
            code=final_code,
            bonus_amount=bonus_amount,
            max_uses=resolved_max_uses,
            duration_minutes=duration_minutes,
            created_by_tg_id=message.from_user.id,
        )
    except IntegrityError:
        await message.answer('Такой промокод уже существует!')
        return
    except Exception:
        logger.exception('Unexpected error while creating promo from bot admin')
        await message.answer('Не удалось создать промокод из-за внутренней ошибки. Проверь логи.')
        return

    created_code = created.code if hasattr(created, 'code') else str(created)

    await AuditLogRepository(session).create(
        action=AuditAction.promo_created,
        actor_type=AuditActorType.admin,
        actor_tg_id=message.from_user.id,
        entity_type='promo_code',
        entity_id=created_code,
        details={
            'code': created_code,
            'bonus_amount': str(bonus_amount),
            'max_uses': resolved_max_uses,
            'duration_minutes': duration_minutes,
        },
    )

    await session.flush()
    logger.info('admin_action tg_id=%s action=promo_create code=%s', message.from_user.id, created_code)
    await state.clear()
    await message.answer(f'Промокод создан: <code>{created_code}</code>', reply_markup=admin_main_keyboard())


@router.message(AdminState.waiting_promo_edit)
async def admin_promo_edit_input(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    promo = await PromoRepository(session).get_by_id_for_update(int(data['promo_id']))
    if not promo:
        await message.answer('Промокод не найден')
        await state.clear()
        return

    try:
        bonus, max_uses, minutes, active = [part.strip() for part in (message.text or '').split(';')]
        expires_at = None if int(minutes) == 0 else datetime.now(timezone.utc) + timedelta(minutes=int(minutes))
        promo = await PromoService(session).update_promo(
            promo_id=promo.id,
            code=promo.code,
            bonus_amount=Decimal(bonus),
            max_uses=None if int(max_uses) == 0 else int(max_uses),
            expires_at=expires_at,
            is_active=active in {'1', 'true', 'yes', 'on'},
        )
    except Exception:
        await message.answer('Неверный формат. Используй: bonus;max_uses_or_0;minutes_or_0;active(0|1)')
        return

    await AuditLogRepository(session).create(
        action=AuditAction.admin_action,
        actor_type=AuditActorType.admin,
        actor_tg_id=message.from_user.id,
        entity_type='promo_code',
        entity_id=str(promo.id),
        details={
            'action': 'edit',
            'code': promo.code,
            'bonus_amount': str(promo.bonus_amount),
            'max_uses': promo.max_uses,
            'expires_at': promo.expires_at.isoformat() if promo.expires_at else None,
            'is_active': promo.is_active,
        },
    )

    await session.flush()
    logger.info('admin_action tg_id=%s action=promo_edit code=%s', message.from_user.id, promo.code)
    await state.clear()
    await message.answer('Промокод обновлен.', reply_markup=admin_main_keyboard())


@router.callback_query(AdminCallback.filter(F.section == 'tickets'))
async def admin_tickets(
    callback: CallbackQuery,
    callback_data: AdminCallback,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
) -> None:
    await state.clear()
    repo = SupportTicketRepository(session)

    if callback_data.action == 'open' and callback_data.item_id:
        ticket = await repo.get_by_id(callback_data.item_id)
        if not ticket:
            await callback.answer('Тикет не найден', show_alert=True)
            return
        await safe_edit_message_text(
            callback.message,
            _ticket_card(ticket),
            reply_markup=admin_ticket_keyboard(ticket.id, _ticket_is_active(ticket)),
        )
        await callback.answer()
        return

    if callback_data.action == 'close' and callback_data.item_id:
        ticket = await repo.get_by_id_for_update(callback_data.item_id)
        if not ticket:
            await callback.answer('Тикет не найден', show_alert=True)
            return

        closed_now = await repo.close(
            ticket,
            reason='admin_panel',
            closed_by_admin_tg_id=callback.from_user.id,
            actor_type='admin',
            actor_tg_id=callback.from_user.id,
        )
        if not closed_now:
            await callback.answer('Тикет уже закрыт', show_alert=True)
            await safe_edit_message_text(
                callback.message,
                _ticket_card(ticket),
                reply_markup=admin_ticket_keyboard(ticket.id, _ticket_is_active(ticket)),
            )
            return

        await AuditLogRepository(session).create(
            action=AuditAction.ticket_closed,
            actor_type=AuditActorType.admin,
            actor_tg_id=callback.from_user.id,
            entity_type='support_ticket',
            entity_id=str(ticket.id),
            details={'reason': 'admin_panel'},
        )
        await session.flush()
        logger.info('admin_action tg_id=%s action=ticket_close ticket=%s', callback.from_user.id, ticket.id)
        await callback.answer('Тикет закрыт')
        await admin_tickets(
            callback,
            AdminCallback(section='tickets', action='open', item_id=ticket.id),
            session,
            settings,
            state,
        )
        return

    if callback_data.action == 'history' and callback_data.item_id:
        messages = await SupportMessageRepository(session).list_by_ticket(callback_data.item_id)
        body: list[str] = []
        for row in messages[-20:]:
            sender_label = 'Q' if row.sender_type.value == 'user' else 'A'
            rendered_text = fmt.quote(row.text or '(медиа)')
            body.append(
                f'[{_format_admin_dt_short(row.created_at)}] {sender_label}: {rendered_text}'
            )
        text = '\n'.join(body) if body else 'Сообщений нет.'
        await callback.message.answer(f'История тикета #{callback_data.item_id}\n\n{text[:3500]}')
        await callback.answer()
        return

    page = max(0, callback_data.page)
    offset = page * PAGE_SIZE
    tickets = await repo.list_recent(limit=PAGE_SIZE, offset=offset)
    total = await repo.count()
    await safe_edit_message_text(
        callback.message,
        '🎧 <b>Тикеты поддержки</b>',
        reply_markup=admin_tickets_keyboard(tickets, page, page > 0, offset + PAGE_SIZE < total),
    )
    await callback.answer()


@router.callback_query(AdminCallback.filter(F.section == 'price'))
async def admin_price(
    callback: CallbackQuery,
    callback_data: AdminCallback,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
) -> None:
    if callback_data.action == 'edit' and callback_data.field:
        await state.set_state(AdminState.waiting_price_edit)
        await state.update_data(price_field=callback_data.field)
        await callback.message.answer(f'Введите новое значение для поля: <code>{callback_data.field}</code>')
        await callback.answer()
        return

    rules = await PricingService.get_rules(session)
    text = (
        '💸 <b>Текущие правила ценообразования</b>\n\n'
        f'base_price = {rules.base_price}\n'
        f'traffic_step_price = {rules.traffic_step_price}\n'
        f'device_step_price = {rules.device_step_price}\n'
        f'unlimited_devices_price = {rules.unlimited_devices_price}\n'
        f'unlimited_combo_price = {rules.unlimited_combo_price}\n'
        f'min_topup_amount = {rules.min_topup_amount}\n'
        f'max_discount_percent = {rules.max_discount_percent}\n'
        f'max_months = {rules.max_months}\n'
        f'base_traffic_gb = {rules.base_traffic_gb}\n'
        f'traffic_step_gb = {rules.traffic_step_gb}'
    )
    try:
        await safe_edit_message_text(callback.message, text, reply_markup=admin_price_keyboard())
    except Exception:
        await callback.message.answer(text, reply_markup=admin_price_keyboard())
    await callback.answer()


@router.message(AdminState.waiting_price_edit)
async def admin_price_edit_input(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    field = data.get('price_field')
    if field not in PRICE_EDITABLE_FIELDS:
        await state.clear()
        await message.answer('Поле недоступно для редактирования.', reply_markup=admin_main_keyboard())
        return

    repo = PricingRuleRepository(session)
    row = await (repo.ensure() if hasattr(repo, 'ensure') else repo.get_or_create())

    try:
        if field in {'max_months', 'base_traffic_gb', 'traffic_step_gb'}:
            value = int((message.text or '').strip())
        else:
            value = Decimal((message.text or '').replace(',', '.').strip())
    except Exception:
        await message.answer('Введите корректное числовое значение.')
        return

    setattr(row, field, value)

    await AuditLogRepository(session).create(
        action=AuditAction.pricing_updated,
        actor_type=AuditActorType.admin,
        actor_tg_id=message.from_user.id,
        entity_type='pricing_rules',
        entity_id='1',
        details={
            'field': field,
            'value': str(value),
        },
    )

    await session.flush()
    logger.info('admin_action tg_id=%s action=price_edit field=%s value=%s', message.from_user.id, field, value)
    await state.clear()
    await message.answer('Параметр обновлен.', reply_markup=admin_main_keyboard())


@router.callback_query(AdminCallback.filter(F.section == 'tariffs'))
async def admin_tariffs(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    rules = await PricingService.get_rules(session)
    plans = await PricingService.list_plans(session)
    text = ['📦 <b>Тарифный конструктор</b>', '']
    for plan in plans:
        if plan.monthly_traffic_gb is None:
            text.append(f'♾️ {fmt.quote(plan.title)} — {rules.unlimited_combo_price} ₽/мес')
        else:
            single = PricingService.calculate_monthly_price(plan.monthly_traffic_gb, 'single', 1, rules)
            unlim = PricingService.calculate_monthly_price(plan.monthly_traffic_gb, 'unlimited', 0, rules)
            text.append(
                f'🌐 {plan.monthly_traffic_gb} ГБ: 1 устр. {single} ₽ | безлимит {unlim} ₽'
            )
    await safe_edit_message_text(callback.message, '\n'.join(text), reply_markup=admin_back_keyboard())
    await callback.answer()


@router.callback_query(AdminCallback.filter(F.section == 'broadcast'))
async def admin_broadcast(
    callback: CallbackQuery,
    callback_data: AdminCallback,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
) -> None:
    repo = BroadcastJobRepository(session)
    audit_repo = AuditLogRepository(session)

    if callback_data.action == 'compose':
        await state.clear()
        await state.update_data(broadcast_mode='create')
        await state.set_state(AdminState.waiting_broadcast_text)
        await callback.message.answer('Введите текст рассылки.')
        await callback.answer()
        return

    if callback_data.action in {'refresh', 'back'}:
        await state.clear()
        await _show_broadcasts(callback, session, edit=True)
        await callback.answer()
        return

    if callback_data.action == 'open' and callback_data.item_id:
        job = await repo.get_by_id(callback_data.item_id)
        if not job:
            await callback.answer('Рассылка не найдена', show_alert=True)
            return
        await safe_edit_message_text(callback.message, _broadcast_card(job), reply_markup=admin_broadcast_job_keyboard(job))
        await callback.answer()
        return

    if callback_data.action == 'delete' and callback_data.item_id:
        job = await repo.get_by_id_for_update(callback_data.item_id)
        if not job:
            await callback.answer('Рассылка не найдена', show_alert=True)
            return
        if job.status == BroadcastJobStatus.running:
            await callback.answer('Нельзя удалить выполняемую рассылку', show_alert=True)
            return
        if job.status not in {
            BroadcastJobStatus.draft,
            BroadcastJobStatus.scheduled,
            BroadcastJobStatus.completed,
            BroadcastJobStatus.failed,
            BroadcastJobStatus.cancelled,
        }:
            await callback.answer('Рассылку нельзя удалить в текущем статусе', show_alert=True)
            return

        job_id = job.id
        await repo.delete(job)
        await session.flush()

        await audit_repo.create(
            action=AuditAction.admin_action,
            actor_type=AuditActorType.admin,
            actor_tg_id=callback.from_user.id,
            entity_type='broadcast_job',
            entity_id=str(job_id),
            details={'action': 'delete'},
        )

        await callback.answer('Рассылка удалена')
        await _show_broadcasts(callback, session, edit=True)
        return

    if callback_data.action == 'edit_text' and callback_data.item_id:
        job = await repo.get_by_id(callback_data.item_id)
        if not job or not getattr(job, 'is_editable', job.status in {BroadcastJobStatus.draft, BroadcastJobStatus.scheduled}):
            await callback.answer('Можно редактировать только черновики и запланированные рассылки', show_alert=True)
            return
        await state.clear()
        await state.update_data(broadcast_mode='edit_text', broadcast_job_id=job.id)
        await state.set_state(AdminState.waiting_broadcast_edit_text)
        await callback.message.answer('Введите новый текст рассылки.')
        await callback.answer()
        return

    if callback_data.action == 'edit_time' and callback_data.item_id:
        job = await repo.get_by_id(callback_data.item_id)
        if not job or not getattr(job, 'is_editable', job.status in {BroadcastJobStatus.draft, BroadcastJobStatus.scheduled}):
            await callback.answer('Можно редактировать только черновики и запланированные рассылки', show_alert=True)
            return
        await state.clear()
        await state.update_data(broadcast_mode='edit_time', broadcast_job_id=job.id)
        await callback.message.answer('Выберите новое время отправки.', reply_markup=admin_broadcast_schedule_keyboard())
        await callback.answer()
        return

    if callback_data.action == 'pick_time':
        data = await state.get_data()
        mode = data.get('broadcast_mode')
        if mode not in {'create', 'edit_time'}:
            await callback.answer('Сначала введите текст рассылки', show_alert=True)
            return

        now_utc = datetime.now(timezone.utc)
        if callback_data.field == 'now':
            run_at = now_utc
        elif callback_data.field == '10m':
            run_at = now_utc + timedelta(minutes=10)
        elif callback_data.field == '1h':
            run_at = now_utc + timedelta(hours=1)
        elif callback_data.field == 'custom':
            next_state = (
                AdminState.waiting_broadcast_custom_time
                if mode == 'create'
                else AdminState.waiting_broadcast_edit_custom_time
            )
            await state.set_state(next_state)
            await callback.message.answer('Введите время в формате ДД.ММ ЧЧ:ММ')
            await callback.answer()
            return
        else:
            await callback.answer('Неизвестный вариант времени', show_alert=True)
            return

        if mode == 'create':
            text_value = data.get('broadcast_text', '')
            await state.update_data(broadcast_run_at=run_at.isoformat())
            await safe_edit_message_text(
                callback.message,
                _broadcast_preview_text(text_value, run_at),
                reply_markup=admin_broadcast_confirm_keyboard(),
            )
            await callback.answer()
            return

        job = await repo.get_by_id_for_update(int(data['broadcast_job_id']))
        if not job or not getattr(job, 'is_editable', job.status in {BroadcastJobStatus.draft, BroadcastJobStatus.scheduled}):
            await callback.answer('Рассылка больше недоступна для редактирования', show_alert=True)
            return

        await repo.update_run_at(job, run_at)
        await audit_repo.create(
            action=AuditAction.admin_action,
            actor_type=AuditActorType.admin,
            actor_tg_id=callback.from_user.id,
            entity_type='broadcast_job',
            entity_id=str(job.id),
            details={
                'action': 'edit_time',
                'run_at': run_at.isoformat(),
            },
        )
        await session.flush()
        await safe_edit_message_text(callback.message, _broadcast_card(job), reply_markup=admin_broadcast_job_keyboard(job))
        await state.clear()
        await callback.answer('Время отправки обновлено')
        return

    if callback_data.action == 'confirm':
        data = await state.get_data()
        if data.get('broadcast_mode') != 'create':
            await callback.answer('Нет черновика для сохранения', show_alert=True)
            return

        text_value = (data.get('broadcast_text') or '').strip()
        run_at_raw = data.get('broadcast_run_at')
        if not text_value or not run_at_raw:
            await callback.answer('Черновик неполный', show_alert=True)
            return

        run_at = datetime.fromisoformat(run_at_raw)
        job = await repo.create(
            created_by_tg_id=callback.from_user.id,
            text=text_value,
            run_at=run_at,
            status=BroadcastJobStatus.scheduled,
        )

        await audit_repo.create(
            action=AuditAction.broadcast_created,
            actor_type=AuditActorType.admin,
            actor_tg_id=callback.from_user.id,
            entity_type='broadcast_job',
            entity_id=str(job.id),
            details={
                'run_at': run_at.isoformat(),
                'text_preview': text_value[:500],
            },
        )

        await session.flush()
        await state.clear()
        await safe_edit_message_text(callback.message, _broadcast_card(job), reply_markup=admin_broadcast_job_keyboard(job))
        await callback.answer('Рассылка запланирована')
        return

    await state.clear()
    await _show_broadcasts(callback, session, edit=True)
    await callback.answer()


@router.message(AdminState.waiting_broadcast_text)
async def admin_broadcast_text(message: Message, session: AsyncSession, settings: Settings, state: FSMContext) -> None:
    text_value = (message.text or '').strip()
    if not text_value:
        await message.answer('Текст пустой. Введите текст рассылки.')
        return
    await state.update_data(broadcast_mode='create', broadcast_text=text_value)
    await message.answer('Выберите время отправки.', reply_markup=admin_broadcast_schedule_keyboard())


@router.message(AdminState.waiting_broadcast_custom_time)
async def admin_broadcast_custom_time(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    text_value = (data.get('broadcast_text') or '').strip()
    if not text_value:
        await message.answer('Сначала введите текст рассылки.')
        return

    try:
        run_at = _parse_admin_run_at(message.text or '')
    except ValueError as exc:
        await message.answer(str(exc))
        return

    await state.update_data(broadcast_run_at=run_at.isoformat())
    await message.answer(_broadcast_preview_text(text_value, run_at), reply_markup=admin_broadcast_confirm_keyboard())


@router.message(AdminState.waiting_broadcast_edit_text)
async def admin_broadcast_edit_text(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
) -> None:
    text_value = (message.text or '').strip()
    if not text_value:
        await message.answer('Текст пустой. Введите новый текст рассылки.')
        return

    data = await state.get_data()
    job_id = int(data.get('broadcast_job_id', 0))
    repo = BroadcastJobRepository(session)
    job = await repo.get_by_id_for_update(job_id)
    if not job or not getattr(job, 'is_editable', job.status in {BroadcastJobStatus.draft, BroadcastJobStatus.scheduled}):
        await state.clear()
        await message.answer('Рассылка больше недоступна для редактирования.', reply_markup=admin_main_keyboard())
        return

    await repo.update_text(job, text_value)
    await AuditLogRepository(session).create(
        action=AuditAction.admin_action,
        actor_type=AuditActorType.admin,
        actor_tg_id=message.from_user.id,
        entity_type='broadcast_job',
        entity_id=str(job.id),
        details={
            'action': 'edit_text',
            'text_preview': text_value[:500],
        },
    )

    await session.flush()
    await state.clear()
    await message.answer(_broadcast_card(job), reply_markup=admin_broadcast_job_keyboard(job))


@router.message(AdminState.waiting_broadcast_edit_custom_time)
async def admin_broadcast_edit_custom_time(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
) -> None:
    try:
        run_at = _parse_admin_run_at(message.text or '')
    except ValueError as exc:
        await message.answer(str(exc))
        return

    data = await state.get_data()
    job_id = int(data.get('broadcast_job_id', 0))
    repo = BroadcastJobRepository(session)
    job = await repo.get_by_id_for_update(job_id)
    if not job or not getattr(job, 'is_editable', job.status in {BroadcastJobStatus.draft, BroadcastJobStatus.scheduled}):
        await state.clear()
        await message.answer('Рассылка больше недоступна для редактирования.', reply_markup=admin_main_keyboard())
        return

    await repo.update_run_at(job, run_at)
    await AuditLogRepository(session).create(
        action=AuditAction.admin_action,
        actor_type=AuditActorType.admin,
        actor_tg_id=message.from_user.id,
        entity_type='broadcast_job',
        entity_id=str(job.id),
        details={
            'action': 'edit_time',
            'run_at': run_at.isoformat(),
        },
    )

    await session.flush()
    await state.clear()
    await message.answer(_broadcast_card(job), reply_markup=admin_broadcast_job_keyboard(job))
