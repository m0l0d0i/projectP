from __future__ import annotations

from collections.abc import Callable

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from app.i18n import gettext_lazy as _l

TRIAL_BUTTON_TEXT = _l('🎁 Тест на 24 часа')
ADMIN_BUTTON_TEXT = _l('⚙️ Админка')

BASE_MENU_LABELS: list[list[str]] = [
    [_l('👑 Мой VPN'), _l('👤 Мой профиль')],
    [_l('💳 Пополнить'), _l('📞 Поддержка')],
    [_l('📜 Правила сервиса')],
]

BASE_MENU_ROWS: list[list[str]] = BASE_MENU_LABELS


def _buttons_row(*labels: str) -> list[KeyboardButton]:
    return [KeyboardButton(text=label) for label in labels]


def main_menu(
    show_trial: bool = False,
    show_admin: bool = False,
    *,
    translator: Callable[[str], str] | None = None,
) -> ReplyKeyboardMarkup:
    tr = translator or (lambda s: s)
    keyboard: list[list[KeyboardButton]] = []

    if show_trial:
        keyboard.append(_buttons_row(tr(TRIAL_BUTTON_TEXT)))

    for row in BASE_MENU_LABELS:
        keyboard.append(_buttons_row(*(tr(label) for label in row)))

    if show_admin:
        keyboard.append(_buttons_row(tr(ADMIN_BUTTON_TEXT)))

    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder=tr(_l('Выберите действие')),
    )
