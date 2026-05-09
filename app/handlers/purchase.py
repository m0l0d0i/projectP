from __future__ import annotations

from datetime import datetime
import logging
from decimal import Decimal, InvalidOperation, ROUND_UP

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.text_decorations import html_decoration as fmt
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Invoice, InvoicePurpose
from app.db.repositories import AppSettingsRepository, SubscriptionRepository
from app.handlers.common import get_or_create_user
from app.keyboards.inline import (
    BalanceAmountCallback,
    DeviceCountCallback,
    DeviceModeCallback,
    InvoiceActionCallback,
    MonthsCallback,
    TopUpCallback,
    TrafficPackageCallback,
    balance_topup_keyboard,
    device_count_keyboard,
    device_mode_keyboard,
    invoice_cart_keyboard,
    months_keyboard,
    traffic_selector_keyboard,
)
from app.keyboards.reply import main_menu
from app.services.marzban import MarzbanAPIError, MarzbanClient
from app.services.payment_engine import DuplicateInvoiceError, PaymentService
from app.services.payments.base import PaymentProvider, PaymentProviderError
from app.services.subscriptions import SubscriptionService
from app.services.tariffs import PricingService, money
from app.states.purchase import PurchaseState
from app.utils.formatters import format_dt
from app.utils.telegram import safe_callback_answer, safe_edit_message_text, safe_edit_reply_markup
from app.utils.runtime_settings import coerce_int_set, effective_list_from_row

router = Router(name='purchase')
logger = logging.getLogger(__name__)

MAIN_MENU_BUTTONS = {'🎁 Тест на 24 часа', '👑 Мой VPN', '👤 Мой профиль', '💳 Пополнить', '📞 Поддержка', '📜 Правила сервиса'}


def _user_friendly_payment_error_message(exc: PaymentProviderError) -> str:
    return 'Платежный сервис временно недоступен. Попробуйте позже.'


async def _handle_payment_provider_error(callback: CallbackQuery, *, action: str, exc: PaymentProviderError) -> None:
    logger.exception('Payment provider error during %s', action, exc_info=(type(exc), exc, exc.__traceback__))
    await safe_callback_answer(callback, _user_friendly_payment_error_message(exc), show_alert=True)


_DUPLICATE_INVOICE_USER_MESSAGE = (
    'Вы уже создавали такой счёт несколько секунд назад. '
    'Откройте предыдущее сообщение со ссылкой на оплату.'
)


async def _handle_duplicate_invoice(
    callback: CallbackQuery,
    *,
    action: str,
    user_tg_id: int | None,
    exc: DuplicateInvoiceError,
) -> None:
    logger.warning(
        'Duplicate invoice intent rejected during %s: tg_id=%s key=%s',
        action,
        user_tg_id,
        exc.idempotency_key,
    )
    await safe_callback_answer(callback, _DUPLICATE_INVOICE_USER_MESSAGE, show_alert=True)


def _format_amount_label(value: Decimal | int) -> str:
    amount = Decimal(str(value))
    if amount == amount.to_integral_value():
        return str(int(amount))
    return format(amount.normalize(), 'f').rstrip('0').rstrip('.')


def _device_mode_label(device_mode: str, device_count: int) -> str:
    if device_mode == 'single':
        return '1 устройство'
    if device_mode == 'unlimited':
        return 'Безлимит устройств'
    return f'{device_count} устройств'


def _selected_traffic_gb_from_code(plan_code: str) -> int | None:
    return PricingService.parse_package_code(plan_code)


def _render_traffic_selector_text(device_mode: str, device_count: int) -> str:
    return (
        '🌐 <b>Трафик на месяц</b>\n\n'
        f'📱 <b>Режим устройств:</b> {fmt.quote(_device_mode_label(device_mode, device_count))}\n'
        'Выберите объем трафика кнопками ниже.\n'
        'Доступно от <b>250 ГБ</b> до <b>500 ГБ</b> с шагом <b>50 ГБ</b>, '
        'либо <b>♾️ Безлимит</b>.'
    )


