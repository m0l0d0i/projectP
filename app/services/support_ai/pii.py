"""PII-маскирование перед отправкой данных тикета в внешний LLM (FEA-C32).

LLM-провайдеры могут логировать запросы у себя. Чтобы не утекали телефоны,
email, telegram username и платёжные данные — заменяем их на placeholder'ы
типа `[email]`, `[phone]`, `[tg_id]`. Контекст тикета остаётся читаемым,
а персональные данные не покидают периметр.

Это base-line; для production добавьте дополнительные паттерны под ваш
домен (например, маркеры платежей конкретных провайдеров) и обновите
unit-тесты.
"""

from __future__ import annotations

import re
from typing import Final

# Email — простой conservative-паттерн (не RFC-полный, но покрывает
# 99% случаев в саппорт-чатах).
_EMAIL_RE: Final = re.compile(
    r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'
)
# Телефоны: международный/российский формат, ≥ 10 цифр с возможными
# разделителями (пробел/дефис/скобки/плюс).
_PHONE_RE: Final = re.compile(
    r'(?<!\d)(?:\+?\d[\s\-()]*){10,15}(?!\d)'
)
# Telegram tg_id (consecutive 7–12 digits — типичный диапазон), а также
# @username длиной 5–32 символа.
_TG_ID_RE: Final = re.compile(r'(?<!\d)\d{7,12}(?!\d)')
_TG_USERNAME_RE: Final = re.compile(r'@[A-Za-z0-9_]{5,32}\b')
# Длинные «карточные» цифровые блоки (12–19 цифр подряд с возможным
# разделителем) — выглядит как PAN; маскируем целиком.
_CARD_RE: Final = re.compile(r'(?<!\d)(?:\d[\s\-]?){12,19}(?!\d)')


def mask_pii(text: str) -> str:
    """Заменить PII-токены на placeholder'ы.

    Порядок применения: card → email → phone → tg_username → tg_id.
    Card идёт первым, чтобы не быть проглоченным phone-паттерном (у
    карты ≥ 12 цифр, у телефона ≥ 10 — пересечение возможно). Email и
    username — до tg_id, чтобы не съедать цифры в локальной части.
    """
    if not text:
        return text

    masked = _CARD_RE.sub('[card]', text)
    masked = _EMAIL_RE.sub('[email]', masked)
    masked = _PHONE_RE.sub('[phone]', masked)
    masked = _TG_USERNAME_RE.sub('[tg_user]', masked)
    masked = _TG_ID_RE.sub('[tg_id]', masked)
    return masked
