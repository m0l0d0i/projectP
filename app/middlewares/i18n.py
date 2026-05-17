from __future__ import annotations

import gettext
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

logger = logging.getLogger(__name__)

DEFAULT_LOCALE = 'ru'
SUPPORTED_LOCALES: tuple[str, ...] = ('ru', 'en')
DOMAIN = 'messages'

LOCALE_DIR = Path(__file__).resolve().parents[2] / 'locale'


def resolve_locale(language_code: str | None) -> str:
    if not language_code:
        return DEFAULT_LOCALE
    primary = language_code.lower().split('-', 1)[0]
    if primary == 'en':
        return 'en'
    return DEFAULT_LOCALE


class I18nMiddleware(BaseMiddleware):
    def __init__(self, locale_dir: Path | None = None, domain: str = DOMAIN) -> None:
        self.locale_dir = locale_dir or LOCALE_DIR
        self.domain = domain
        self._translations: dict[str, gettext.NullTranslations] = {}
        for lang in SUPPORTED_LOCALES:
            try:
                self._translations[lang] = gettext.translation(
                    domain, localedir=str(self.locale_dir), languages=[lang]
                )
            except FileNotFoundError:
                logger.warning(
                    'i18n: .mo for locale=%s not found in %s — using source strings as fallback',
                    lang,
                    self.locale_dir,
                )
                self._translations[lang] = gettext.NullTranslations()

    def _user_language(self, event: TelegramObject) -> str:
        user = getattr(event, 'from_user', None)
        code = getattr(user, 'language_code', None) if user else None
        return resolve_locale(code)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        lang = self._user_language(event)
        translation = self._translations.get(lang) or self._translations[DEFAULT_LOCALE]
        data['_'] = translation.gettext
        data['ngettext'] = translation.ngettext
        data['locale'] = lang
        return await handler(event, data)