def _render_device_mode_text(*, early: bool = False) -> str:
    title = 'Продление подписки' if early else 'Оформление новой подписки'
    return (
        f'🌐 <b>{title}</b>\n\n'
        'Выберите режим устройств:'
    )


def _parse_payload_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    raw = str(value).strip()
    if not raw:
        return None
    try:
        normalized = raw.replace('Z', '+00:00')
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


async def _return_to_subscription_details(
    callback: CallbackQuery,
    *,
    session: AsyncSession,
    settings: Settings,
    marzban: MarzbanClient,
    subscription_id: int,
) -> bool:
    if not subscription_id:
        return False

    user = await get_or_create_user(callback, session)
    subscription = await SubscriptionRepository(session).get_by_id(subscription_id)
    if subscription is None or subscription.user_id != user.id:
        return False

    from app.handlers.vpn import _details_screen

    await _details_screen(
        callback.message,
        session=session,
        settings=settings,
        marzban=marzban,
        subscription=subscription,
    )
    return True


async def _payment_service(
    session: AsyncSession,
    settings: Settings,
    marzban: MarzbanClient,
    payments: PaymentProvider,
) -> PaymentService:
    subscription_service = SubscriptionService(session, settings, marzban)
    return PaymentService(session, settings, payments, subscription_service)


async def _load_app_settings(session: AsyncSession):
    repo = AppSettingsRepository(session)
    return await repo.get()


async def _is_admin_user(session: AsyncSession, tg_id: int, settings: Settings) -> bool:
    try:
        app_settings = await _load_app_settings(session)
        admin_ids = coerce_int_set(effective_list_from_row(app_settings, 'admin_ids', settings.admin_ids))
        return tg_id in admin_ids
    except Exception:
        return tg_id in coerce_int_set(settings.admin_ids)


async def _reply_main_menu(target, session: AsyncSession, settings: Settings, text: str) -> None:
    user = await get_or_create_user(target, session)
    is_admin = await _is_admin_user(session, user.tg_id, settings)
    await target.answer(
        text,
        reply_markup=main_menu(
            show_trial=not bool(user.trial_issued_at),
            show_admin=is_admin,
        ),
    )


async def _get_max_months(session: AsyncSession) -> int:
    rules = await PricingService.get_rules(session)
    return max(PricingService.MIN_MONTHS, int(rules.max_months))


async def _get_min_topup_amount(session: AsyncSession) -> int:
    rules = await PricingService.get_rules(session)
    raw_value = getattr(rules, 'min_topup_amount', Decimal('50.00'))
    min_amount = Decimal(str(raw_value)).quantize(Decimal('1'), rounding=ROUND_UP)
    return max(1, int(min_amount))


async def _render_months_text(
    session: AsyncSession,
    plan_code: str,
    device_mode: str,
    device_count: int,
    months: int,
    early: bool,
) -> str:
    if plan_code == 'unlim':
        device_mode = 'unlimited'
        device_count = 0

    basket = await PricingService.calculate_tariff_basket(
        session=session,
        plan_code=plan_code,
        months=months,
        user_balance=Decimal('0.00'),
        use_balance=False,
        device_mode=device_mode,
        device_count=device_count,
        selected_traffic_gb=_selected_traffic_gb_from_code(plan_code),
    )
    discount = basket.discount_percent * 100
    title = '⏳ <b>Продление подписки</b>' if early else '📆 <b>Срок подписки</b>'
    return (
        f'{title}\n\n'
        f'🌐 <b>Тариф:</b> {fmt.quote(basket.plan.title)}\n'
        f'📱 <b>Устройства:</b> {fmt.quote(basket.device_label)}\n'
        f'💸 <b>Цена за месяц:</b> {basket.monthly_price_before_discount} ₽\n'
        f'🎁 <b>Скидка:</b> {discount:.1f}%\n'
        f'🧾 <b>Итого за {months} мес.:</b> {basket.total} ₽'
    )


