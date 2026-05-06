from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select

from app.db.models import AuditAction, AuditActorType, Referral, ReferralSource, TransactionType
from app.db.repositories import AuditLogRepository, ReferralRepository, TransactionRepository, UserRepository

REF_BONUS = Decimal('50.00')


class ReferralService:
    def __init__(self, session) -> None:
        self.session = session
        self.users = UserRepository(session)
        self.referrals = ReferralRepository(session)
        self.transactions = TransactionRepository(session)
        self.audit = AuditLogRepository(session)

    async def _grant_bonus(self, inviter_id: int, invited_id: int, source: ReferralSource) -> bool:
        inviter = await self.users.get_by_id_for_update(inviter_id)
        invited = await self.users.get_by_id_for_update(invited_id)
        referral = await self.referrals.get_by_invited_id_for_update(invited_id)
        if not inviter or not invited or not referral or referral.is_activated:
            return False

        await self.users.add_balance(inviter, REF_BONUS)
        await self.users.add_balance(invited, REF_BONUS)
        await self.transactions.create(
            inviter.id,
            REF_BONUS,
            TransactionType.income,
            f'Рефералка: бонус за приглашенного #{invited.tg_id}',
        )
        description = 'Рефералка: бонус по ссылке' if source == ReferralSource.link else 'Рефералка: бонус за ввод промокода'
        await self.transactions.create(invited.id, REF_BONUS, TransactionType.income, description)

        referral.is_activated = True
        referral.activated_at = datetime.now(timezone.utc)

        await self.audit.create(
            action=AuditAction.referral_activated,
            actor_type=AuditActorType.system,
            actor_tg_id=None,
            entity_type='referral',
            entity_id=str(referral.id),
            details={
                'source': referral.source.value,
                'inviter_id': inviter.id,
                'invited_id': invited.id,
                'bonus_amount': str(REF_BONUS),
            },
        )
        await self.session.flush()
        return True

    async def bind_inviter_by_link(self, invited_tg_id: int, inviter_tg_id: int) -> tuple[bool, str | None]:
        invited = await self.users.get_by_tg_id_for_update(invited_tg_id)
        inviter = await self.users.get_by_tg_id_for_update(inviter_tg_id)

        if not invited:
            return False, 'Пользователь не найден.'
        if not inviter:
            return False, 'Реферер не найден.'
        if invited.id == inviter.id:
            return False, 'Нельзя использовать собственную реферальную ссылку.'

        existing = await self.referrals.get_by_invited_id_for_update(invited.id)
        if existing:
            return False, 'Пользователь уже привязан к рефереру.'

        referral = await self.referrals.create_if_not_exists(inviter.id, invited.id, ReferralSource.link)
        if not referral:
            return False, 'Не удалось создать рефералку.'
        return True, None

    async def activate_if_first_paid(self, invited_user_id: int) -> bool:
        referral = await self.referrals.get_by_invited_id_for_update(invited_user_id)
        if not referral or referral.is_activated:
            return False
        return await self._grant_bonus(referral.inviter_id, referral.invited_id, referral.source)

    async def can_use_referral_code(self, invited_tg_id: int) -> bool:
        invited = await self.users.get_by_tg_id(invited_tg_id)
        if not invited:
            return False
        return not await self.referrals.exists_for_invited(invited.id)

    async def redeem_referral_code(self, invited_tg_id: int, inviter_code: str) -> tuple[bool, str]:
        invited = await self.users.get_by_tg_id_for_update(invited_tg_id)
        if not invited:
            return False, 'Пользователь не найден'

        if await self.referrals.exists_for_invited(invited.id):
            return False, 'Вы уже были приглашены по ссылке или уже использовали промокод реферала.'

        code = (inviter_code or '').strip().lower()
        if not code:
            return False, 'Промокод не указан.'

        inviter = await self.users.get_by_referral_code(code)
        if not inviter:
            return False, 'Промокод не найден или неактивен.'
        if inviter.id == invited.id:
            return False, 'Нельзя активировать свой промокод.'

        referral = await self.referrals.create_if_not_exists(inviter.id, invited.id, ReferralSource.code)
        if not referral:
            return False, 'Не удалось создать реферальную связь.'

        return True, '✅ Промокод применён. Бонус будет начислен после вашей первой оплаты.'

    async def _activated_count_for_inviter(self, inviter_user_id: int) -> int:
        result = await self.session.execute(
            select(func.count(Referral.id)).where(
                Referral.inviter_id == inviter_user_id,
                Referral.is_activated.is_(True),
            )
        )
        return int(result.scalar_one())

    async def stats_for_inviter(self, inviter_user_id: int) -> tuple[int, Decimal]:
        invited_count = await self.referrals.count_for_inviter(inviter_user_id)
        activated_count = await self._activated_count_for_inviter(inviter_user_id)
        referral_balance = (REF_BONUS * activated_count).quantize(Decimal('0.01'))
        return invited_count, referral_balance
