from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

TRIAL_BUTTON_TEXT = '🎁 Тест на 24 часа'
ADMIN_BUTTON_TEXT = '⚙️ Админка'

BASE_MENU_ROWS: list[list[str]] = [
    ['👑 Мой VPN', '👤 Мой профиль'],
    ['💳 Пополнить', '📞 Поддержка'],
    ['📜 Правила сервиса'],
]


def _buttons_row(*labels: str) -> list[KeyboardButton]:
    return [KeyboardButton(text=label) for label in labels]


def main_menu(show_trial: bool = False, show_admin: bool = False) -> ReplyKeyboardMarkup:
    keyboard: list[list[KeyboardButton]] = []

    if show_trial:
        keyboard.append(_buttons_row(TRIAL_BUTTON_TEXT))

    for row in BASE_MENU_ROWS:
        keyboard.append(_buttons_row(*row))

    if show_admin:
        keyboard.append(_buttons_row(ADMIN_BUTTON_TEXT))

    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder='Выберите действие',
    )