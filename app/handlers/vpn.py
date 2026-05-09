from __future__ import annotations

import ipaddress
import logging
from decimal import Decimal
from urllib.parse import urlparse

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.text_decorations import html_decoration as fmt
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Subscription
from app.db.repositories import AppLinkRepository, AppSettingsRepository, SubscriptionRepository, UserRepository
from app.handlers.common import get_or_create_user
from app.keyboards.inline import (
    DeviceInfoCallback,
    NotificationCallback,
    TrialCallback,
    VpnCallback,
    active_vpn_keyboard,
    device_keyboard,
    device_mode_keyboard,
    device_os_keyboard,
    key_photo_back_keyboard,
    months_keyboard,
    topup_keyboard,
    trial_confirm_keyboard,
    vpn_details_keyboard,
    vpn_services_keyboard,
)
from app.services.cache import CacheService
from app.services.notification_dispatcher import NotificationDispatcher
from app.keyboards.reply import main_menu
from app.services.marzban import MarzbanAPIError, MarzbanClient
from app.services.subscriptions import ResetTrafficQuote, SubscriptionService
from app.services.subscription_urls import canonicalize_subscription_url_from_settings
from app.services.tariffs import PricingService
from app.utils.formatters import bytes_to_gb, format_dt
from app.utils.qr import build_qr_png
from app.utils.runtime_settings import coerce_int_set, effective_bool_from_row, effective_int_from_row, effective_list_from_row
from app.utils.telegram import safe_callback_answer, safe_edit_message_text

router = Router(name='vpn')
logger = logging.getLogger(__name__)

SCREEN2_TEXT = '📱 💻 Выберите свое устройство из предложенного списка:'
SCREEN3_WITH_LINKS_TEXT = '🌐 Подключение к VPN VLESS:'
SCREEN3_EMPTY_TEXT = (
    'К сожалению, инструкции/ссылки на приложение для этого устройства ещё нет 😔 '
    'Если хотите помочь записать процесс установки/посоветовать приложение, пожалуйста, обратитесь в поддержку 📩🙏.'
)


def _service(session: AsyncSession, settings: Settings, marzban: MarzbanClient) -> SubscriptionService:
    return SubscriptionService(session, settings, marzban)


def _status_label(subscription: Subscription, status: str | None) -> str:
    if status in {'expired', 'disabled'}:
        return '🔴 Неактивный'
    if subscription.is_alive_local:
        return '🟢 Активный'
    return '🔴 Неактивный'


def _used_bytes_to_gb(value: int | None) -> str:
    if value is None:
        return '0 ГБ'
    gb = value / (1024 ** 3)
    return f'{gb:.0f} ГБ'


def _traffic_bytes_label(value: int | None) -> str:
    if value in (None, 0):
        return '0 ГБ'
    return bytes_to_gb(value)


def _format_day_word(days: int) -> str:
    remainder_10 = days % 10
    remainder_100 = days % 100
    if remainder_10 == 1 and remainder_100 != 11:
        return 'день'
    if remainder_10 in {2, 3, 4} and remainder_100 not in {12, 13, 14}:
        return 'дня'
    return 'дней'


def _format_duration_text(days: int) -> str:
    if days == 1:
        return '24 часа'
    return f'{days} {_format_day_word(days)}'


def _format_device_label(device_count: int) -> str:
    remainder_10 = device_count % 10
    remainder_100 = device_count % 100
    if remainder_10 == 1 and remainder_100 != 11:
        word = 'устройство'
    elif remainder_10 in {2, 3, 4} and remainder_100 not in {12, 13, 14}:
        word = 'устройства'
    else:
        word = 'устройств'
    return f'{device_count} {word}'


def _subscription_link_keyboard(subscription_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='🌐 Страница вашей подписки', url=subscription_url)],
        ]
    )


def _reset_traffic_quote_keyboard(subscription_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text='✅ Подтвердить сброс',
                    callback_data=VpnCallback(
                        action='reset_traffic_confirm',
                        subscription_id=subscription_id,
                    ).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text='⬅️ Назад к услуге',
                    callback_data=VpnCallback(
                        action='details',
                        subscription_id=subscription_id,
                    ).pack(),
                )
            ],
        ]
    )


