from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import (
    AuditAction,
    AuditActorType,
    SupportSenderType,
    SupportTicketStatus,
    User,
)
from app.db.repositories import (
    AuditLogRepository,
    InvoiceRepository,
    SubscriptionRepository,
    SupportMessageRepository,
    SupportTicketRepository,
)
from app.services.marzban import MarzbanClient

logger = logging.getLogger(__name__)

# Реальные TG ID положительные; сдвиг -10^12 - user.id гарантирует
# отрицательное значение и уникальность (user.id PK уникален).
_ANONYMIZED_TG_OFFSET = -(10 ** 12)


def anonymized_tg_id(user_id: int) -> int:
    return _ANONYMIZED_TG_OFFSET - int(user_id)


def is_anonymized_tg_id(tg_id: int | None) -> bool:
    return tg_id is not None and int(tg_id) <= _ANONYMIZED_TG_OFFSET


class PrivacyService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        marzban: MarzbanClient | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.marzban = marzban

    async def export_user_data(self, user: User) -> dict[str, Any]:
        sub_repo = SubscriptionRepository(self.session)
        invoice_repo = InvoiceRepository(self.session)
        ticket_repo = SupportTicketRepository(self.session)
        msg_repo = SupportMessageRepository(self.session)

        subscriptions = await sub_repo.list_by_user_id(user.id)
        invoices = await invoice_repo.list_by_user_id(user.id, limit=10000)
        tickets = await ticket_repo.list_by_user(user.id)

        ticket_data: list[dict[str, Any]] = []
        for t in tickets:
            messages = await msg_repo.list_by_ticket(t.id)
            ticket_data.append({
                'id': t.id,
                'status': _enum_value(t.status),
                'created_at': _iso(t.created_at),
                'closed_at': _iso(getattr(t, 'closed_at', None)),
                'close_reason': getattr(t, 'close_reason', None),
                'tags': list(getattr(t, 'tags', None) or []),
                'messages': [
                    {
                        'sender': _enum_value(m.sender_type),
                        'text': m.text,
                        'media_type': m.media_type,
                        'media_file_name': m.media_file_name,
                        'created_at': _iso(m.created_at),
                    }
                    for m in messages
                ],
            })

        return {
            'export_generated_at': datetime.now(timezone.utc).isoformat(),
            'user': {
                'id': user.id,
                'tg_id': user.tg_id,
                'username': user.username,
                'first_name': user.first_name,
                'last_name': user.last_name,
                'balance': str(user.balance),
                'referral_code': user.referral_code,
                'created_at': _iso(user.created_at),
                'trial_issued_at': _iso(user.trial_issued_at),
                'first_paid_at': _iso(user.first_paid_at),
                'tags': list(user.tags or []),
                'is_blocked': user.is_blocked,
                'anonymized_at': _iso(user.anonymized_at),
            },
            'subscriptions': [
                {
                    'id': s.id,
                    'service_id': s.service_id,
                    'marzban_username': s.marzban_username,
                    'created_at': _iso(s.created_at),
                    'expire_date': _iso(s.expire_date),
                    'is_trial': s.is_trial,
                    'used_traffic_bytes': s.used_traffic_bytes,
                    'monthly_traffic_bytes': s.monthly_traffic_bytes,
                    'current_tariff_code': s.current_tariff_code,
                    'used_device_count': s.used_device_count,
                    'used_device_mode': s.used_device_mode,
                    'is_active': s.is_active,
                }
                for s in subscriptions
            ],
            'invoices': [
                {
                    'id': i.id,
                    'purpose': _enum_value(i.purpose),
                    'amount': str(i.amount),
                    'balance_used': str(i.balance_used),
                    'payable_amount': str(i.payable_amount),
                    'status': _enum_value(i.status),
                    'provider': i.provider,
                    'created_at': _iso(i.created_at),
                    'paid_at': _iso(i.paid_at),
                }
                for i in invoices
            ],
            'support_tickets': ticket_data,
        }

    async def erase_user(
        self,
        user: User,
        *,
        actor_tg_id: int | None,
        actor_username: str | None = None,
        actor_type: AuditActorType = AuditActorType.user,
    ) -> dict[str, Any]:
        if user.anonymized_at is not None:
            return {'already_anonymized': True}

        sub_repo = SubscriptionRepository(self.session)
        ticket_repo = SupportTicketRepository(self.session)
        msg_repo = SupportMessageRepository(self.session)
        audit_repo = AuditLogRepository(self.session)

        subs = await sub_repo.list_by_user_id(user.id)

        marzban_usernames_disabled: list[str] = []
        if self.settings.marzban_enabled and self.marzban is not None:
            for sub in subs:
                if not sub.marzban_username or not sub.is_active:
                    continue
                try:
                    await self.marzban.safe_modify_user(sub.marzban_username, status='disabled')
                    marzban_usernames_disabled.append(sub.marzban_username)
                except Exception:
                    logger.exception(
                        'Failed to disable Marzban user during erase: user_id=%s sub_id=%s mz=%s',
                        user.id, sub.id, sub.marzban_username,
                    )

        for sub in subs:
            if sub.is_active:
                sub.is_active = False

        tickets = await ticket_repo.list_by_user(user.id)
        ticket_ids_closed: list[int] = []
        for t in tickets:
            if t.status != SupportTicketStatus.closed:
                closed = await ticket_repo.close(
                    t,
                    'gdpr_erased',
                    actor_tg_id=actor_tg_id,
                    actor_type=SupportSenderType.user if actor_type == AuditActorType.user else SupportSenderType.admin,
                )
                if closed:
                    ticket_ids_closed.append(t.id)
            messages = await msg_repo.list_by_ticket(t.id)
            for m in messages:
                m.text = None
                m.media_file_id = None
                m.media_file_unique_id = None
                m.media_file_name = None
                m.media_mime_type = None
                m.media_size_bytes = None

        # Anonymize PII
        user.username = None
        user.first_name = None
        user.last_name = None
        user.admin_notes = None
        user.tags = []
        user.balance = Decimal('0.00')
        user.is_blocked = True
        user.blocked_reason = 'gdpr_erased'
        user.blocked_at = datetime.now(timezone.utc)
        user.tg_id = anonymized_tg_id(user.id)
        user.anonymized_at = datetime.now(timezone.utc)

        await self.session.flush()

        details = {
            'subscriptions_disabled': [s.id for s in subs],
            'marzban_users_disabled': marzban_usernames_disabled,
            'tickets_closed': ticket_ids_closed,
            'tickets_total': [t.id for t in tickets],
        }
        await audit_repo.create(
            action=AuditAction.user_erased,
            actor_type=actor_type,
            actor_tg_id=actor_tg_id,
            actor_username=actor_username,
            entity_type='user',
            entity_id=str(user.id),
            details=details,
        )
        return details


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _enum_value(value: Any) -> Any:
    if hasattr(value, 'value'):
        return value.value
    return value