def render_balance_topup_prompt(amount: int, min_topup_amount: int) -> str:
    min_amount_label = _format_amount_label(min_topup_amount)
    return (
        '💳 <b>Пополнение баланса</b>\n\n'
        'Введите сумму сообщением или измените её кнопками ниже.\n'
        f'💰 <b>Сумма:</b> {amount} ₽\n\n'
        f'Минимальная сумма пополнения — {min_amount_label} ₽.'
    )


def _render_topup_cycle_scope(payload: dict) -> str:
    extra_traffic_gb = payload.get('extra_traffic_gb')
    service_id = payload.get('subscription_service_id')
    cycle_end_at = _parse_payload_datetime(payload.get('traffic_cycle_end_at'))

    lines: list[str] = []
    if service_id:
        lines.append(f'🔢 <b>Услуга:</b> {fmt.quote(str(service_id))}')
    if extra_traffic_gb is not None:
        lines.append(f'➕ <b>Будет начислено:</b> {fmt.quote(str(extra_traffic_gb))} ГБ')
    if cycle_end_at is not None:
        lines.append(f'📅 <b>Действует до конца текущего цикла:</b> {format_dt(cycle_end_at)}')
    else:
        lines.append('📅 <b>Действует:</b> только до конца текущего расчетного периода')

    lines.append('♻️ <b>Важно:</b> этот трафик не переносится на следующий цикл и будет обнулён при monthly reset.')
    return '\n'.join(lines)


def render_invoice_text(invoice: Invoice, user_balance: Decimal) -> str:
    payload = invoice.payload_json or {}
    balance_used = money(invoice.balance_used)
    payable = money(invoice.payable_amount)
    total = money(invoice.amount)

    if invoice.purpose == InvoicePurpose.tariff:
        traffic = payload.get('monthly_traffic_gb')
        traffic_label = '♾️ Безлимит трафика каждый месяц' if traffic is None else f'🌐 {traffic} ГБ каждый месяц'
        discount_percent = Decimal(str(payload.get('discount_percent', '0'))) * 100
        title = '🧺 <b>Корзина продления</b>' if payload.get('early_renewal') else '🧺 <b>Корзина подписки</b>'
        return (
            f'{title}\n\n'
            f'🌐 <b>Тариф:</b> {traffic_label}\n'
            f'📱 <b>Устройства:</b> {fmt.quote(str(payload.get("device_label", "-")))}\n'
            f'📆 <b>Срок:</b> {payload.get("months", 1)} мес.\n'
            f'🎁 <b>Скидка:</b> {discount_percent:.1f}%\n\n'
            f'💳 <b>Стоимость:</b> {total} ₽\n'
            f'💰 <b>Баланс:</b> {money(user_balance)} ₽\n'
            f'➖ <b>Списать с баланса:</b> {balance_used} ₽\n'
            f'🧾 <b>К оплате:</b> {payable} ₽'
        )

    if invoice.purpose == InvoicePurpose.topup:
        cycle_scope = _render_topup_cycle_scope(payload)
        # FEA-A8: если есть бонусный баланс (например, от промокода) и он
        # ещё не списан с инвойса — подсказать пользователю про кнопку.
        balance_hint = ''
        if money(user_balance) > Decimal('0.00') and balance_used <= Decimal('0.00'):
            balance_hint = (
                f'\n💡 На балансе есть {money(user_balance)} ₽ — '
                'можно списать их кнопкой «Использовать баланс».'
            )
        return (
            '📦 <b>Докупка трафика</b>\n\n'
            f'🌐 <b>Пакет:</b> {fmt.quote(str(payload.get("topup_title", "")))}\n'
            f'{cycle_scope}\n\n'
            f'💳 <b>Стоимость:</b> {total} ₽\n'
            f'💰 <b>Баланс:</b> {money(user_balance)} ₽\n'
            f'➖ <b>Списать с баланса:</b> {balance_used} ₽\n'
            f'🧾 <b>К оплате:</b> {payable} ₽'
            f'{balance_hint}'
        )

    return (
        '💳 <b>Пополнение баланса</b>\n\n'
        f'💰 <b>Сумма пополнения:</b> {total} ₽\n'
        f'🧾 <b>К оплате:</b> {payable} ₽'
    )


