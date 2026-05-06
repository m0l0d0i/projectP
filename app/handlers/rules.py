from __future__ import annotations

import ipaddress
from pathlib import Path
from urllib.parse import urlparse

from aiogram import F, Router

from app.config import Settings
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import AppSettingsRepository


def _effective_optional_url_from_row(row, attr_name: str, fallback: str | None) -> str | None:
    if row is None:
        return fallback
    return getattr(row, attr_name, None)
from app.keyboards.inline import RulesCallback, rules_keyboard

router = Router(name='rules')
RULES_DIR = Path(__file__).resolve().parents[1] / 'data' / 'rules'
RULE_URLS = {
    'rules': 'rules_service_url',
    'rules_of_use': 'rules_of_use_url',
    'privacy_policy': 'rules_privacy_url',
}


def _normalize_public_rule_url(value: str | None) -> str | None:
    normalized = str(value or '').strip()
    if not normalized:
        return None

    parsed = urlparse(normalized)
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
        return None

    hostname = (parsed.hostname or '').strip().lower()
    if not hostname or hostname == 'localhost':
        return None

    if parsed.path.startswith('/admin/'):
        return None

    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        return normalized

    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return None

    return normalized


async def _load_rule_url(session: AsyncSession, settings: Settings, doc_key: str) -> str | None:
    url_attr = RULE_URLS.get(doc_key)
    if not url_attr:
        return None

    repo = AppSettingsRepository(session)
    settings_row = await repo.get()
    env_fallback = getattr(settings, url_attr, None)
    value = _effective_optional_url_from_row(settings_row, url_attr, env_fallback)
    return _normalize_public_rule_url(value)


@router.message(F.text == '📜 Правила сервиса')
async def rules_menu(message: Message) -> None:
    await message.answer('Выберите нужный документ:', reply_markup=rules_keyboard())


@router.callback_query(RulesCallback.filter())
async def send_rule_file(
    callback: CallbackQuery,
    callback_data: RulesCallback,
    session: AsyncSession,
    settings: Settings,
) -> None:
    url = await _load_rule_url(session, settings, callback_data.doc)
    if url:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text='📖 Открыть документ', url=url)]
            ]
        )
        await callback.message.answer(
            'Откройте документ по ссылке ниже. Он откроется прямо внутри Telegram.',
            reply_markup=keyboard,
        )
        await callback.answer()
        return

    path = RULES_DIR / f'{callback_data.doc}.txt'
    if path.exists():
        await callback.message.answer_document(FSInputFile(path))
        await callback.answer('Ссылка не настроена, отправляю локальный файл')
        return

    await callback.answer('Документ недоступен', show_alert=True)