def _render_purchase_entry_text(*, early: bool = False) -> str:
    title = 'Продление подписки' if early else 'Оформление новой подписки'
    return (
        f'🌐 <b>{title}</b>\n\n'
        'Выберите режим устройств:'
    )


def _trial_details_keyboard(subscription_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='🔑 Показать ключ / QR', callback_data=VpnCallback(action='show_key', subscription_id=subscription_id).pack())],
            [InlineKeyboardButton(text='📱 Выбрать устройство', callback_data=VpnCallback(action='device_menu', subscription_id=subscription_id).pack())],
            [InlineKeyboardButton(text='⬅️ Назад к услугам', callback_data=VpnCallback(action='services', subscription_id=subscription_id).pack())],
        ]
    )


def _is_unlimited_subscription(subscription: Subscription) -> bool:
    return getattr(subscription, 'monthly_traffic_bytes', None) in (None, 0)


def _unlimited_details_keyboard(subscription_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='🔑 Показать ключ / QR', callback_data=VpnCallback(action='show_key', subscription_id=subscription_id).pack())],
            [InlineKeyboardButton(text='📱 Выбрать устройство', callback_data=VpnCallback(action='device_menu', subscription_id=subscription_id).pack())],
            [InlineKeyboardButton(text='💳 Продлить подписку', callback_data=VpnCallback(action='renew', subscription_id=subscription_id).pack())],
            [InlineKeyboardButton(text='⬅️ Назад к услугам', callback_data=VpnCallback(action='services', subscription_id=subscription_id).pack())],
        ]
    )


def _is_public_http_url(candidate: str | None) -> bool:
    if not candidate:
        return False

    raw = str(candidate).strip()
    if not raw:
        return False

    parsed = urlparse(raw)
    if parsed.scheme not in {'http', 'https'}:
        return False
    if not parsed.netloc:
        return False

    hostname = (parsed.hostname or '').strip().lower()
    if not hostname:
        return False
    if hostname in {'localhost'}:
        return False
    if parsed.path.startswith('/admin/'):
        return False

    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        return True

    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _sanitize_public_url(candidate: str | None) -> str | None:
    if not candidate:
        return None
    normalized = str(candidate).strip()
    if not normalized:
        return None
    return normalized if _is_public_http_url(normalized) else None


def _get_cycle_extra_traffic_bytes(subscription: Subscription) -> int:
    return max(0, int(getattr(subscription, 'cycle_extra_traffic_bytes', 0) or 0))


def _get_cycle_base_bytes(subscription: Subscription) -> int | None:
    value = getattr(subscription, 'effective_cycle_base_bytes', None)
    if value in (None, 0):
        return getattr(subscription, 'monthly_traffic_bytes', None)
    return int(value)


def _get_cycle_total_bytes(subscription: Subscription, fallback_data_limit: int | None) -> int | None:
    value = getattr(subscription, 'effective_cycle_total_bytes', None)
    if value in (None, 0):
        monthly_traffic_bytes = getattr(subscription, 'monthly_traffic_bytes', None)
        if monthly_traffic_bytes not in (None, 0):
            return int(fallback_data_limit or monthly_traffic_bytes)
        return fallback_data_limit
    return int(value)


def _get_cycle_end_dt(subscription: Subscription) -> object:
    return getattr(subscription, 'traffic_cycle_end_at', None) or getattr(subscription, 'next_traffic_reset_at', None)


def _render_reset_quote_text(
    *,
    subscription: Subscription,
    quote: ResetTrafficQuote,
    balance: Decimal,
) -> str:
    days_left = max(1, int(getattr(quote, 'days_left_in_month', 1) or 1))
    days_total = max(days_left, int(getattr(quote, 'days_in_month', days_left) or days_left))

    return (
        '♻️ <b>Сброс трафика</b>\n\n'
        f'Услуга: <b>{fmt.quote(subscription.service_id)}</b>\n'
        f'Стоимость сброса: <b>{quote.reset_price} ₽</b>\n'
        f'Цена тарифа за цикл: <b>{quote.monthly_price} ₽</b>\n'
        f'До конца текущего цикла: <b>{days_left} {_format_day_word(days_left)}</b> из <b>{days_total}</b>\n'
        f'Ваш баланс: <b>{balance} ₽</b>\n\n'
        'После подтверждения трафик будет сброшен, а сумма сразу спишется с баланса.'
    )


