"""Резолвер видимости тарифов для пользователя (FEA-ADMIN-TARIFF-PLUS).

Берёт `User` (с применёнными `tags`, `unlocked_tariff_ids`,
`first_paid_at`) и `TariffPlan` (с `visibility`, окнами,
`segment_filter_json`) и решает, имеет ли пользователь право увидеть
конкретный тариф в боте.

DSL `segment_filter_json` поддерживает базовые ключи (расширение —
аддитивно, чтобы не ломать данные):

* `min_paid_invoices` — целое; пользователь должен иметь ≥ N оплаченных
  invoices.
* `paid_only` — bool; пользователь должен иметь `first_paid_at IS NOT NULL`.
* `created_before` — ISO-дата; user.created_at должен быть до этой даты.
* `created_after` — ISO-дата; user.created_at должен быть после.
* `tags_any` — list[str]; должен совпадать хотя бы один тег.
* `tags_all` — list[str]; должны совпасть все.

Несовпадение DSL → тариф невидим. Неподдерживаемый ключ → тариф
невидим (fail-safe — лучше скрыть лишний тариф, чем показать
пользователю не его).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Invoice,
    InvoiceStatus,
    Subscription,
    TariffPlan,
    TariffVisibility,
    User,
)

logger = logging.getLogger(__name__)


_SUPPORTED_SEGMENT_KEYS: frozenset[str] = frozenset(
    {
        'min_paid_invoices',
        'paid_only',
        'created_before',
        'created_after',
        'tags_any',
        'tags_all',
    }
)


@dataclass(frozen=True, slots=True)
class TariffVisibilityContext:
    """Срез данных пользователя для резолва видимости.

    Передаётся в `is_tariff_visible_for(...)`; собирается один раз через
    `build_user_context(session, user)`, чтобы избежать N+1 запросов на
    listing тарифов (1 invoice-count, 1 active-subs-count per tariff —
    делается отдельным проходом).
    """

    user_id: int
    tags: tuple[str, ...]
    unlocked_tariff_ids: frozenset[int]
    first_paid_at: datetime | None
    created_at: datetime
    paid_invoices_count: int


async def build_user_context(
    session: AsyncSession,
    user: User,
) -> TariffVisibilityContext:
    """Собрать `TariffVisibilityContext` для одного пользователя.

    Делает один SQL для подсчёта оплаченных invoices (consumed/paid).
    """
    paid_invoices_count = int(
        (
            await session.execute(
                select(func.count(Invoice.id)).where(
                    Invoice.user_id == user.id,
                    Invoice.status.in_(
                        (InvoiceStatus.paid, InvoiceStatus.consumed)
                    ),
                )
            )
        ).scalar_one()
        or 0
    )
    return TariffVisibilityContext(
        user_id=user.id,
        tags=tuple(user.tags or []),
        unlocked_tariff_ids=frozenset(int(x) for x in (user.unlocked_tariff_ids or [])),
        first_paid_at=user.first_paid_at,
        created_at=user.created_at,
        paid_invoices_count=paid_invoices_count,
    )


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _evaluate_segment_filter(
    segment: dict[str, Any] | None,
    ctx: TariffVisibilityContext,
) -> bool:
    """Проверка `segment_filter_json` против контекста пользователя.

    None / пустой dict → True (нет ограничений). Неподдерживаемый ключ →
    False (fail-safe). Каждое условие — AND (логика «все условия должны
    выполняться»); для OR оператор пока не реализован — расширяем по
    мере необходимости.
    """
    if not segment:
        return True

    for key, raw_value in segment.items():
        if key not in _SUPPORTED_SEGMENT_KEYS:
            logger.warning('Unknown tariff segment_filter key: %r', key)
            return False

        if key == 'min_paid_invoices':
            try:
                threshold = int(raw_value)
            except (TypeError, ValueError):
                return False
            if ctx.paid_invoices_count < threshold:
                return False

        elif key == 'paid_only':
            if bool(raw_value) and ctx.first_paid_at is None:
                return False

        elif key == 'created_before':
            cutoff = _parse_iso_datetime(raw_value)
            if cutoff is None or ctx.created_at >= cutoff:
                return False

        elif key == 'created_after':
            cutoff = _parse_iso_datetime(raw_value)
            if cutoff is None or ctx.created_at <= cutoff:
                return False

        elif key == 'tags_any':
            if not isinstance(raw_value, (list, tuple)):
                return False
            wanted = {str(t).strip().lower() for t in raw_value if str(t).strip()}
            user_tags = {t.lower() for t in ctx.tags}
            if not wanted or not (wanted & user_tags):
                return False

        elif key == 'tags_all':
            if not isinstance(raw_value, (list, tuple)):
                return False
            wanted = {str(t).strip().lower() for t in raw_value if str(t).strip()}
            user_tags = {t.lower() for t in ctx.tags}
            if not wanted.issubset(user_tags):
                return False

    return True


def _is_within_window(now: datetime, available_from: datetime | None, available_to: datetime | None) -> bool:
    if available_from is not None and now < available_from:
        return False
    if available_to is not None and now > available_to:
        return False
    return True


def is_tariff_visible_for(
    tariff: TariffPlan,
    ctx: TariffVisibilityContext | None,
    *,
    now: datetime | None = None,
    inventory_remaining: int | None = None,
) -> bool:
    """Определяет, должен ли тариф быть виден пользователю.

    Если `ctx is None` — пользователь не аутентифицирован/анон
    (например, расчёт превью для не-loggedin контекста); видны только
    `public` тарифы вне любых окон. Иначе — полная цепочка проверок.

    `inventory_remaining` (опционально): сколько активных подписок ещё
    можно создать на этом тарифе (если `max_active_subscriptions` задан).
    Если 0 → тариф невидим. None означает «не считали» — пропускаем
    inventory-check (вызывающий код, например, /admin/, не нуждается).
    """
    if not getattr(tariff, 'is_active', False) or getattr(tariff, 'is_archived', False):
        return False

    current_time = now or datetime.now(timezone.utc)
    if not _is_within_window(current_time, tariff.available_from, tariff.available_to):
        return False

    if inventory_remaining is not None and tariff.max_active_subscriptions is not None:
        if inventory_remaining <= 0:
            return False

    visibility = tariff.visibility
    if visibility is TariffVisibility.public:
        return True

    if ctx is None:
        return False

    if tariff.id in ctx.unlocked_tariff_ids:
        return True

    if visibility is TariffVisibility.code_only:
        # Без unlock через промокод — невидим.
        return False
    if visibility is TariffVisibility.private_link:
        # Без unlock через deep-link — невидим.
        return False
    if visibility is TariffVisibility.segment_only:
        return _evaluate_segment_filter(tariff.segment_filter_json, ctx)

    return False


async def filter_visible_tariffs(
    session: AsyncSession,
    user: User | None,
    tariffs: Iterable[TariffPlan],
) -> list[TariffPlan]:
    """Helper для bot-listing: применяет `is_tariff_visible_for` к
    списку тарифов с предварительной сборкой контекста и подсчётом
    inventory-remaining (одним запросом для всех с `max_active_subscriptions`).
    """
    items = list(tariffs)
    if not items:
        return []

    ctx = await build_user_context(session, user) if user is not None else None

    capped_ids = [t.id for t in items if t.max_active_subscriptions is not None]
    inventory_map: dict[int, int] = {}
    if capped_ids:
        rows = await session.execute(
            select(Subscription.current_tariff_id, func.count(Subscription.id))
            .where(
                Subscription.current_tariff_id.in_(capped_ids),
                Subscription.is_active.is_(True),
            )
            .group_by(Subscription.current_tariff_id)
        )
        used: dict[int, int] = {tariff_id: int(c) for tariff_id, c in rows.all()}
        for t in items:
            if t.max_active_subscriptions is not None:
                inventory_map[t.id] = max(0, int(t.max_active_subscriptions) - used.get(t.id, 0))

    now = datetime.now(timezone.utc)
    return [
        t
        for t in items
        if is_tariff_visible_for(t, ctx, now=now, inventory_remaining=inventory_map.get(t.id))
    ]


def parse_segment_filter_text(text: str | None) -> dict[str, Any] | None:
    """Парсит JSON-текст `segment_filter_json` из admin-формы.

    Пустой/whitespace → None (нет фильтра). Невалидный JSON или
    неподдерживаемые ключи → ValueError (для UI).
    """
    if not text or not text.strip():
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f'segment_filter_json: невалидный JSON ({exc})') from exc
    if not isinstance(parsed, dict):
        raise ValueError('segment_filter_json: ожидается JSON-объект')
    unknown = set(parsed.keys()) - _SUPPORTED_SEGMENT_KEYS
    if unknown:
        raise ValueError(
            'segment_filter_json: неподдерживаемые ключи '
            f'{sorted(unknown)}; разрешены: {sorted(_SUPPORTED_SEGMENT_KEYS)}'
        )
    return parsed
