"""RBAC-инфраструктура для веб-админки (FEA-C39).

Предоставляет:
* `WebAdminPrincipal` — кто залогинен (username + role + флаг is_legacy).
* `authenticate_web_admin` — DB-lookup с fallback на legacy env-credentials
  (`WEB_ADMIN_USERNAME`/`WEB_ADMIN_PASSWORD`) — нужно пока боевые установки
  не мигрировали на DB.
* `require_role(*roles)` — Depends-factory для роле-специфичных gate'ов.
* `bootstrap_web_admin_from_env` — на старте создаёт `superadmin`-запись
  из env, если ни одного активного superadmin'а в DB ещё нет.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import AuditAction, AuditActorType, WebAdminRole, WebAdminUser
from app.db.repositories import AuditLogRepository, WebAdminUserRepository
from app.services.web_admin_auth import (
    hash_password,
    verify_password,
)


_MUTATING_METHODS = frozenset({'POST', 'PUT', 'PATCH', 'DELETE'})

logger = logging.getLogger(__name__)

web_admin_security = HTTPBasic()


@dataclass(frozen=True, slots=True)
class WebAdminPrincipal:
    """Кто залогинен сейчас в админку.

    `is_legacy` = True означает, что аутентификация прошла по env-credentials
    (WEB_ADMIN_USERNAME/PASSWORD), а не по записи в `web_admin_users`. Это
    переходный режим, чтобы не сломать прод до бутстрапа DB-записи.
    """

    username: str
    role: WebAdminRole
    is_legacy: bool
    db_id: int | None = None

    def has_role(self, *allowed: WebAdminRole) -> bool:
        # superadmin — wildcard для всех явных проверок (в декораторах
        # require_role это применяется отдельно, здесь — для удобства).
        if self.role is WebAdminRole.superadmin:
            return True
        return self.role in allowed


async def authenticate_web_admin(
    session: AsyncSession,
    settings: Settings,
    *,
    username: str,
    password: str,
) -> WebAdminPrincipal | None:
    """Возвращает WebAdminPrincipal или None при неверных кредах.

    Порядок:
    1. Lookup в `web_admin_users` (case-insensitive, только активных).
       Если password_hash совпал — touch_last_login + вернуть principal.
    2. Если не нашли в DB — fallback на env-credentials. В legacy-режиме
       роль = superadmin (как сейчас по факту), `is_legacy=True`.
    """
    repo = WebAdminUserRepository(session)
    db_user = await repo.get_by_username(username)
    if db_user is not None and db_user.is_active and verify_password(db_user.password_hash, password):
        await repo.touch_last_login(db_user)
        return WebAdminPrincipal(
            username=db_user.username,
            role=db_user.role,
            is_legacy=False,
            db_id=db_user.id,
        )

    # Legacy fallback: env WEB_ADMIN_USERNAME/PASSWORD как superadmin.
    env_username = settings.web_admin_username
    env_password_hash = settings.web_admin_password_value
    valid_username = secrets.compare_digest(username, env_username)
    valid_password = verify_password(env_password_hash, password)
    if valid_username and valid_password:
        if db_user is None:
            logger.warning(
                'Legacy env-based web-admin login: username=%s. '
                'Создайте запись в web_admin_users и снимите WEB_ADMIN_PASSWORD '
                'после миграции (FEA-C39).',
                env_username,
            )
        return WebAdminPrincipal(
            username=env_username,
            role=WebAdminRole.superadmin,
            is_legacy=True,
            db_id=None,
        )

    return None


def require_role(*allowed_roles: WebAdminRole):
    """Depends-factory: роле-специфичный gate.

    Пустой `allowed_roles` = только аутентификация без проверки роли
    (эквивалент legacy require_web_admin). superadmin проходит всегда.
    Возвращает `WebAdminPrincipal`, чтобы хендлеры могли логировать
    actor_username и принимать решения исходя из роли.
    """

    async def dependency(
        request: Request,
        creds: HTTPBasicCredentials = Depends(web_admin_security),
    ) -> WebAdminPrincipal:
        # Login-rate-limit реиспользуем из routes.py — импорт лениво,
        # чтобы избежать кругового импорта на этапе инициализации.
        from app.web.routes import _login_rate_limit_check, _record_login_failure

        client_ip = request.client.host if request.client else 'unknown'
        allowed, retry_after = _login_rate_limit_check(client_ip)
        if not allowed:
            logger.warning(
                'Web-admin login rate-limited: ip=%s retry_after=%s', client_ip, retry_after
            )
            raise HTTPException(
                status_code=429,
                detail='Too many failed login attempts',
                headers={'Retry-After': str(retry_after), 'WWW-Authenticate': 'Basic'},
            )

        settings: Settings = request.app.state.settings
        sessionmaker = request.app.state.sessionmaker

        async with sessionmaker.begin() as session:
            principal = await authenticate_web_admin(
                session,
                settings,
                username=creds.username,
                password=creds.password,
            )

        if principal is None:
            _record_login_failure(client_ip)
            raise HTTPException(
                status_code=401,
                detail='Unauthorized',
                headers={'WWW-Authenticate': 'Basic'},
            )

        if allowed_roles and not principal.has_role(*allowed_roles):
            logger.warning(
                'RBAC deny: username=%s role=%s required=%s path=%s',
                principal.username,
                principal.role.value,
                [r.value for r in allowed_roles],
                request.url.path,
            )
            raise HTTPException(status_code=403, detail='Недостаточно прав для этого действия')

        # Compliance: каждое mutation-действие через RBAC-gate пишется
        # в audit_logs (action=web_admin_action) с username/role/path/
        # method. GET не логируем — слишком шумно (журнал распухнет).
        # Отдельная транзакция через sessionmaker.begin() — иначе
        # хендлер, использующий свой `async with sessionmaker.begin()`,
        # увидит конфликт: nested begin() запрещён в asyncpg.
        if request.method.upper() in _MUTATING_METHODS:
            try:
                async with sessionmaker.begin() as session:
                    await AuditLogRepository(session).create(
                        action=AuditAction.web_admin_action,
                        actor_type=AuditActorType.admin,
                        actor_tg_id=None,
                        actor_username=principal.username,
                        entity_type='web_admin_route',
                        entity_id=request.url.path,
                        details={
                            'method': request.method,
                            'role': principal.role.value,
                            'is_legacy': principal.is_legacy,
                            'client_ip': client_ip,
                        },
                    )
            except Exception:
                # Не валим запрос из-за журнала — просто warning.
                logger.exception(
                    'Failed to write web_admin_action audit log for %s %s',
                    request.method,
                    request.url.path,
                )

        return principal

    return dependency


# Удобные алиасы — один Depends на роль/группу ролей.
require_any = require_role()
require_superadmin = require_role(WebAdminRole.superadmin)
require_finance = require_role(WebAdminRole.finance)
require_support = require_role(WebAdminRole.support)
require_finance_or_support = require_role(WebAdminRole.finance, WebAdminRole.support)


async def bootstrap_web_admin_from_env(
    session: AsyncSession,
    settings: Settings,
) -> None:
    """На старте создать superadmin-запись для env-username, если её нет.

    Идемпотентно. Не создаёт ничего, если уже есть активный superadmin
    под этим username (а вот неактивная запись с тем же username не
    блокирует вторую — bootstrap её не трогает, операторы решают).
    """
    repo = WebAdminUserRepository(session)
    existing = await repo.get_by_username(settings.web_admin_username)
    if existing is not None:
        return

    # Если в БД уже есть хоть один активный superadmin под другим именем —
    # bootstrap не нужен: оператор вручную мигрировал. Не создаём дубль.
    active_supers = await repo.count_active_by_role(WebAdminRole.superadmin)
    if active_supers > 0:
        return

    raw_password = settings.web_admin_password_value
    # Если в env уже хэш argon2 — переиспользуем; иначе хэшируем plaintext
    # (validate_password_strength уже проверила минимальную длину).
    if raw_password.startswith('$argon2'):
        password_hash = raw_password
    else:
        password_hash = hash_password(raw_password)

    await repo.create(
        username=settings.web_admin_username,
        password_hash=password_hash,
        role=WebAdminRole.superadmin,
        is_active=True,
        notes='Bootstrap из WEB_ADMIN_USERNAME (FEA-C39).',
    )
    logger.info(
        'Bootstrapped web_admin_users superadmin from env: username=%s',
        settings.web_admin_username,
    )


__all__ = (
    'WebAdminPrincipal',
    'authenticate_web_admin',
    'bootstrap_web_admin_from_env',
    'require_any',
    'require_finance',
    'require_finance_or_support',
    'require_role',
    'require_superadmin',
    'require_support',
    'web_admin_security',
)
