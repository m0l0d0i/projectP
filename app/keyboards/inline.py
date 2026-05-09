from __future__ import annotations

from decimal import Decimal

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from typing import TYPE_CHECKING

from app.db.models import BroadcastJobStatus, Invoice, SupportTicketStatus
from app.services.tariffs import PricingService

if TYPE_CHECKING:
    from app.services.tariffs import TopUpOption  # noqa: F401


class TrialCallback(CallbackData, prefix='trial'):
    action: str


class TrafficPackageCallback(CallbackData, prefix='traffic'):
    action: str = 'set'
    package_code: str = 't250'
    device_mode: str = 'single'
    device_count: int = 0
    early: int = 0
    subscription_id: int = 0


class DeviceModeCallback(CallbackData, prefix='devmode'):
    package_code: str = 't250'
    mode: str = 'single'
    early: int = 0
    subscription_id: int = 0


class DeviceCountCallback(CallbackData, prefix='devcount'):
    action: str
    package_code: str = 't250'
    count: int = 2
    early: int = 0
    subscription_id: int = 0


class MonthsCallback(CallbackData, prefix='months'):
    action: str
    package_code: str = 't250'
    months: int = 1
    device_mode: str = 'single'
    device_count: int = 0
    early: int = 0
    subscription_id: int = 0


class InvoiceActionCallback(CallbackData, prefix='invoice'):
    action: str
    invoice_id: int


class TopUpCallback(CallbackData, prefix='topup'):
    code: str
    subscription_id: int = 0


class NotificationCallback(CallbackData, prefix='notif'):
    """FEA-NOTIF: действия из inline-кнопок smart-push (snooze и т.п.)."""

    action: str  # 'snooze'
    code: str = ''  # код правила, к которому относится действие


class DeviceInfoCallback(CallbackData, prefix='device_info'):
    action: str
    subscription_id: int = 0
    os_name: str = 'iOS'


class RulesCallback(CallbackData, prefix='rules'):
    doc: str


class SupportTicketCallback(CallbackData, prefix='support_ticket'):
    action: str
    ticket_id: int = 0
    page: int = 0
    msg_page: int = 0


class VpnCallback(CallbackData, prefix='vpn'):
    action: str
    subscription_id: int = 0


class ProfileCallback(CallbackData, prefix='profile'):
    action: str


class BalanceAmountCallback(CallbackData, prefix='balance_amount'):
    action: str
    amount: int


def pager_row(action_prefix: str, page: int, has_prev: bool, has_next: bool, *, ticket_id: int = 0) -> list[InlineKeyboardButton]:
    prev_action = {'page': 'page', 'history': 'history_page'}[action_prefix]
    buttons: list[InlineKeyboardButton] = []
    if has_prev:
        buttons.append(
            InlineKeyboardButton(
                text='⬅️',
                callback_data=SupportTicketCallback(
                    action=prev_action,
                    ticket_id=ticket_id,
                    page=page - 1,
                    msg_page=page - 1,
                ).pack(),
            )
        )
    buttons.append(
        InlineKeyboardButton(
            text=f'{page + 1}',
            callback_data=SupportTicketCallback(
                action='noop',
                ticket_id=ticket_id,
                page=page,
                msg_page=page,
            ).pack(),
        )
    )
    if has_next:
        buttons.append(
            InlineKeyboardButton(
                text='➡️',
                callback_data=SupportTicketCallback(
                    action=prev_action,
                    ticket_id=ticket_id,
                    page=page + 1,
                    msg_page=page + 1,
                ).pack(),
            )
        )
    return buttons


def trial_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='🚀 Активировать тест', callback_data=TrialCallback(action='take').pack())],
            [InlineKeyboardButton(text='🙅 Пока не надо', callback_data=TrialCallback(action='decline').pack())],
        ]
    )


def vpn_services_keyboard(services: list[tuple[int, bool, str]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for subscription_id, is_active, service_id in services:
        dot = '🟢' if is_active else '🔴'
        rows.append(
            [
                InlineKeyboardButton(
                    text=f'{dot} Услуга {service_id}',
                    callback_data=VpnCallback(action='details', subscription_id=subscription_id).pack(),
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text='🌐 Оформить новую подписку на VPN', callback_data=VpnCallback(action='buy_new').pack())]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def active_vpn_keyboard(subscription_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='🌐 Открыть услугу', callback_data=VpnCallback(action='details', subscription_id=subscription_id).pack())],
        ]
    )