@router.message(F.text == '💳 Пополнить')
async def balance_topup_menu(message: Message, state: FSMContext, session: AsyncSession) -> None:
    min_topup_amount = await _get_min_topup_amount(session)
    initial_amount = max(300, min_topup_amount)

    await state.set_state(PurchaseState.waiting_balance_topup_amount)
    await state.update_data(balance_topup_amount=initial_amount)
    await message.answer(
        render_balance_topup_prompt(initial_amount, min_topup_amount),
        reply_markup=balance_topup_keyboard(initial_amount, min_amount=min_topup_amount),
    )


@router.message(PurchaseState.waiting_balance_topup_amount, F.text.in_(MAIN_MENU_BUTTONS))
async def balance_state_menu_navigation(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    marzban: MarzbanClient,
) -> None:
    await state.clear()
    if message.text == '💳 Пополнить':
        await balance_topup_menu(message, state, session)
        return
    if message.text == '👤 Мой профиль':
        from app.handlers.profile import profile_home
        await profile_home(message, session, state)
        return
    if message.text == '👑 Мой VPN':
        from app.handlers.vpn import my_vpn
        await my_vpn(message, session, settings, marzban)
        return
    if message.text == '📞 Поддержка':
        from app.handlers.support import support_home
        await support_home(message, session)
        return
    if message.text == '📜 Правила сервиса':
        from app.handlers.rules import rules_menu
        await rules_menu(message)
        return
    await _reply_main_menu(message, session, settings, 'Выберите действие ниже.')


@router.message(PurchaseState.waiting_balance_topup_amount, ~F.text.startswith('/'))
async def balance_topup_input(message: Message, state: FSMContext, session: AsyncSession) -> None:
    min_topup_amount = await _get_min_topup_amount(session)
    text = (message.text or '').strip()
    try:
        amount = int(Decimal(text))
    except (InvalidOperation, ValueError):
        await message.answer('Введите сумму цифрами, например: 300')
        return

    if amount < min_topup_amount:
        await message.answer(f'Минимальная сумма пополнения — {_format_amount_label(min_topup_amount)} ₽')
        return

    await state.update_data(balance_topup_amount=amount)
    await message.answer(
        render_balance_topup_prompt(amount, min_topup_amount),
        reply_markup=balance_topup_keyboard(amount, min_amount=min_topup_amount),
    )


@router.callback_query(BalanceAmountCallback.filter())
async def balance_amount_callbacks(
    callback: CallbackQuery,
    callback_data: BalanceAmountCallback,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    marzban: MarzbanClient,
    payments: PaymentProvider,
) -> None:
    min_topup_amount = await _get_min_topup_amount(session)
    current_data = await state.get_data()
    current_amount = int(current_data.get('balance_topup_amount') or max(300, min_topup_amount))
    amount = max(min_topup_amount, callback_data.amount)

    if callback_data.action == 'noop':
        await safe_callback_answer(callback)
        return

    if callback_data.action == 'cancel':
        await state.clear()
        if callback.message:
            await safe_edit_reply_markup(callback.message, reply_markup=None)
            await _reply_main_menu(callback, session, settings, '👌 Пополнение отменено.')
        await safe_callback_answer(callback)
        return

    if callback_data.action == 'set':
        if amount == current_amount:
            await safe_callback_answer(
                callback,
                f'Минимальная сумма пополнения — {_format_amount_label(min_topup_amount)} ₽'
                if amount <= min_topup_amount
                else 'Сумма уже выбрана',
            )
            return

        await state.update_data(balance_topup_amount=amount)
        changed = await safe_edit_message_text(
            callback.message,
            render_balance_topup_prompt(amount, min_topup_amount),
            reply_markup=balance_topup_keyboard(amount, min_amount=min_topup_amount),
        )
        await safe_callback_answer(
            callback,
            'Сумма обновлена' if changed else f'Минимальная сумма пополнения — {_format_amount_label(min_topup_amount)} ₽',
        )
        return

    if callback_data.action == 'confirm':
        if amount < min_topup_amount:
            await safe_callback_answer(
                callback,
                f'Минимальная сумма пополнения — {_format_amount_label(min_topup_amount)} ₽',
                show_alert=True,
            )
            return

        user = await get_or_create_user(callback, session)
        service = await _payment_service(session, settings, marzban, payments)
        try:
            invoice = await service.create_balance_topup_invoice(user=user, amount=Decimal(str(amount)))
            await session.commit()
        except DuplicateInvoiceError as exc:
            await _handle_duplicate_invoice(
                callback,
                action='create_balance_topup_invoice',
                user_tg_id=callback.from_user.id if callback.from_user else None,
                exc=exc,
            )
            return
        except PaymentProviderError as exc:
            await _handle_payment_provider_error(callback, action='create_balance_topup_invoice', exc=exc)
            return
        except ValueError as exc:
            await safe_callback_answer(callback, str(exc), show_alert=True)
            return

        await state.clear()
        await safe_edit_message_text(
            callback.message,
            render_invoice_text(invoice, user.balance),
            reply_markup=invoice_cart_keyboard(invoice, user.balance),
            disable_web_page_preview=True,
        )
        await safe_callback_answer(callback, 'Счет создан')


