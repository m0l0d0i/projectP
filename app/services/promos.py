from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditAction, AuditActorType, PromoCode, TransactionType
from app.db.repositories import (
    AuditLogRepository,
    PromoRedemptionRepository,
    PromoRepository,
    TransactionRepository,
    UserRepository,
)


class PromoService:
    _CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    _CODE_LENGTH = 10

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.promos = PromoRepository(session)
        self.redemptions = PromoRedemptionRepository(session)
        self.users = UserRepository(session)
        self.transactions = TransactionRepository(session)
        self.audit = AuditLogRepository(session)

    @classmethod
    def generate_code(cls, length: int = _CODE_LENGTH) -> str:
        if length < 4:
            raise ValueError('Длина кода промокода должна быть не меньше 4 символов.')
        return ''.join(secrets.choice(cls._CODE_ALPHABET) for _ in range(length))

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _normalize_code(code: str | None) -> str:
        return (code or '').strip().upper()

    @staticmethod
    def _normalize_bonus_amount(value: Decimal | int | float | str) -> Decimal:
        try:
            amount = Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError('Некорректная бонусная сумма промокода.') from exc
        if amount <= 0:
            raise ValueError('Бонус промокода должен быть больше 0.')
        if amount > Decimal('1000000.00'):
            raise ValueError('Бонус промокода слишком большой.')
        return amount

    @staticmethod
    def _normalize_max_uses(value: int | str | None) -> int | None:
        if value in (None, '', 0, '0'):
            return None
        try:
            normalized = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError('Максимум использований должен быть целым числом.') from exc
        if normalized < 1:
            raise ValueError('Максимум использований должен быть не меньше 1.')
        if normalized > 1_000_000:
            raise ValueError('Максимум использований слишком большой.')
        return normalized

    @classmethod
    def _normalize_duration_minutes(cls, value: int | str | None) -> int | None:
        if value in (None, '', 0, '0'):
            return None
        try:
            minutes = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError('Срок действия в минутах должен быть целым числом.') from exc
        if minutes < 1:
            raise ValueError('Срок действия в минутах должен быть не меньше 1.')
        if minutes > 60 * 24 * 365 * 5:
            raise ValueError('Срок действия промокода слишком большой.')
        return minutes

    @classmethod
    def _normalize_expires_at(
        cls,
        *,
        expires_at: datetime | None,
        duration_minutes: int | str | None,
    ) -> datetime | None:
        normalized_duration = cls._normalize_duration_minutes(duration_minutes)
        if expires_at is not None and normalized_duration is not None:
            raise ValueError('Нельзя одновременно задавать expires_at и duration_minutes.')

        if expires_at is not None:
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            else:
                expires_at = expires_at.astimezone(timezone.utc)
            if expires_at <= cls._now():
                raise ValueError('Дата истечения должна быть в будущем.')
            return expires_at

        if normalized_duration is None:
            return None
        return cls._now() + timedelta(minutes=normalized_duration)

    @classmethod
    def _promo_is_expired(cls, promo: PromoCode) -> bool:
        return promo.expires_at is not None and promo.expires_at <= cls._now()

    @classmethod
    def _promo_is_exhausted(cls, promo: PromoCode) -> bool:
        return promo.max_uses is not None and promo.used_count >= promo.max_uses

    @classmethod
    def resolve_admin_status(cls, promo: PromoCode) -> str:
        if not promo.is_active:
            return 'archived'
        if cls._promo_is_expired(promo):
            return 'expired'
        if cls._promo_is_exhausted(promo):
            return 'exhausted'
        return 'active'

    async def _get_by_id_for_update(self, promo_id: int) -> PromoCode | None:
        repo_method = getattr(self.promos, 'get_by_id_for_update', None)
        if callable(repo_method):
            return await repo_method(promo_id)

        result = await self.session.execute(
            select(PromoCode).where(PromoCode.id == promo_id).with_for_update()
        )
        return result.scalar_one_or_none()

    async def _ensure_unique_code(self, code: str, *, exclude_promo_id: int | None = None) -> None:
        existing = await self.promos.get_by_code(code)
        if existing is None:
            return
        if exclude_promo_id is not None and existing.id == exclude_promo_id:
            return
        raise ValueError('Промокод с таким кодом уже существует.')

    async def _create_with_unique_code(
        self,
        *,
        requested_code: str | None,
        bonus_amount: Decimal,
        max_uses: int | None,
        expires_at: datetime | None,
        is_active: bool,
        created_by_tg_id: int | None,
    ) -> PromoCode:
        if requested_code:
            await self._ensure_unique_code(requested_code)
            promo = await self.promos.create(
                code=requested_code,
                bonus_amount=bonus_amount,
                max_uses=max_uses,
                expires_at=expires_at,
                created_by_tg_id=created_by_tg_id,
            )
            if promo.is_active != is_active:
                await self.promos.set_active(promo, is_active)
            return promo

        for _ in range(25):
            generated = self.generate_code()
            if await self.promos.get_by_code(generated) is not None:
                continue
            promo = await self.promos.create(
                code=generated,
                bonus_amount=bonus_amount,
                max_uses=max_uses,
                expires_at=expires_at,
                created_by_tg_id=created_by_tg_id,
            )
            if promo.is_active != is_active:
                await self.promos.set_active(promo, is_active)
            return promo

        raise ValueError('Не удалось сгенерировать уникальный код промокода.')

    async def redeem(self, tg_user_id: int, code: str) -> tuple[bool, str]:
        normalized_code = self._normalize_code(code)
        if not normalized_code:
            return False, 'Промокод пустой.'

        user = await self.users.get_by_tg_id_for_update(tg_user_id)
        if not user:
            return False, 'Пользователь не найден.'

        promo = await self.promos.get_by_code_for_update(normalized_code)
        if not promo or not promo.is_active:
            return False, 'Промокод неактивен.'

        if self._promo_is_expired(promo):
            return False, 'Срок действия промокода истёк.'

        if self._promo_is_exhausted(promo):
            return False, 'Лимит использований промокода исчерпан.'

        if await self.redemptions.has_redeemed(promo.id, user.id):
            return False, 'Вы уже использовали этот промокод.'

        bonus_amount = self._normalize_bonus_amount(promo.bonus_amount)

        await self.redemptions.create(promo.id, user.id)
        promo.used_count += 1

        await self.users.add_balance(user, bonus_amount)
        await self.transactions.create(
            user.id,
            bonus_amount,
            TransactionType.income,
            f'Промокод: {promo.code}',
        )
        await self.audit.create(
            action=AuditAction.promo_redeemed,
            actor_type=AuditActorType.user,
            actor_tg_id=tg_user_id,
            entity_type='promo_code',
            entity_id=str(promo.id),
            details={
                'code': promo.code,
                'bonus_amount': str(bonus_amount),
                'user_id': user.id,
                'used_count': promo.used_count,
                'max_uses': promo.max_uses,
            },
        )

        # FEA-ADMIN-TARIFF-PLUS: если промокод привязан к тарифу
        # (`unlocks_tariff_id`) — добавляем тариф в `unlocked_tariff_ids`
        # пользователя, чтобы visibility-резолвер пускал к code_only/private.
        unlock_message = ''
        unlocks_tariff_id = getattr(promo, 'unlocks_tariff_id', None)
        if unlocks_tariff_id is not None:
            added = await self.users.add_unlocked_tariff(user, int(unlocks_tariff_id))
            if added:
                await self.audit.create(
                    action=AuditAction.tariff_unlock_granted,
                    actor_type=AuditActorType.user,
                    actor_tg_id=tg_user_id,
                    entity_type='tariff_plan',
                    entity_id=str(unlocks_tariff_id),
                    details={
                        'source': 'promo_code',
                        'promo_code': promo.code,
                        'user_id': user.id,
                    },
                )
                unlock_message = ' Также разблокирован специальный тариф.'

        await self.session.flush()
        return True, f'✅ Промокод применён. Баланс пополнен.{unlock_message}'

    async def create_promo(
        self,
        *,
        code: str | None,
        bonus_amount: Decimal | int | float | str,
        max_uses: int | str | None,
        duration_minutes: int | str | None = None,
        expires_at: datetime | None = None,
        is_active: bool = True,
        created_by_tg_id: int | None,
    ) -> PromoCode:
        final_code = self._normalize_code(code) or None
        normalized_bonus_amount = self._normalize_bonus_amount(bonus_amount)
        normalized_max_uses = self._normalize_max_uses(max_uses)
        normalized_expires_at = self._normalize_expires_at(
            expires_at=expires_at,
            duration_minutes=duration_minutes,
        )

        promo = await self._create_with_unique_code(
            requested_code=final_code,
            bonus_amount=normalized_bonus_amount,
            max_uses=normalized_max_uses,
            expires_at=normalized_expires_at,
            is_active=bool(is_active),
            created_by_tg_id=created_by_tg_id,
        )
        await self.session.flush()
        return promo

    async def update_promo(
        self,
        *,
        promo_id: int,
        code: str,
        bonus_amount: Decimal | int | float | str,
        max_uses: int | str | None,
        expires_at: datetime | None,
        is_active: bool,
    ) -> PromoCode:
        promo = await self._get_by_id_for_update(promo_id)
        if promo is None:
            raise LookupError('Промокод не найден.')

        normalized_code = self._normalize_code(code)
        if not normalized_code:
            raise ValueError('Код промокода не может быть пустым.')
        await self._ensure_unique_code(normalized_code, exclude_promo_id=promo.id)

        normalized_bonus_amount = self._normalize_bonus_amount(bonus_amount)
        normalized_max_uses = self._normalize_max_uses(max_uses)
        normalized_expires_at = self._normalize_expires_at(
            expires_at=expires_at,
            duration_minutes=None,
        )

        repo_update = getattr(self.promos, 'update', None)
        if callable(repo_update):
            promo = await repo_update(
                promo,
                code=normalized_code,
                bonus_amount=normalized_bonus_amount,
                max_uses=normalized_max_uses,
                expires_at=normalized_expires_at,
                is_active=bool(is_active),
            )
        else:
            promo.code = normalized_code
            promo.bonus_amount = normalized_bonus_amount
            promo.max_uses = normalized_max_uses
            promo.expires_at = normalized_expires_at
            promo.is_active = bool(is_active)
            await self.session.flush()

        return promo

    async def set_active(self, *, promo_id: int, is_active: bool) -> PromoCode:
        promo = await self._get_by_id_for_update(promo_id)
        if promo is None:
            raise LookupError('Промокод не найден.')
        await self.promos.set_active(promo, bool(is_active))
        return promo

    async def archive_promo(self, promo_id: int) -> PromoCode:
        return await self.set_active(promo_id=promo_id, is_active=False)

    async def activate_promo(self, promo_id: int) -> PromoCode:
        return await self.set_active(promo_id=promo_id, is_active=True)

    async def delete_promo(self, promo_id: int) -> bool:
        promo = await self._get_by_id_for_update(promo_id)
        if promo is None:
            raise LookupError('Промокод не найден.')
        if (promo.used_count or 0) > 0:
            raise ValueError(
                'Промокод уже использовался. Физическое удаление запрещено, используйте архивирование.'
            )

        delete_by_id = getattr(self.promos, 'delete_by_id', None)
        if callable(delete_by_id):
            return await delete_by_id(promo.id)

        await self.session.delete(promo)
        await self.session.flush()
        return True