async def _load_app_settings(session: AsyncSession):
    repo = AppSettingsRepository(session)
    return await repo.get()


async def _is_admin_user(session: AsyncSession, tg_id: int, settings: Settings) -> bool:
    try:
        app_settings = await _load_app_settings(session)
        admin_ids = coerce_int_set(effective_list_from_row(app_settings, 'admin_ids', settings.admin_ids))
        return tg_id in admin_ids
    except Exception:
        logger.exception('Failed to load admin_ids from AppSettings, falling back to env settings')
        return tg_id in coerce_int_set(settings.admin_ids)


async def _load_subscription_for_user(session: AsyncSession, user_id: int, subscription_id: int) -> Subscription | None:
    subscription = await SubscriptionRepository(session).get_by_id(subscription_id)
    if subscription is None or subscription.user_id != user_id:
        return None
    return subscription


async def _load_subscription_view(
    session: AsyncSession,
    settings: Settings,
    marzban: MarzbanClient,
    subscription: Subscription,
) -> dict[str, object]:
    try:
        synced = await _service(session, settings, marzban).sync_remote_state(subscription)
        remote = synced.remote
        expire_dt = remote.expire_datetime
        data_limit = remote.data_limit
        used_traffic = remote.used_traffic
        status = remote.status
        sub_url = canonicalize_subscription_url_from_settings(
            remote.subscription_url or subscription.subscription_url,
            settings,
        )
    except MarzbanAPIError:
        expire_dt = subscription.expire_date
        data_limit = subscription.data_limit_bytes
        used_traffic = subscription.used_traffic_bytes
        status = 'active' if subscription.is_alive_local else 'expired'
        sub_url = canonicalize_subscription_url_from_settings(subscription.subscription_url, settings)

    cycle_base_bytes = _get_cycle_base_bytes(subscription)
    cycle_extra_bytes = _get_cycle_extra_traffic_bytes(subscription)
    cycle_total_bytes = _get_cycle_total_bytes(subscription, data_limit)
    provided_bytes = cycle_total_bytes if cycle_total_bytes not in (None, 0) else data_limit
    provided_label = 'Безлимит' if provided_bytes in (None, 0) else bytes_to_gb(provided_bytes)

    return {
        'expire_dt': expire_dt,
        'data_limit': data_limit,
        'used_traffic': used_traffic,
        'status': status,
        'subscription_url': sub_url,
        'provided_label': provided_label,
        'used_label': _used_bytes_to_gb(used_traffic),
        'status_label': _status_label(subscription, status),
        'online_limit_label': subscription.online_limit if subscription.online_limit else 'Без ограничений',
        'cycle_base_bytes': cycle_base_bytes,
        'cycle_total_bytes': cycle_total_bytes,
        'cycle_extra_bytes': cycle_extra_bytes,
        'cycle_base_label': 'Безлимит' if cycle_base_bytes in (None, 0) else _traffic_bytes_label(cycle_base_bytes),
        'cycle_total_label': 'Безлимит' if cycle_total_bytes in (None, 0) else _traffic_bytes_label(cycle_total_bytes),
        'cycle_extra_label': _traffic_bytes_label(cycle_extra_bytes),
        'has_cycle_extra': cycle_extra_bytes > 0,
        'traffic_cycle_end_dt': _get_cycle_end_dt(subscription),
    }


async def _services_screen(
    target: Message,
    *,
    session: AsyncSession,
    user_id: int,
) -> None:
    subs = await SubscriptionRepository(session).list_by_user_id(user_id)
    active_services = [(s.id, s.is_alive_local, s.service_id) for s in subs if s.is_alive_local]
    if not active_services:
        await safe_edit_message_text(
            target,
            '🌐 <b>Подключение VPN</b>\n\nВыберите режим устройств:',
            reply_markup=device_mode_keyboard(),
        )
        return

    await safe_edit_message_text(
        target,
        '👑 <b>Мой VPN</b>\n\nВыберите услугу:',
        reply_markup=vpn_services_keyboard(active_services),
    )