def vpn_details_keyboard(subscription_id: int, *, is_trial: bool = False) -> InlineKeyboardMarkup:
    if is_trial:
        return trial_vpn_details_keyboard(subscription_id)

    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text='📱 Подключить устройство', callback_data=VpnCallback(action='device_menu', subscription_id=subscription_id).pack())],
        [InlineKeyboardButton(text='⏳ Продлить подписку', callback_data=VpnCallback(action='renew', subscription_id=subscription_id).pack())],
        [InlineKeyboardButton(text='🔄 Сбросить трафик', callback_data=VpnCallback(action='reset_traffic', subscription_id=subscription_id).pack())],
        [InlineKeyboardButton(text='📦 Докупить трафик', callback_data=VpnCallback(action='topup', subscription_id=subscription_id).pack())],
        [InlineKeyboardButton(text='⬅️ Назад', callback_data=VpnCallback(action='services').pack())],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def trial_vpn_details_keyboard(subscription_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='📱 Подключить устройство', callback_data=VpnCallback(action='device_menu', subscription_id=subscription_id).pack())],
            [InlineKeyboardButton(text='⬅️ Назад', callback_data=VpnCallback(action='services').pack())],
        ]
    )


def _format_money_label(value: Decimal | int | float | str) -> str:
    amount = Decimal(str(value))
    if amount == amount.to_integral_value():
        return str(int(amount))
    return format(amount.normalize(), 'f').rstrip('0').rstrip('.')


def reset_traffic_confirm_keyboard(subscription_id: int, reset_price: Decimal | int | float | str) -> InlineKeyboardMarkup:
    price_label = _format_money_label(reset_price)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f'✅ Подтвердить сброс за {price_label} ₽',
                    callback_data=VpnCallback(action='reset_traffic_confirm', subscription_id=subscription_id).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text='⬅️ Отмена',
                    callback_data=VpnCallback(action='details', subscription_id=subscription_id).pack(),
                )
            ],
        ]
    )


def subscription_link_keyboard(subscription_url: str, *, text: str = 'Страница вашей подписки') -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, url=subscription_url)],
        ]
    )


def low_traffic_alert_keyboard(
    subscription_id: int = 0,
    *,
    allow_topup: bool = True,
    allow_early_renew: bool = True,
    notification_code: str = '',
) -> InlineKeyboardMarkup:
    """Клавиатура для smart-push о трафике (FEA-NOTIF).

    `notification_code` — код правила, к которому привязан snooze (если
    оставлен пустым, кнопка «Не напоминать 24ч» не отображается).
    """
    rows: list[list[InlineKeyboardButton]] = []
    if allow_topup:
        rows.append([
            InlineKeyboardButton(
                text='➕ 50 ГБ',
                callback_data=TopUpCallback(code='topup50', subscription_id=subscription_id).pack(),
            ),
            InlineKeyboardButton(
                text='➕ 100 ГБ',
                callback_data=TopUpCallback(code='topup100', subscription_id=subscription_id).pack(),
            ),
        ])
    if allow_early_renew:
        rows.append([InlineKeyboardButton(
            text='⏳ Продлить 1 мес',
            callback_data=VpnCallback(action='renew_early', subscription_id=subscription_id).pack(),
        )])
    if notification_code:
        rows.append([InlineKeyboardButton(
            text='🔕 Не напоминать 24ч',
            callback_data=NotificationCallback(action='snooze', code=notification_code).pack(),
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def device_mode_keyboard(*, early: bool = False, subscription_id: int = 0, package_code: str = 't250') -> InlineKeyboardMarkup:
    back_text = '⬅️ Назад к услуге' if early and subscription_id else '⬅️ Назад к Мой VPN'
    back_action = 'details' if early and subscription_id else 'services'
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='1️⃣ 1 устройство', callback_data=DeviceModeCallback(package_code=package_code, mode='single', early=int(early), subscription_id=subscription_id).pack())],
            [InlineKeyboardButton(text='🔢 Выбрать количество устройств', callback_data=DeviceModeCallback(package_code=package_code, mode='custom', early=int(early), subscription_id=subscription_id).pack())],
            [InlineKeyboardButton(text='♾️ Неограниченно устройств', callback_data=DeviceModeCallback(package_code=package_code, mode='unlimited', early=int(early), subscription_id=subscription_id).pack())],
            [InlineKeyboardButton(text=back_text, callback_data=VpnCallback(action=back_action, subscription_id=subscription_id).pack())],
        ]
    )