@router.callback_query(DeviceModeCallback.filter())
async def choose_device_mode(
    callback: CallbackQuery,
    callback_data: DeviceModeCallback,
    session: AsyncSession,
    settings: Settings,
    marzban: MarzbanClient,
) -> None:
    message_text = (
        getattr(callback.message, 'html_text', None)
        or getattr(callback.message, 'text', None)
        or ''
    )
    is_back_from_purchase_step = '🌐 <b>Трафик на месяц</b>' in message_text or '📆 <b>Срок подписки</b>' in message_text or '⏳ <b>Продление подписки</b>' in message_text

    if callback_data.mode == 'custom':
        count = 2
        changed = await safe_edit_message_text(
            callback.message,
            '🔢 <b>Количество устройств</b>\n\nНастройте количество устройств кнопками ниже.',
            reply_markup=device_count_keyboard(
                callback_data.package_code,
                count=count,
                early=bool(callback_data.early),
                subscription_id=callback_data.subscription_id,
            ),
        )
        await safe_callback_answer(callback, 'Открыл выбор устройств' if changed else 'Количество устройств уже выбрано')
        return

    if is_back_from_purchase_step:
        if bool(callback_data.early) and callback_data.subscription_id:
            restored = await _return_to_subscription_details(
                callback,
                session=session,
                settings=settings,
                marzban=marzban,
                subscription_id=callback_data.subscription_id,
            )
            if restored:
                await safe_callback_answer(callback, 'Возвращаю к услуге')
                return

        changed = await safe_edit_message_text(
            callback.message,
            _render_device_mode_text(early=bool(callback_data.early)),
            reply_markup=device_mode_keyboard(
                early=bool(callback_data.early),
                subscription_id=callback_data.subscription_id,
                package_code=callback_data.package_code,
            ),
        )
        await safe_callback_answer(callback, 'Возвращаю к выбору устройств' if changed else 'Это меню уже открыто')
        return

    default_code = 'unlim' if callback_data.mode == 'unlimited' else 't250'
    changed = await safe_edit_message_text(
        callback.message,
        _render_traffic_selector_text(
            callback_data.mode,
            1 if callback_data.mode == 'single' else 0,
        ),
        reply_markup=traffic_selector_keyboard(
            default_code,
            callback_data.mode,
            device_count=1 if callback_data.mode == 'single' else 0,
            early=bool(callback_data.early),
            subscription_id=callback_data.subscription_id,
        ),
    )
    await safe_callback_answer(callback, 'Открыл выбор трафика' if changed else 'Это меню уже открыто')