async def _details_screen(
    target: Message,
    *,
    session: AsyncSession,
    settings: Settings,
    marzban: MarzbanClient,
    subscription: Subscription,
) -> None:
    view = await _load_subscription_view(session, settings, marzban, subscription)

    extra_block = ''
    keyboard = vpn_details_keyboard(subscription.id)
    if subscription.is_trial:
        extra_block = '\n\n🎁 <b>Тестовая подписка</b>\nПродление, сброс трафика и докупка трафика недоступны.'
        keyboard = _trial_details_keyboard(subscription.id)
    elif _is_unlimited_subscription(subscription):
        extra_block = '\n\n♾️ <b>Безлимитный тариф</b>\nДокупка трафика для этого тарифа не требуется и недоступна.'
        keyboard = _unlimited_details_keyboard(subscription.id)
    else:
        traffic_lines = [
            f'Базовый трафик цикла: {view["cycle_base_label"]}',
            f'Использованный трафик: {view["used_label"]}',
        ]
        if view['has_cycle_extra']:
            traffic_lines.insert(1, f'Доп. трафик текущего цикла: +{view["cycle_extra_label"]}')
            traffic_lines.insert(2, f'Доступно в текущем цикле: {view["cycle_total_label"]}')
        else:
            traffic_lines.insert(1, f'Предоставленный трафик: {view["provided_label"]}')
        if view['traffic_cycle_end_dt'] is not None:
            traffic_lines.append(f'Текущий цикл до: {format_dt(view["traffic_cycle_end_dt"])}')
        extra_block = '\n' + '\n'.join(traffic_lines)

    text = (
        '🌐 Информация о вашей услуге:\n\n'
        f'Статус: {view["status_label"]}\n'
        f'Дата окончания: {format_dt(view["expire_dt"])}\n'
        f'Номер услуги: {subscription.service_id}'
        f'{extra_block}'
    )
    await safe_edit_message_text(target, text, reply_markup=keyboard)


async def _device_menu_screen(target: Message, subscription_id: int) -> None:
    await safe_edit_message_text(target, SCREEN2_TEXT, reply_markup=device_keyboard(subscription_id))


async def _device_os_screen(
    target: Message,
    *,
    session: AsyncSession,
    subscription_id: int,
    os_name: str,
) -> None:
    link = await AppLinkRepository(session).get_by_os_name(os_name)

    download_url = _sanitize_public_url(getattr(link, 'download_url', None) if link is not None else None)
    guide_url = _sanitize_public_url(getattr(link, 'guide_url', None) if link is not None else None)

    if link is not None:
        raw_download_url = getattr(link, 'download_url', None)
        raw_guide_url = getattr(link, 'guide_url', None)
        if raw_download_url and not download_url:
            logger.warning('Skipping invalid app download URL for os=%s: %r', os_name, raw_download_url)
        if raw_guide_url and not guide_url:
            logger.warning('Skipping invalid app guide URL for os=%s: %r', os_name, raw_guide_url)

    if not download_url and not guide_url:
        await safe_edit_message_text(
            target,
            SCREEN3_EMPTY_TEXT,
            reply_markup=device_os_keyboard(subscription_id, os_name),
        )
        return

    await safe_edit_message_text(
        target,
        SCREEN3_WITH_LINKS_TEXT,
        reply_markup=device_os_keyboard(
            subscription_id,
            os_name,
            download_url=download_url,
            guide_url=guide_url,
        ),
    )


async def _get_max_months(session: AsyncSession) -> int:
    rules = await PricingService.get_rules(session)
    for attr in ('max_duration_months', 'max_months'):
        value = getattr(rules, attr, None)
        if isinstance(value, int) and value > 0:
            return value
    return 12