def device_count_keyboard(package_code: str, count: int = 2, early: bool = False, subscription_id: int = 0) -> InlineKeyboardMarkup:
    count = max(2, min(PricingService.MAX_CUSTOM_DEVICES, count))
    back_text = '⬅️ Назад к услуге' if early and subscription_id else '⬅️ Назад к выбору устройств'
    back_action = 'details' if early and subscription_id else 'buy_new'
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text='➖ 1',
                    callback_data=DeviceCountCallback(
                        action='set',
                        package_code=package_code,
                        count=max(2, count - 1),
                        early=int(early),
                        subscription_id=subscription_id,
                    ).pack(),
                ),
                InlineKeyboardButton(
                    text=f'📱 {count}',
                    callback_data=DeviceCountCallback(
                        action='noop',
                        package_code=package_code,
                        count=count,
                        early=int(early),
                        subscription_id=subscription_id,
                    ).pack(),
                ),
                InlineKeyboardButton(
                    text='➕ 1',
                    callback_data=DeviceCountCallback(
                        action='set',
                        package_code=package_code,
                        count=min(PricingService.MAX_CUSTOM_DEVICES, count + 1),
                        early=int(early),
                        subscription_id=subscription_id,
                    ).pack(),
                ),
            ],
            [InlineKeyboardButton(text='✅ Продолжить', callback_data=DeviceCountCallback(action='confirm', package_code=package_code, count=count, early=int(early), subscription_id=subscription_id).pack())],
            [InlineKeyboardButton(text='♾️ Безлимит устройств', callback_data=DeviceModeCallback(package_code=package_code, mode='unlimited', early=int(early), subscription_id=subscription_id).pack())],
            [InlineKeyboardButton(text=back_text, callback_data=VpnCallback(action=back_action, subscription_id=subscription_id).pack())],
        ]
    )


def _purchase_back_button(*, package_code: str, device_mode: str, device_count: int = 0, early: bool = False, subscription_id: int = 0) -> InlineKeyboardButton:
    back_text = '⬅️ Назад к услуге' if early and subscription_id else '⬅️ Назад к устройствам'
    return InlineKeyboardButton(
        text=back_text,
        callback_data=TrafficPackageCallback(
            action='back_to_devices',
            package_code=package_code,
            device_mode=device_mode,
            device_count=device_count,
            early=int(early),
            subscription_id=subscription_id,
        ).pack(),
    )


def traffic_selector_keyboard(package_code: str, device_mode: str, device_count: int = 0, *, early: bool = False, subscription_id: int = 0) -> InlineKeyboardMarkup:
    traffic_gb = PricingService.parse_package_code(package_code)
    if traffic_gb is None:
        label = '♾️ Безлимит'
        minus_code = PricingService.package_code(PricingService.TRAFFIC_OPTIONS[-1])
        plus_code = 'unlim'
    else:
        label = f'🌐 {traffic_gb} ГБ'
        idx = PricingService.TRAFFIC_OPTIONS.index(PricingService.normalize_traffic_gb(traffic_gb))
        minus_gb = PricingService.TRAFFIC_OPTIONS[max(0, idx - 1)]
        plus_gb = PricingService.TRAFFIC_OPTIONS[min(len(PricingService.TRAFFIC_OPTIONS) - 1, idx + 1)]
        minus_code = PricingService.package_code(minus_gb)
        plus_code = PricingService.package_code(plus_gb)

    rows = [
        [
            InlineKeyboardButton(text='➖ 50 ГБ', callback_data=TrafficPackageCallback(action='set', package_code=minus_code, device_mode=device_mode, device_count=device_count, early=int(early), subscription_id=subscription_id).pack()),
            InlineKeyboardButton(text=label, callback_data=TrafficPackageCallback(action='noop', package_code=package_code, device_mode=device_mode, device_count=device_count, early=int(early), subscription_id=subscription_id).pack()),
            InlineKeyboardButton(text='➕ 50 ГБ', callback_data=TrafficPackageCallback(action='set', package_code=plus_code, device_mode=device_mode, device_count=device_count, early=int(early), subscription_id=subscription_id).pack()),
        ],
        [InlineKeyboardButton(text='✅ Продолжить', callback_data=TrafficPackageCallback(action='confirm', package_code=package_code, device_mode=device_mode, device_count=device_count, early=int(early), subscription_id=subscription_id).pack())],
    ]

    if package_code == 'unlim':
        rows.append(
            [InlineKeyboardButton(text='✅ Выбран безлимит трафика', callback_data=TrafficPackageCallback(action='noop', package_code='unlim', device_mode=device_mode, device_count=device_count, early=int(early), subscription_id=subscription_id).pack())]
        )
    else:
        rows.append(
            [InlineKeyboardButton(text='♾️ Безлимит трафика', callback_data=TrafficPackageCallback(action='set', package_code='unlim', device_mode=device_mode, device_count=device_count, early=int(early), subscription_id=subscription_id).pack())]
        )

    rows.append([_purchase_back_button(package_code=package_code, device_mode=device_mode, device_count=device_count, early=early, subscription_id=subscription_id)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def months_keyboard(package_code: str, device_mode: str, device_count: int = 0, months: int = 1, early: bool = False, subscription_id: int = 0, max_months: int | None = None, max_discount_percent: Decimal = Decimal('0')) -> InlineKeyboardMarkup:
    effective_max_months = max(PricingService.MIN_MONTHS, int(max_months or PricingService.MAX_MONTHS))
    months = min(effective_max_months, max(PricingService.MIN_MONTHS, months))
    discount = PricingService.discount_percent(months, Decimal(str(max_discount_percent)))
    discount_label = 'без скидки' if discount <= 0 else f'скидка {(discount * 100):.1f}%'
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text='➖ 1', callback_data=MonthsCallback(action='set', package_code=package_code, months=max(1, months - 1), device_mode=device_mode, device_count=device_count, early=int(early), subscription_id=subscription_id).pack()),
                InlineKeyboardButton(text=f'📆 {months} мес.', callback_data=MonthsCallback(action='noop', package_code=package_code, months=months, device_mode=device_mode, device_count=device_count, early=int(early), subscription_id=subscription_id).pack()),
                InlineKeyboardButton(text='➕ 1', callback_data=MonthsCallback(action='set', package_code=package_code, months=min(effective_max_months, months + 1), device_mode=device_mode, device_count=device_count, early=int(early), subscription_id=subscription_id).pack()),
            ],
            [InlineKeyboardButton(text=f'🎁 {discount_label}', callback_data=MonthsCallback(action='noop', package_code=package_code, months=months, device_mode=device_mode, device_count=device_count, early=int(early), subscription_id=subscription_id).pack())],
            [InlineKeyboardButton(text='✅ Продолжить', callback_data=MonthsCallback(action='confirm', package_code=package_code, months=months, device_mode=device_mode, device_count=device_count, early=int(early), subscription_id=subscription_id).pack())],
            [_purchase_back_button(package_code=package_code, device_mode=device_mode, device_count=device_count, early=early, subscription_id=subscription_id)],
        ]
    )