@router.callback_query(DeviceCountCallback.filter())
async def choose_device_count(callback: CallbackQuery, callback_data: DeviceCountCallback) -> None:
    count = max(2, min(PricingService.MAX_CUSTOM_DEVICES, callback_data.count))

    if callback_data.action == 'noop':
        await safe_callback_answer(callback)
        return

    if callback_data.action == 'set':
        changed = await safe_edit_message_text(
            callback.message,
            '🔢 <b>Количество устройств</b>\n\nНастройте количество устройств кнопками ниже.',
            reply_markup=device_count_keyboard(
                callback_data.package_code,
                count=count,
                early=bool(callback_data.early),
                subscription_id=callback_data.subscription_id,
            ),
        )
        await safe_callback_answer(
            callback,
            'Количество обновлено' if changed else f'Минимум 2 и максимум {PricingService.MAX_CUSTOM_DEVICES} устройств',
        )
        return

    changed = await safe_edit_message_text(
        callback.message,
        _render_traffic_selector_text('custom', count),
        reply_markup=traffic_selector_keyboard(
            't250',
            'custom',
            device_count=count,
            early=bool(callback_data.early),
            subscription_id=callback_data.subscription_id,
        ),
    )
    await safe_callback_answer(callback, 'Продолжаем' if changed else 'Это меню уже открыто')


@router.callback_query(TrafficPackageCallback.filter())
async def choose_traffic_package(
    callback: CallbackQuery,
    callback_data: TrafficPackageCallback,
    session: AsyncSession,
    settings: Settings,
    marzban: MarzbanClient,
) -> None:
    if callback_data.action == 'noop':
        await safe_callback_answer(callback)
        return

    device_mode = callback_data.device_mode
    device_count = callback_data.device_count

    if callback_data.action == 'back_to_devices':
        if bool(callback_data.early) and callback_data.subscription_id:
            restored = await _return_to_subscription_details(
                callback,
                session=session,
                settings=settings,
                marzban=marzban,
                subscription_id=callback_data.subscription_id,
            )
            if restored:
                await safe_callback_answer(callback, 'Возвращаю к услуге')
                return

        if device_mode == 'custom':
            count = max(2, min(PricingService.MAX_CUSTOM_DEVICES, device_count or 2))
            changed = await safe_edit_message_text(
                callback.message,
                '🔢 <b>Количество устройств</b>\n\nНастройте количество устройств кнопками ниже.',
                reply_markup=device_count_keyboard(
                    callback_data.package_code,
                    count=count,
                    early=bool(callback_data.early),
                    subscription_id=callback_data.subscription_id,
                ),
            )
            await safe_callback_answer(callback, 'Возвращаю к выбору устройств' if changed else 'Это меню уже открыто')
            return

        changed = await safe_edit_message_text(
            callback.message,
            _render_device_mode_text(early=bool(callback_data.early)),
            reply_markup=device_mode_keyboard(
                early=bool(callback_data.early),
                subscription_id=callback_data.subscription_id,
                package_code=callback_data.package_code,
            ),
        )
        await safe_callback_answer(callback, 'Возвращаю к выбору устройств' if changed else 'Это меню уже открыто')
        return

    if callback_data.action == 'set':
        selected_mode = 'unlimited' if callback_data.package_code == 'unlim' else device_mode
        selected_count = 0 if selected_mode == 'unlimited' else device_count

        changed = await safe_edit_message_text(
            callback.message,
            _render_traffic_selector_text(selected_mode, selected_count),
            reply_markup=traffic_selector_keyboard(
                callback_data.package_code,
                selected_mode,
                device_count=selected_count,
                early=bool(callback_data.early),
                subscription_id=callback_data.subscription_id,
            ),
        )
        if changed:
            await safe_callback_answer(callback, 'Трафик обновлен')
        else:
            traffic = _selected_traffic_gb_from_code(callback_data.package_code)
            if callback_data.package_code == 'unlim' or traffic is None:
                await safe_callback_answer(callback, 'Это значение уже выбрано')
            elif traffic == PricingService.TRAFFIC_OPTIONS[0]:
                await safe_callback_answer(callback, 'Минимальный трафик уже выбран')
            elif traffic == PricingService.TRAFFIC_OPTIONS[-1]:
                await safe_callback_answer(callback, 'Максимальный трафик уже выбран')
            else:
                await safe_callback_answer(callback, 'Это значение уже выбрано')
        return

    selected_mode = 'unlimited' if callback_data.package_code == 'unlim' else device_mode
    selected_count = 0 if selected_mode == 'unlimited' else device_count

    rules = await PricingService.get_rules(session)
    changed = await safe_edit_message_text(
        callback.message,
        await _render_months_text(
            session,
            callback_data.package_code,
            selected_mode,
            selected_count,
            1,
            bool(callback_data.early),
        ),
        reply_markup=months_keyboard(
            callback_data.package_code,
            selected_mode,
            device_count=selected_count,
            months=1,
            early=bool(callback_data.early),
            subscription_id=callback_data.subscription_id,
            max_months=await _get_max_months(session),
            max_discount_percent=rules.max_discount_percent,
        ),
    )
    await safe_callback_answer(callback, 'Продолжаем' if changed else 'Это меню уже открыто')