async def _render_months_text(
    session: AsyncSession,
    package_code: str,
    device_mode: str,
    device_count: int,
    months: int,
    early: bool,
    user_balance: Decimal,
) -> str:
    basket = await PricingService.calculate_tariff_basket(
        session=session,
        plan_code=package_code,
        months=months,
        user_balance=user_balance,
        use_balance=False,
        device_mode=device_mode,
        device_count=device_count,
    )
    action_label = 'Продление подписки' if early else 'Оформление подписки'

    return (
        f'🌐 <b>{action_label}</b>\n\n'
        f'Тариф: <b>{basket.plan.title}</b>\n'
        f'Устройства: <b>{basket.device_label}</b>\n'
        f'Срок: <b>{months} мес.</b>\n'
        f'Месячная цена: <b>{basket.effective_monthly_price} ₽</b>\n'
        f'Сумма без скидки: <b>{basket.subtotal} ₽</b>\n'
        f'Скидка: <b>{basket.discount_percent}%</b>\n'
        f'К оплате: <b>{basket.payable} ₽</b>'
    )


@router.message(F.text == '🎁 Тест на 24 часа')
async def trial_from_main_menu(message: Message, session: AsyncSession, settings: Settings) -> None:
    user = await get_or_create_user(message, session)
    is_admin = await _is_admin_user(session, user.tg_id, settings)

    if user.trial_issued_at:
        await message.answer(
            'Тест уже был использован ранее.',
            reply_markup=main_menu(show_trial=False, show_admin=is_admin),
        )
        return

    app_settings = await _load_app_settings(session)
    duration_text = _format_duration_text(effective_int_from_row(app_settings, 'trial_duration_days', settings.trial_duration_days, minimum=1))
    traffic_text = f'{effective_int_from_row(app_settings, 'trial_traffic_gb', settings.trial_traffic_gb, minimum=0)} ГБ'
    device_text = _format_device_label(effective_int_from_row(app_settings, 'trial_device_count', settings.trial_device_count, minimum=1))

    await message.answer(
        '🎁 <b>Бесплатный тест</b>\n\n'
        f'Вы получите доступ на {duration_text}, {traffic_text} трафика и {device_text}.\n'
        'Хотите активировать?',
        reply_markup=trial_confirm_keyboard(),
    )


@router.callback_query(TrialCallback.filter())
async def take_trial(
    callback: CallbackQuery,
    callback_data: TrialCallback,
    session: AsyncSession,
    settings: Settings,
    marzban: MarzbanClient,
) -> None:
    user = await get_or_create_user(callback, session)
    is_admin = await _is_admin_user(session, user.tg_id, settings)

    if callback_data.action == 'decline':
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            '👌 Хорошо, тест можно взять позже.',
            reply_markup=main_menu(show_trial=not bool(user.trial_issued_at), show_admin=is_admin),
        )
        await safe_callback_answer(callback)
        return

    try:
        await _service(session, settings, marzban).issue_trial(user)
        await session.commit()
    except ValueError as exc:
        await callback.message.answer(
            str(exc),
            reply_markup=main_menu(show_trial=not bool(user.trial_issued_at), show_admin=is_admin),
        )
        await safe_callback_answer(callback)
        return
    except MarzbanAPIError:
        await callback.message.answer(
            '❌ Не удалось активировать тест из-за ошибки панели. Попробуйте позже.',
            reply_markup=main_menu(show_trial=not bool(user.trial_issued_at), show_admin=is_admin),
        )
        await safe_callback_answer(callback)
        return

    latest = await SubscriptionRepository(session).get_latest(user.id)
    await callback.message.answer(
        '✅ Тестовая подписка активирована.',
        reply_markup=active_vpn_keyboard(subscription_id=latest.id if latest else 0),
    )
    await callback.message.answer(
        '🏠 Главное меню обновлено.',
        reply_markup=main_menu(show_trial=False, show_admin=is_admin),
    )
    await safe_callback_answer(callback)


@router.message(F.text == '👑 Мой VPN')
async def my_vpn(message: Message, session: AsyncSession, settings: Settings, marzban: MarzbanClient) -> None:
    user = await get_or_create_user(message, session)
    subs = await SubscriptionRepository(session).list_by_user_id(user.id)
    active_services = [(s.id, s.is_alive_local, s.service_id) for s in subs if s.is_alive_local]
    if active_services:
        await message.answer('👑 <b>Мой VPN</b>\n\nВыберите услугу:', reply_markup=vpn_services_keyboard(active_services))
        return
    await message.answer('🌐 <b>Подключение VPN</b>\n\nВыберите режим устройств:', reply_markup=device_mode_keyboard())