def get_payment_keyboard(
    payable_amount: Decimal | float | int,
    use_balance: bool,
    user_balance: Decimal | float | int,
    invoice_id: int,
    *,
    payment_url: str | None = None,
    allow_balance: bool = True,
    confirm_label: str | None = None,
) -> InlineKeyboardMarkup:
    payable = Decimal(str(payable_amount))
    balance = Decimal(str(user_balance))

    rows: list[list[InlineKeyboardButton]] = []
    if payment_url:
        rows.append([InlineKeyboardButton(text='💳 Перейти к оплате', url=payment_url)])

    if allow_balance and balance > 0:
        balance_text = '✅ Баланс применен' if use_balance else '💰 Использовать баланс'
        rows.append([InlineKeyboardButton(text=balance_text, callback_data=InvoiceActionCallback(action='toggle_balance', invoice_id=invoice_id).pack())])

    if confirm_label is None:
        confirm_label = '🚀 Активировать (0 ₽)' if payable <= 0 else f'💳 Оплатить {payable} ₽'

    rows.append([InlineKeyboardButton(text=confirm_label, callback_data=InvoiceActionCallback(action='confirm', invoice_id=invoice_id).pack())])
    rows.append([InlineKeyboardButton(text='❌ Отмена', callback_data=InvoiceActionCallback(action='cancel', invoice_id=invoice_id).pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def invoice_cart_keyboard(invoice: Invoice, user_balance: Decimal | float | int = 0) -> InlineKeyboardMarkup:
    payable_amount = Decimal(invoice.payable_amount)
    if payable_amount <= 0:
        confirm_label = '🚀 Активировать (0 ₽)'
    elif (invoice.provider or '').lower() == 'platega':
        confirm_label = '🔄 Проверить оплату'
    else:
        confirm_label = f'💳 Оплатить {payable_amount} ₽'

    return get_payment_keyboard(
        payable_amount=payable_amount,
        use_balance=Decimal(invoice.balance_used) > 0,
        user_balance=user_balance,
        invoice_id=invoice.id,
        payment_url=invoice.payment_url,
        allow_balance=getattr(invoice.purpose, 'value', str(invoice.purpose)) != 'balance_topup',
        confirm_label=confirm_label,
    )


def topup_keyboard(
    subscription_id: int,
    topups: 'list[TopUpOption]',
) -> InlineKeyboardMarkup:
    """Меню «Докупить трафик» (FEA-A8).

    `topups` — список из `PricingService.list_topups(session)`. Опция с
    `is_best_price=True` либо собственным `badge_label` отображается
    дополнительной пометкой в тексте кнопки.
    """
    rows: list[list[InlineKeyboardButton]] = []
    for topup in topups:
        badge = topup.display_badge
        text = f'{topup.title} — {topup.amount} ₽'
        if badge:
            text = f'{text}  {badge}'
        rows.append([InlineKeyboardButton(
            text=text,
            callback_data=TopUpCallback(
                code=topup.code, subscription_id=subscription_id,
            ).pack(),
        )])
    rows.append([InlineKeyboardButton(
        text='⬅️ Назад',
        callback_data=VpnCallback(action='details', subscription_id=subscription_id).pack(),
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def balance_topup_keyboard(amount: int, *, min_amount: int = 50, step_amount: int = 50) -> InlineKeyboardMarkup:
    min_amount = max(1, int(min_amount))
    step_amount = max(1, int(step_amount))
    amount = max(min_amount, int(amount))

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=f'➖ {step_amount} ₽', callback_data=BalanceAmountCallback(action='set', amount=max(min_amount, amount - step_amount)).pack()),
                InlineKeyboardButton(text=f'{amount} ₽', callback_data=BalanceAmountCallback(action='noop', amount=amount).pack()),
                InlineKeyboardButton(text=f'➕ {step_amount} ₽', callback_data=BalanceAmountCallback(action='set', amount=amount + step_amount).pack()),
            ],
            [InlineKeyboardButton(text='✅ Выставить счёт', callback_data=BalanceAmountCallback(action='confirm', amount=amount).pack())],
            [InlineKeyboardButton(text='⬅️ Назад', callback_data=BalanceAmountCallback(action='cancel', amount=amount).pack())],
        ]
    )


def device_keyboard(subscription_id: int) -> InlineKeyboardMarkup:
    devices = ['iOS', 'Android', 'macOS', 'Windows', 'Linux', 'AndroidTV']
    rows: list[list[InlineKeyboardButton]] = []
    for left, right in zip(devices[::2], devices[1::2]):
        rows.append([
            InlineKeyboardButton(text=left, callback_data=DeviceInfoCallback(action='os_info', subscription_id=subscription_id, os_name=left).pack()),
            InlineKeyboardButton(text=right, callback_data=DeviceInfoCallback(action='os_info', subscription_id=subscription_id, os_name=right).pack()),
        ])
    rows.append([InlineKeyboardButton(text='🔑 Посмотреть ключ / QR', callback_data=VpnCallback(action='show_key', subscription_id=subscription_id).pack())])
    rows.append([InlineKeyboardButton(text='⏪ Назад', callback_data=VpnCallback(action='details', subscription_id=subscription_id).pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def device_os_keyboard(subscription_id: int, os_name: str, *, download_url: str | None = None, guide_url: str | None = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if download_url:
        rows.append([InlineKeyboardButton(text='📥 Скачать приложение', url=download_url)])
    if guide_url:
        rows.append([InlineKeyboardButton(text='📖 Подробная инструкция', url=guide_url)])
    if download_url or guide_url:
        rows.append([InlineKeyboardButton(text='🔑 Посмотреть ключ / QR', callback_data=VpnCallback(action='show_key', subscription_id=subscription_id).pack())])
    rows.append([InlineKeyboardButton(text='⏪ Назад', callback_data=DeviceInfoCallback(action='device_menu', subscription_id=subscription_id, os_name=os_name).pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def key_photo_back_keyboard(subscription_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='⏪ Назад', callback_data=VpnCallback(action='back_from_key', subscription_id=subscription_id).pack())],
        ]
    )


def rules_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='📜 Правила сервиса', callback_data=RulesCallback(doc='rules').pack())],
            [InlineKeyboardButton(text='📄 Пользовательское соглашение', callback_data=RulesCallback(doc='rules_of_use').pack())],
            [InlineKeyboardButton(text='📄 Политика конфиденциальности', callback_data=RulesCallback(doc='privacy_policy').pack())],
        ]
    )


def profile_keyboard(*, show_referral_code: bool = True) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text='🤝 Реферальная программа', callback_data=ProfileCallback(action='ref_program').pack())],
        [InlineKeyboardButton(text='🎁 Ввести промокод', callback_data=ProfileCallback(action='enter_promo').pack())],
    ]
    if show_referral_code:
        rows.append([InlineKeyboardButton(text='🎟️ Промокод реферала', callback_data=ProfileCallback(action='referral_code').pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def profile_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='⬅️ Назад', callback_data=ProfileCallback(action='back').pack())]])