@router.callback_query(MonthsCallback.filter())
async def choose_months(
    callback: CallbackQuery,
    callback_data: MonthsCallback,
    session: AsyncSession,
    settings: Settings,
    marzban: MarzbanClient,
    payments: PaymentProvider,
) -> None:
    max_months = await _get_max_months(session)
    months = min(max_months, max(PricingService.MIN_MONTHS, callback_data.months))

    if callback_data.action == 'noop':
        await safe_callback_answer(callback)
        return

    if callback_data.action == 'set':
        rules = await PricingService.get_rules(session)
        changed = await safe_edit_message_text(
            callback.message,
            await _render_months_text(
                session,
                callback_data.package_code,
                callback_data.device_mode,
                callback_data.device_count,
                months,
                bool(callback_data.early),
            ),
            reply_markup=months_keyboard(
                callback_data.package_code,
                callback_data.device_mode,
                device_count=callback_data.device_count,
                months=months,
                early=bool(callback_data.early),
                subscription_id=callback_data.subscription_id,
                max_months=max_months,
                max_discount_percent=rules.max_discount_percent,
            ),
        )
        await safe_callback_answer(callback, 'Срок обновлен' if changed else 'Это значение уже выбрано')
        return

    user = await get_or_create_user(callback, session)
    service = await _payment_service(session, settings, marzban, payments)
    try:
        invoice = await service.create_tariff_invoice(
            user=user,
            package_code=callback_data.package_code,
            months=months,
            device_mode=callback_data.device_mode,
            device_count=callback_data.device_count,
            early_renewal=bool(callback_data.early),
            subscription_id=callback_data.subscription_id or None,
            selected_traffic_gb=_selected_traffic_gb_from_code(callback_data.package_code),
        )
        await session.commit()
    except DuplicateInvoiceError as exc:
        await _handle_duplicate_invoice(
            callback,
            action='create_tariff_invoice',
            user_tg_id=callback.from_user.id if callback.from_user else None,
            exc=exc,
        )
        return
    except PaymentProviderError as exc:
        await _handle_payment_provider_error(callback, action='create_tariff_invoice', exc=exc)
        return
    except ValueError as exc:
        await safe_callback_answer(callback, str(exc), show_alert=True)
        return

    await safe_edit_message_text(
        callback.message,
        render_invoice_text(invoice, user.balance),
        reply_markup=invoice_cart_keyboard(invoice, user.balance),
        disable_web_page_preview=True,
    )
    await safe_callback_answer(callback, 'Счет подготовлен')


