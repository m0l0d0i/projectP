from __future__ import annotations

import enum
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.sql import Select

from app.db.models import Invoice, InvoiceStatus, Subscription, User


class BroadcastSegment(str, enum.Enum):
    all = 'all'
    trial_active = 'trial_active'
    expired_7d = 'expired_7d'
    high_ltv = 'high_ltv'
    no_purchase_after_trial = 'no_purchase_after_trial'


SEGMENT_LABELS: dict[str, str] = {
    BroadcastSegment.all.value: 'Все (не заблокированные, бот не закрыт)',
    BroadcastSegment.trial_active.value: 'Trial-активные (живая trial-подписка)',
    BroadcastSegment.expired_7d.value: 'Истекли за последние 7 дней',
    BroadcastSegment.high_ltv.value: 'High-LTV (оплачено ≥ 1000 ₽ суммарно)',
    BroadcastSegment.no_purchase_after_trial.value: 'Trial без покупки',
}


HIGH_LTV_THRESHOLD = Decimal('1000.00')
EXPIRED_RECENT_DAYS = 7


def normalize_segment(value: str | None) -> str:
    if not value:
        return BroadcastSegment.all.value
    normalized = value.strip().lower()
    try:
        return BroadcastSegment(normalized).value
    except ValueError:
        return BroadcastSegment.all.value


def apply_segment_filter(stmt: Select, segment: str | None) -> Select:
    """Добавляет к стейтменту WHERE-условия для сегмента.

    Базовый фильтр (общий для всех сегментов): не заблокирован,
    бот не закрыт, не анонимизирован. Дальнейшие условия — по `segment`.
    """
    code = normalize_segment(segment)
    now = datetime.now(timezone.utc)

    stmt = stmt.where(
        User.bot_blocked.is_(False),
        User.is_blocked.is_(False),
        User.anonymized_at.is_(None),
    )

    if code == BroadcastSegment.all.value:
        return stmt

    if code == BroadcastSegment.trial_active.value:
        sub_exists = exists().where(
            and_(
                Subscription.user_id == User.id,
                Subscription.is_trial.is_(True),
                Subscription.is_active.is_(True),
                or_(Subscription.expire_date.is_(None), Subscription.expire_date > now),
            )
        )
        return stmt.where(sub_exists)

    if code == BroadcastSegment.expired_7d.value:
        cutoff = now - timedelta(days=EXPIRED_RECENT_DAYS)
        # Подписка expired_at в окне (cutoff, now). Используем expire_date < now AND >= cutoff
        sub_exists = exists().where(
            and_(
                Subscription.user_id == User.id,
                Subscription.expire_date.is_not(None),
                Subscription.expire_date <= now,
                Subscription.expire_date >= cutoff,
            )
        )
        return stmt.where(sub_exists)

    if code == BroadcastSegment.high_ltv.value:
        ltv_subq = (
            select(func.coalesce(func.sum(Invoice.amount), 0))
            .where(
                Invoice.user_id == User.id,
                Invoice.status.in_((InvoiceStatus.paid, InvoiceStatus.consumed, InvoiceStatus.applying)),
            )
            .correlate(User)
            .scalar_subquery()
        )
        return stmt.where(ltv_subq >= HIGH_LTV_THRESHOLD)

    if code == BroadcastSegment.no_purchase_after_trial.value:
        return stmt.where(
            User.trial_issued_at.is_not(None),
            User.first_paid_at.is_(None),
        )

    return stmt