def support_ticket_keyboard(ticket_id: int, *, is_open: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if is_open:
        rows.append([InlineKeyboardButton(text='✍️ Ответить', callback_data=SupportTicketCallback(action='reply', ticket_id=ticket_id).pack())])
        rows.append([InlineKeyboardButton(text='✅ Закрыть заявку', callback_data=SupportTicketCallback(action='close', ticket_id=ticket_id).pack())])
    rows.append([InlineKeyboardButton(text='📜 Посмотреть сообщения', callback_data=SupportTicketCallback(action='history', ticket_id=ticket_id, msg_page=0).pack())])
    rows.append([InlineKeyboardButton(text='⬅️ Назад', callback_data=SupportTicketCallback(action='back').pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def support_history_keyboard(ticket_id: int, *, page: int, has_prev: bool, has_next: bool, is_open: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    nav = pager_row('history', page, has_prev, has_next, ticket_id=ticket_id)
    if nav:
        rows.append(nav)
    if is_open:
        rows.append([InlineKeyboardButton(text='✍️ Ответить', callback_data=SupportTicketCallback(action='reply', ticket_id=ticket_id).pack())])
        rows.append([InlineKeyboardButton(text='✅ Закрыть заявку', callback_data=SupportTicketCallback(action='close', ticket_id=ticket_id).pack())])
    rows.append([InlineKeyboardButton(text='⬅️ Назад', callback_data=SupportTicketCallback(action='open', ticket_id=ticket_id).pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def support_list_keyboard(ticket_ids: list[int], page: int, page_size: int = 6) -> InlineKeyboardMarkup:
    start = page * page_size
    chunk = ticket_ids[start:start + page_size]
    rows: list[list[InlineKeyboardButton]] = []
    for tid in chunk:
        rows.append([InlineKeyboardButton(text=f'Заявка #{tid}', callback_data=SupportTicketCallback(action='open', ticket_id=tid, page=page).pack())])

    nav = pager_row('page', page, page > 0, start + page_size < len(ticket_ids))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton(text='📝 Написать в поддержку', callback_data=SupportTicketCallback(action='new').pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _support_ticket_status_emoji(status: SupportTicketStatus | str) -> str:
    value = status.value if isinstance(status, SupportTicketStatus) else str(status)
    return {
        'waiting_operator': '🟡',
        'waiting_user': '🔵',
        'closed': '⚫',
        'open': '🟡',
    }.get(value, '•')


def _support_ticket_status_label(status: SupportTicketStatus | str) -> str:
    value = status.value if isinstance(status, SupportTicketStatus) else str(status)
    return {
        'waiting_operator': 'Ждет оператора',
        'waiting_user': 'Ждет пользователя',
        'closed': 'Закрыт',
        'open': 'Активен',
    }.get(value, value or 'Неизвестно')


def _promo_status_label(promo) -> str:
    if not getattr(promo, 'is_active', False):
        return 'Архивный'
    expires_at = getattr(promo, 'expires_at', None)
    if expires_at is not None:
        try:
            from datetime import datetime, timezone
            now_utc = datetime.now(timezone.utc)
            if getattr(expires_at, 'tzinfo', None) is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= now_utc:
                return 'Истек'
        except Exception:
            pass
    max_uses = getattr(promo, 'max_uses', None)
    used_count = int(getattr(promo, 'used_count', 0) or 0)
    if max_uses is not None and used_count >= int(max_uses):
        return 'Исчерпан'
    return 'Активен'


def support_admin_ticket_keyboard(ticket_id: int, *, is_open: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text='📜 История', callback_data=SupportTicketCallback(action='admin_history', ticket_id=ticket_id, msg_page=0).pack())]]
    if is_open:
        rows.insert(0, [InlineKeyboardButton(text='✅ Закрыть', callback_data=SupportTicketCallback(action='admin_close', ticket_id=ticket_id).pack())])
        rows.append([InlineKeyboardButton(text='ℹ️ Как ответить', callback_data=SupportTicketCallback(action='admin_reply_help', ticket_id=ticket_id).pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def support_admin_history_keyboard(ticket_id: int, *, page: int, has_prev: bool, has_next: bool, is_open: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    buttons: list[InlineKeyboardButton] = []
    if has_prev:
        buttons.append(InlineKeyboardButton(text='⬅️', callback_data=SupportTicketCallback(action='admin_history', ticket_id=ticket_id, msg_page=page - 1).pack()))
    buttons.append(InlineKeyboardButton(text=f'{page + 1}', callback_data=SupportTicketCallback(action='noop', ticket_id=ticket_id, msg_page=page).pack()))
    if has_next:
        buttons.append(InlineKeyboardButton(text='➡️', callback_data=SupportTicketCallback(action='admin_history', ticket_id=ticket_id, msg_page=page + 1).pack()))
    if buttons:
        rows.append(buttons)
    rows.extend(support_admin_ticket_keyboard(ticket_id, is_open=is_open).inline_keyboard)
    return InlineKeyboardMarkup(inline_keyboard=rows)


class AdminCallback(CallbackData, prefix='admin'):
    section: str
    action: str = 'open'
    item_id: int = 0
    page: int = 0
    field: str = ''


def admin_main_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text='👤 Пользователи', callback_data=AdminCallback(section='users').pack()), InlineKeyboardButton(text='🎟 Промокоды', callback_data=AdminCallback(section='promos').pack())],
        [InlineKeyboardButton(text='📦 Тарифы', callback_data=AdminCallback(section='tariffs').pack()), InlineKeyboardButton(text='💸 Цены', callback_data=AdminCallback(section='price').pack())],
        [InlineKeyboardButton(text='🎧 Тикеты', callback_data=AdminCallback(section='tickets').pack()), InlineKeyboardButton(text='📢 Рассылки', callback_data=AdminCallback(section='broadcast').pack())],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_back_keyboard(section: str = 'root') -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='⬅️ Назад', callback_data=AdminCallback(section=section, action='back').pack())]])


def admin_users_keyboard(users, page: int, has_prev: bool, has_next: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f'{u.tg_id} @{u.username or "-"}', callback_data=AdminCallback(section='users', action='open', item_id=u.id, page=page).pack())] for u in users]
    nav = []
    if has_prev:
        nav.append(InlineKeyboardButton(text='⬅️', callback_data=AdminCallback(section='users', action='page', page=page - 1).pack()))
    nav.append(InlineKeyboardButton(text=f'{page + 1}', callback_data=AdminCallback(section='noop', action='noop').pack()))
    if has_next:
        nav.append(InlineKeyboardButton(text='➡️', callback_data=AdminCallback(section='users', action='page', page=page + 1).pack()))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text='🔎 Найти пользователя', callback_data=AdminCallback(section='users', action='search').pack())])
    rows.append([InlineKeyboardButton(text='⬅️ Назад', callback_data=AdminCallback(section='root').pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_user_keyboard(user) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text='➕ Баланс', callback_data=AdminCallback(section='users', action='add_balance', item_id=user.id).pack()), InlineKeyboardButton(text='➖ Баланс', callback_data=AdminCallback(section='users', action='remove_balance', item_id=user.id).pack())],
        [InlineKeyboardButton(text='🔓 Разблокировать' if user.is_blocked else '🔒 Заблокировать', callback_data=AdminCallback(section='users', action='toggle_block', item_id=user.id).pack())],
        [InlineKeyboardButton(text='⬅️ К списку', callback_data=AdminCallback(section='users').pack())],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_promos_keyboard(promos, page: int, has_prev: bool, has_next: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f'{p.code} ({_promo_status_label(p)})', callback_data=AdminCallback(section='promos', action='open', item_id=p.id, page=page).pack())] for p in promos]
    nav = []
    if has_prev:
        nav.append(InlineKeyboardButton(text='⬅️', callback_data=AdminCallback(section='promos', action='page', page=page - 1).pack()))
    nav.append(InlineKeyboardButton(text=f'{page + 1}', callback_data=AdminCallback(section='noop', action='noop').pack()))
    if has_next:
        nav.append(InlineKeyboardButton(text='➡️', callback_data=AdminCallback(section='promos', action='page', page=page + 1).pack()))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text='➕ Создать промокод', callback_data=AdminCallback(section='promos', action='create').pack())])
    rows.append([InlineKeyboardButton(text='⬅️ Назад', callback_data=AdminCallback(section='root').pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_promo_keyboard(promo) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='⚙️ Редактировать', callback_data=AdminCallback(section='promos', action='edit', item_id=promo.id).pack())],
        [InlineKeyboardButton(text='🟢 Активировать' if not promo.is_active else '🛑 Деактивировать', callback_data=AdminCallback(section='promos', action='toggle_active', item_id=promo.id).pack())],
        [InlineKeyboardButton(text='🗑 Удалить', callback_data=AdminCallback(section='promos', action='delete', item_id=promo.id).pack())],
        [InlineKeyboardButton(text='⬅️ К списку', callback_data=AdminCallback(section='promos').pack())],
    ])