@router.callback_query(TopUpCallback.filter())
async def buy_topup(
    callback: CallbackQuery,
    callback_data: TopUpCallback,
    session: AsyncSession,
    settings: Settings,
    marzban: MarzbanClient,
    payments: PaymentProvider,
) -> None:
    user = await get_or_create_user(callback, session)
    service = await _payment_service(session, settings, marzban, payments)
    try:
        invoice = await service.create_topup_invoice(
            user=user,
            topup_code=callback_data.code,
            subscription_id=callback_data.subscription_id or None,
        )
        await session.commit()
    except DuplicateInvoiceError as exc:
        await _handle_duplicate_invoice(
            callback,
            action='create_topup_invoice',
            user_tg_id=callback.from_user.id if callback.from_user else None,
            exc=exc,
        )
        return
    except PaymentProviderError as exc:
        await _handle_payment_provider_error(callback, action='create_topup_invoice', exc=exc)
        return
    except ValueError as exc:
        await safe_callback_answer(callback, str(exc), show_alert=True)
        return

    text = render_invoice_text(invoice, user.balance)
    await callback.message.answer(
        text,
        reply_markup=invoice_cart_keyboard(invoice, user.balance),
        disable_web_page_preview=True,
    )
    await safe_callback_answer(callback, 'Счет подготовлен')


@router.callback_query(InvoiceActionCallback.filter(F.action == 'toggle_balance'))
async def toggle_balance(
    callback: CallbackQuery,
    callback_data: InvoiceActionCallback,
    session: AsyncSession,
    settings: Settings,
    marzban: MarzbanClient,
    payments: PaymentProvider,
) -> None:
    service = await _payment_service(session, settings, marzban, payments)
    try:
        invoice = await service.toggle_balance(callback_data.invoice_id, callback.from_user.id)
        await session.commit()
    except PaymentProviderError as exc:
        await _handle_payment_provider_error(callback, action='toggle_balance', exc=exc)
        return
    except ValueError as exc:
        await safe_callback_answer(callback, str(exc), show_alert=True)
        return

    user = await get_or_create_user(callback, session)
    text = render_invoice_text(invoice, user.balance)
    await safe_edit_message_text(
        callback.message,
        text,
        reply_markup=invoice_cart_keyboard(invoice, user.balance),
        disable_web_page_preview=True,
    )
    await safe_callback_answer(callback, 'Корзина пересчитана')


@router.callback_query(InvoiceActionCallback.filter(F.action == 'confirm'))
async def confirm_invoice(
    callback: CallbackQuery,
    callback_data: InvoiceActionCallback,
    session: AsyncSession,
    settings: Settings,
    marzban: MarzbanClient,
    payments: PaymentProvider,
) -> None:
    service = await _payment_service(session, settings, marzban, payments)
    try:
        result = await service.process_invoice_for_user(callback_data.invoice_id, callback.from_user.id)
    except PaymentProviderError as exc:
        await _handle_payment_provider_error(callback, action='confirm_invoice', exc=exc)
        return
    except (ValueError, MarzbanAPIError) as exc:
        await safe_callback_answer(callback, str(exc), show_alert=True)
        return

    if result.already_processed:
        await safe_callback_answer(callback, 'Этот счет уже был обработан', show_alert=True)
        return

    if result.invoice.purpose == InvoicePurpose.tariff:
        await callback.message.answer('✅ Оплата подтверждена. Услуга активирована.')
    elif result.invoice.purpose == InvoicePurpose.topup:
        await callback.message.answer(
            '✅ Оплата подтверждена. Дополнительный трафик начислен только на текущий расчетный период.'
        )
    else:
        await callback.message.answer('✅ Баланс успешно пополнен.')

    await safe_edit_reply_markup(callback.message, reply_markup=None)
    await safe_callback_answer(callback, 'Успешно')


@router.callback_query(InvoiceActionCallback.filter(F.action == 'cancel'))
async def cancel_invoice(
    callback: CallbackQuery,
    callback_data: InvoiceActionCallback,
    session: AsyncSession,
    settings: Settings,
    marzban: MarzbanClient,
    payments: PaymentProvider,
) -> None:
    service = await _payment_service(session, settings, marzban, payments)
    try:
        await service.cancel_invoice(callback_data.invoice_id, callback.from_user.id)
        await session.commit()
    except ValueError as exc:
        await safe_callback_answer(callback, str(exc), show_alert=True)
        return

    await safe_edit_reply_markup(callback.message, reply_markup=None)
    await callback.message.answer('❌ Транзакция отменена.')
    await safe_callback_answer(callback, 'Счет отменен')
