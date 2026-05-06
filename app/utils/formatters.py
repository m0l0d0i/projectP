from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

DISPLAY_TIMEZONE = ZoneInfo('Europe/Moscow')
DISPLAY_TIMEZONE_LABEL = 'МСК'


def _ensure_aware_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_display_timezone(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return _ensure_aware_utc(dt).astimezone(DISPLAY_TIMEZONE)


def format_dt(dt: datetime | None) -> str:
    localized = to_display_timezone(dt)
    if localized is None:
        return '—'
    return localized.strftime(f'%Y-%m-%d %H:%M:%S {DISPLAY_TIMEZONE_LABEL}')


def bytes_to_gb(value: int | None) -> str:
    if value is None or value == 0:
        return 'Безлимит'
    gb = value / (1024 ** 3)
    return f'{gb:.0f} ГБ'


def traffic_state(data_limit: int | None, used: int, status: str) -> str:
    if status in {'expired', 'disabled'}:
        return '🔴 Истёк'
    if data_limit and data_limit > 0:
        ratio = used / data_limit
        if ratio >= 0.9:
            return '⚠️ Мало трафика'
    return '🟢 Активный'