def admin_tickets_keyboard(tickets, page: int, has_prev: bool, has_next: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f'#{t.id} {_support_ticket_status_emoji(getattr(t, "status", ""))} {_support_ticket_status_label(getattr(t, "status", ""))}', callback_data=AdminCallback(section='tickets', action='open', item_id=t.id, page=page).pack())] for t in tickets]
    nav = []
    if has_prev:
        nav.append(InlineKeyboardButton(text='⬅️', callback_data=AdminCallback(section='tickets', action='page', page=page - 1).pack()))
    nav.append(InlineKeyboardButton(text=f'{page + 1}', callback_data=AdminCallback(section='noop', action='noop').pack()))
    if has_next:
        nav.append(InlineKeyboardButton(text='➡️', callback_data=AdminCallback(section='tickets', action='page', page=page + 1).pack()))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text='⬅️ Назад', callback_data=AdminCallback(section='root').pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_ticket_keyboard(ticket_id: int, is_open: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text='📜 История', callback_data=AdminCallback(section='tickets', action='history', item_id=ticket_id).pack())]]
    if is_open:
        rows.insert(0, [InlineKeyboardButton(text='✅ Закрыть', callback_data=AdminCallback(section='tickets', action='close', item_id=ticket_id).pack())])
    rows.append([InlineKeyboardButton(text='⬅️ К списку', callback_data=AdminCallback(section='tickets').pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_price_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text='Изменить базовую цену', callback_data=AdminCallback(section='price', action='edit', field='base_price').pack())],
        [InlineKeyboardButton(text='Изменить цену за +50 ГБ', callback_data=AdminCallback(section='price', action='edit', field='traffic_step_price').pack())],
        [InlineKeyboardButton(text='Изменить цену за устройство', callback_data=AdminCallback(section='price', action='edit', field='device_step_price').pack())],
        [InlineKeyboardButton(text='Изменить безлимит устройств', callback_data=AdminCallback(section='price', action='edit', field='unlimited_devices_price').pack())],
        [InlineKeyboardButton(text='Изменить безлимитный тариф', callback_data=AdminCallback(section='price', action='edit', field='unlimited_combo_price').pack())],
        [InlineKeyboardButton(text='Изменить мин. пополнение', callback_data=AdminCallback(section='price', action='edit', field='min_topup_amount').pack())],
        [InlineKeyboardButton(text='Изменить max скидку %', callback_data=AdminCallback(section='price', action='edit', field='max_discount_percent').pack())],
        [InlineKeyboardButton(text='⬅️ Назад', callback_data=AdminCallback(section='root').pack())],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _broadcast_status_emoji(status: BroadcastJobStatus | str) -> str:
    value = status.value if isinstance(status, BroadcastJobStatus) else str(status)
    return {
        'draft': '📝',
        'scheduled': '🕓',
        'pending': '🕓',
        'running': '🚀',
        'completed': '✅',
        'failed': '❌',
        'cancelled': '⛔',
    }.get(value, '•')


def _broadcast_status_label(status: BroadcastJobStatus | str) -> str:
    value = status.value if isinstance(status, BroadcastJobStatus) else str(status)
    return {
        'draft': 'Черновик',
        'scheduled': 'Запланирована',
        'pending': 'Запланирована',
        'running': 'Выполняется',
        'completed': 'Завершена',
        'failed': 'Ошибка',
        'cancelled': 'Отменена',
    }.get(value, value or 'Неизвестно')


def admin_broadcast_keyboard(jobs=None) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text='✍️ Создать рассылку', callback_data=AdminCallback(section='broadcast', action='compose').pack())],
        [InlineKeyboardButton(text='📋 Список рассылок', callback_data=AdminCallback(section='broadcast', action='refresh').pack())],
    ]
    for job in jobs or []:
        rows.append([
            InlineKeyboardButton(
                text=f"{_broadcast_status_emoji(job.status)} #{job.id} | {_broadcast_status_label(getattr(job, 'status', ''))} | {getattr(job, 'run_at', None).strftime('%d.%m %H:%M') if getattr(job, 'run_at', None) else '—'}",
                callback_data=AdminCallback(section='broadcast', action='open', item_id=job.id).pack(),
            )
        ])
    rows.append([InlineKeyboardButton(text='⬅️ Назад', callback_data=AdminCallback(section='root').pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_broadcast_schedule_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='Сейчас', callback_data=AdminCallback(section='broadcast', action='pick_time', field='now').pack())],
            [InlineKeyboardButton(text='Через 10 минут', callback_data=AdminCallback(section='broadcast', action='pick_time', field='10m').pack())],
            [InlineKeyboardButton(text='Через 1 час', callback_data=AdminCallback(section='broadcast', action='pick_time', field='1h').pack())],
            [InlineKeyboardButton(text='Свое время (ДД.ММ ЧЧ:ММ)', callback_data=AdminCallback(section='broadcast', action='pick_time', field='custom').pack())],
            [InlineKeyboardButton(text='Отмена', callback_data=AdminCallback(section='broadcast', action='refresh').pack())],
        ]
    )


