from __future__ import annotations

import gettext as _gettext

from app.middlewares.i18n import DOMAIN, LOCALE_DIR, SUPPORTED_LOCALES


def gettext_lazy(message: str) -> str:
    return message


def all_translations(msgid: str) -> set[str]:
    results: set[str] = {msgid}
    for lang in SUPPORTED_LOCALES:
        try:
            translation = _gettext.translation(
                DOMAIN, localedir=str(LOCALE_DIR), languages=[lang]
            )
        except FileNotFoundError:
            continue
        results.add(translation.gettext(msgid))
    return results