@router.callback_query(VpnCallback.filter(F.action == 'services'))
async def services_screen(callback: CallbackQuery, session: AsyncSession) -> None:
    user = await get_or_create_user(callback, session)
    await _services_screen(callback.message, session=session, user_id=user.id)
    await safe_callback_answer(callback)


@router.callback_query(VpnCallback.filter(F.action == 'buy_new'))
async def open_new_purchase_flow(callback: CallbackQuery) -> None:
    changed = await safe_edit_message_text(
        callback.message,
        _render_purchase_entry_text(),
        reply_markup=device_mode_keyboard(),
    )
    await safe_callback_answer(callback, 'Открываю оформление подписки' if changed else 'Это меню уже открыто')


@router.callback_query(VpnCallback.filter(F.action.in_({'renew', 'renew_early'})))
async def open_tariffs_renew(
    callback: CallbackQuery,
    callback_data: VpnCallback,
    session: AsyncSession,
) -> None:
    user = await get_or_create_user(callback, session)
    subscription = await _load_subscription_for_user(session, user.id, callback_data.subscription_id)
    if not subscription:
        await safe_callback_answer(callback, 'Подписка не найдена', show_alert=True)
        return

    if subscription.is_trial:
        await safe_callback_answer(callback, 'Тестовую подписку нельзя продлить', show_alert=True)
        return

    early = callback_data.action == 'renew_early'
    if subscription.is_alive_local and subscription.current_tariff_code:
        device_mode = subscription.used_device_mode or 'single'
        device_count = int(subscription.used_device_count or (1 if device_mode == 'single' else 0))
        max_months = await _get_max_months(session)
        rules = await PricingService.get_rules(session)

        await safe_edit_message_text(
            callback.message,
            await _render_months_text(
                session=session,
                package_code=subscription.current_tariff_code,
                device_mode=device_mode,
                device_count=device_count,
                months=1,
                early=early,
                user_balance=user.balance,
            ),
            reply_markup=months_keyboard(
                subscription.current_tariff_code,
                device_mode,
                device_count=device_count,
                months=1,
                early=early,
                subscription_id=subscription.id,
                max_months=max_months,
                max_discount_percent=rules.max_discount_percent,
            ),
        )
        await safe_callback_answer(callback, 'Выберите срок продления')
        return

    await safe_edit_message_text(
        callback.message,
        '🌐 <b>Продление подписки</b>\n\nВыберите режим устройств:',
        reply_markup=device_mode_keyboard(
            early=early,
            subscription_id=callback_data.subscription_id,
            package_code=subscription.current_tariff_code or 't250',
        ),
    )
    await safe_callback_answer(callback)


@router.callback_query(VpnCallback.filter(F.action == 'details'))
async def vpn_details(
    callback: CallbackQuery,
    callback_data: VpnCallback,
    session: AsyncSession,
    settings: Settings,
    marzban: MarzbanClient,
) -> None:
    user = await get_or_create_user(callback, session)
    subscription = await _load_subscription_for_user(session, user.id, callback_data.subscription_id)
    if not subscription:
        await safe_callback_answer(callback, 'Подписка не найдена', show_alert=True)
        return

    await _details_screen(callback.message, session=session, settings=settings, marzban=marzban, subscription=subscription)
    await safe_callback_answer(callback)


@router.callback_query(VpnCallback.filter(F.action == 'device_menu'))
async def device_menu(callback: CallbackQuery, callback_data: VpnCallback, session: AsyncSession) -> None:
    user = await get_or_create_user(callback, session)
    subscription = await _load_subscription_for_user(session, user.id, callback_data.subscription_id)
    if not subscription:
        await safe_callback_answer(callback, 'Подписка не найдена', show_alert=True)
        return

    await _device_menu_screen(callback.message, subscription.id)
    await safe_callback_answer(callback)


@router.callback_query(DeviceInfoCallback.filter(F.action == 'device_menu'))
async def device_menu_back(callback: CallbackQuery, callback_data: DeviceInfoCallback) -> None:
    await _device_menu_screen(callback.message, callback_data.subscription_id)
    await safe_callback_answer(callback)