def admin_broadcast_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='Да', callback_data=AdminCallback(section='broadcast', action='confirm').pack())],
            [InlineKeyboardButton(text='Отмена', callback_data=AdminCallback(section='broadcast', action='refresh').pack())],
        ]
    )


def admin_broadcast_job_keyboard(job) -> InlineKeyboardMarkup:
    rows = []
    status = getattr(getattr(job, 'status', None), 'value', getattr(job, 'status', None))
    status = str(status or '').strip().lower()
    if status == 'pending':
        status = BroadcastJobStatus.scheduled.value

    editable_statuses = {
        BroadcastJobStatus.draft.value,
        BroadcastJobStatus.scheduled.value,
    }
    terminal_statuses = {
        BroadcastJobStatus.completed.value,
        BroadcastJobStatus.failed.value,
        BroadcastJobStatus.cancelled.value,
    }

    if status in editable_statuses:
        rows.append([InlineKeyboardButton(text='✏️ Редактировать текст', callback_data=AdminCallback(section='broadcast', action='edit_text', item_id=job.id).pack())])
        rows.append([InlineKeyboardButton(text='🕒 Изменить время', callback_data=AdminCallback(section='broadcast', action='edit_time', item_id=job.id).pack())])
    if status in editable_statuses | terminal_statuses:
        rows.append([InlineKeyboardButton(text='🗑 Удалить', callback_data=AdminCallback(section='broadcast', action='delete', item_id=job.id).pack())])
    rows.append([InlineKeyboardButton(text='⏪ Назад', callback_data=AdminCallback(section='broadcast', action='refresh').pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)