@router.callback_query(DeviceInfoCallback.filter(F.action == 'os_info'))
async def device_os_info(callback: CallbackQuery, callback_data: DeviceInfoCallback, session: AsyncSession) -> None:
    await _device_os_screen(
        callback.message,
        session=session,
        subscription_id=callback_data.subscription_id,
        os_name=callback_data.os_name,
    )
    await safe_callback_answer(callback)


@router.callback_query(VpnCallback.filter(F.action == 'show_key'))
async def show_key(
    callback: CallbackQuery,
    callback_data: VpnCallback,
    session: AsyncSession,
    marzban: MarzbanClient,
    settings: Settings,
) -> None:
    user = await get_or_create_user(callback, session)
    subscription = await _load_subscription_for_user(session, user.id, callback_data.subscription_id)
    if not subscription:
        await safe_callback_answer(callback, 'Подписка не найдена', show_alert=True)
        return

    view = await _load_subscription_view(session, settings, marzban, subscription)
    sub_url = view['subscription_url']
    if not sub_url:
        await safe_callback_answer(callback, 'Ссылка подписки отсутствует', show_alert=True)
        return

    app_settings = await _load_app_settings(session)
    qr = await build_qr_png(str(sub_url))

    caption = (
        '🌐 Информация о вашей услуге следующая:\n'
        f'🔎 Статус услуги: {view["status_label"]}\n'
        f'Максимальное количество подключений: {view["online_limit_label"]}\n'
        'Протокол: VLESS\n'
        f'Номер услуги: {subscription.service_id}\n'
        f'♾️ Предоставленный трафик: {view["provided_label"]}\n'
        f'📅 Активен до: {format_dt(view["expire_dt"])}'
    )

    if subscription.is_trial:
        caption += '\n🎁 Тестовая подписка: продление, сброс трафика и докупка недоступны.'
    elif _is_unlimited_subscription(subscription):
        caption += '\n♾️ Безлимитный тариф: докупка трафика недоступна.'
    elif view['has_cycle_extra']:
        caption += f'\n➕ Доп. трафик текущего цикла: +{view["cycle_extra_label"]}'

    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass

    await callback.message.answer_photo(
        qr,
        caption=caption,
        reply_markup=key_photo_back_keyboard(subscription.id),
    )

    if effective_bool_from_row(app_settings, 'show_subscription_page_button', settings.show_subscription_page_button):
        await callback.message.answer(
            '🌐 Откройте страницу вашей подписки по кнопке ниже:',
            reply_markup=_subscription_link_keyboard(str(sub_url)),
            disable_web_page_preview=True,
        )

    await safe_callback_answer(callback)


@router.callback_query(VpnCallback.filter(F.action == 'back_from_key'))
async def back_from_key(callback: CallbackQuery, callback_data: VpnCallback) -> None:
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass

    await callback.message.answer(SCREEN2_TEXT, reply_markup=device_keyboard(callback_data.subscription_id))
    await safe_callback_answer(callback)


@router.callback_query(VpnCallback.filter(F.action == 'topup'))
async def topup_menu(callback: CallbackQuery, callback_data: VpnCallback, session: AsyncSession) -> None:
    user = await get_or_create_user(callback, session)
    subscription = await _load_subscription_for_user(session, user.id, callback_data.subscription_id)
    if not subscription:
        await safe_callback_answer(callback, 'Подписка не найдена', show_alert=True)
        return

    if subscription.is_trial:
        await safe_callback_answer(callback, 'Для тестовой подписки докупка трафика недоступна', show_alert=True)
        return

    if _is_unlimited_subscription(subscription):
        await safe_callback_answer(callback, 'Для безлимитного тарифа докупка трафика не требуется и недоступна', show_alert=True)
        return

    await callback.message.answer(
        'Выберите объем доп. трафика:',
        reply_markup=topup_keyboard(callback_data.subscription_id),
    )
    await safe_callback_answer(callback)


@router.callback_query(VpnCallback.filter(F.action == 'reset_traffic'))
async def show_reset_traffic_quote(
    callback: CallbackQuery,
    callback_data: VpnCallback,
    session: AsyncSession,
    settings: Settings,
    marzban: MarzbanClient,
) -> None:
    user = await get_or_create_user(callback, session)
    subscription = await _load_subscription_for_user(session, user.id, callback_data.subscription_id)
    if not subscription:
        await safe_callback_answer(callback, 'Подписка не найдена', show_alert=True)
        return

    if subscription.is_trial:
        await safe_callback_answer(callback, 'Для тестовой подписки сброс трафика недоступен', show_alert=True)
        return

    service = _service(session, settings, marzban)
    try:
        quote = await service.calculate_manual_reset_quote(subscription)
    except ValueError as exc:
        await safe_callback_answer(callback, str(exc), show_alert=True)
        return

    await safe_edit_message_text(
        callback.message,
        _render_reset_quote_text(subscription=subscription, quote=quote, balance=user.balance or Decimal('0.00')),
        reply_markup=_reset_traffic_quote_keyboard(subscription.id),
    )
    await safe_callback_answer(callback, 'Проверьте стоимость и подтвердите сброс')


@router.callback_query(VpnCallback.filter(F.action == 'reset_traffic_confirm'))
async def confirm_reset_traffic(
    callback: CallbackQuery,
    callback_data: VpnCallback,
    session: AsyncSession,
    settings: Settings,
    marzban: MarzbanClient,
) -> None:
    current_user = await get_or_create_user(callback, session)
    locked_user = await UserRepository(session).get_by_id_for_update(current_user.id)
    subscription = await SubscriptionRepository(session).get_by_id_for_update(callback_data.subscription_id)

    if locked_user is None:
        await safe_callback_answer(callback, 'Пользователь не найден', show_alert=True)
        return
    if subscription is None or subscription.user_id != locked_user.id:
        await safe_callback_answer(callback, 'Подписка не найдена', show_alert=True)
        return
    if subscription.is_trial:
        await safe_callback_answer(callback, 'Для тестовой подписки сброс трафика недоступен', show_alert=True)
        return

    service = _service(session, settings, marzban)
    try:
        quote, _ = await service.reset_traffic_paid(locked_user, subscription)
        await session.commit()
    except ValueError as exc:
        await session.rollback()
        await safe_callback_answer(callback, str(exc), show_alert=True)
        return
    except MarzbanAPIError:
        await session.rollback()
        await safe_callback_answer(callback, 'Не удалось выполнить сброс через панель. Попробуйте позже.', show_alert=True)
        return

    await _details_screen(callback.message, session=session, settings=settings, marzban=marzban, subscription=subscription)
    await safe_callback_answer(callback, f'Трафик сброшен. Списано {quote.reset_price} ₽')


@router.callback_query(VpnCallback.filter(F.action == 'back_main'))
async def back_main(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    user = await get_or_create_user(callback, session)
    await _services_screen(callback.message, session=session, user_id=user.id)
    await safe_callback_answer(callback, 'Возвращаю к Мой VPN')


_SNOOZE_TTL_SECONDS = 24 * 3600


@router.callback_query(NotificationCallback.filter(F.action == 'snooze'))
async def snooze_notification(
    callback: CallbackQuery,
    callback_data: NotificationCallback,
    session: AsyncSession,
    cache: CacheService,
) -> None:
    user = await get_or_create_user(callback, session)
    code = (callback_data.code or '').strip()
    if not code:
        await safe_callback_answer(callback, 'Не удалось понять, какое уведомление отключить.', show_alert=True)
        return

    redis_client = getattr(cache, 'redis', None)
    if redis_client is None:
        await safe_callback_answer(callback, 'Snooze временно недоступен. Попробуйте позже.', show_alert=True)
        return

    key = NotificationDispatcher.snooze_key(
        prefix=cache.prefix,
        user_id=user.id,
        code=code,
    )
    try:
        await redis_client.set(key, '1', ex=_SNOOZE_TTL_SECONDS)
    except Exception:
        logger.exception('Failed to set snooze key for user_id=%s code=%s', user.id, code)
        await safe_callback_answer(callback, 'Не удалось включить snooze. Попробуйте позже.', show_alert=True)
        return

    await safe_callback_answer(callback, '🔕 Не буду напоминать 24 часа.')
