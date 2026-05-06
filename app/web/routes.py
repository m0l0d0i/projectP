from __future__ import annotations

import asyncio
import csv
import json
import html
import inspect
import logging
import io
import os
import re
import secrets
import ipaddress
import shlex
import shutil
import subprocess
import tempfile
from contextlib import suppress
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote_plus, urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import (
    AuditAction,
    AuditActorType,
    AuditLog,
    BroadcastJobStatus,
    InvoiceStatus,
    NodeHealthStatus,
    NodeSourceStatus,
    NodeSyncState,
    SupportTicketStatus,
    TransactionType,
    User,
)
from app.db.repositories import (
    AppLinkRepository,
    AppSettingsRepository,
    AuditLogRepository,
    BroadcastJobRepository,
    InvoiceRepository,
    MarzbanPageSettingsRepository,
    PricingRuleRepository,
    PromoRepository,
    SubscriptionRepository,
    SupportMessageRepository,
    SupportTicketRepository,
    TariffRepository,
    TransactionRepository,
    UserRepository,
)
from app.db.repositories.node_registry import NodeRegistryRepository
from app.services.broadcasts import BroadcastService, BroadcastValidationError
from app.services.geodata_updater import GeodataUpdater
from app.services.marzban import MarzbanClient
from app.services.marzban_env_manager import MarzbanEnvManager
from app.services.marzban_template_renderer import MarzbanTemplateRenderer
from app.services.node_policy import NodePolicyService
from app.services.payment_engine import PaymentService
from app.services.payments import MockPaymentProvider, PaymentProvider, PlategaProvider
from app.services.promos import PromoService
from app.services.routing_profiles import RoutingProfilesService, RoutingProfileValidationError
from app.services.subscription_urls import build_canonical_subscription_url, configured_public_subscription_origin
from app.services.subscriptions import SubscriptionService
from app.services.tariffs import PricingService
from app.utils.formatters import DISPLAY_TIMEZONE, bytes_to_gb, format_dt
from app.utils.runtime_settings import (
    effective_bool_from_row,
    effective_int_from_row,
    effective_list_from_row,
    effective_optional_int_from_row,
)

router = APIRouter()
web_admin_security = HTTPBasic()
logger = logging.getLogger(__name__)


ADMIN_PAGE_SIZE = 20
CSV_EXPORT_LIMIT = 5000


def _first_int_or_none(values: list[object] | tuple[object, ...] | set[object] | None) -> int | None:
    for value in values or []:
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _app_settings_view(row, settings: Settings) -> SimpleNamespace:
    min_interval = (
        getattr(row, 'anti_spam_min_interval_seconds', None)
        if row is not None
        else settings.anti_spam_min_interval_seconds
    )
    if min_interval is None:
        min_interval = settings.anti_spam_min_interval_seconds

    return SimpleNamespace(
        admin_ids=effective_list_from_row(row, 'admin_ids', settings.admin_ids),
        support_ids=effective_list_from_row(row, 'support_ids', settings.support_ids),
        startup_notify_ids=effective_list_from_row(row, 'startup_notify_ids', settings.startup_notify_ids),
        support_chat_id=effective_optional_int_from_row(row, 'support_chat_id', settings.support_chat_id),
        support_chat_test_last_status=getattr(row, 'support_chat_test_last_status', 'never') if row is not None else 'never',
        support_chat_test_last_error=getattr(row, 'support_chat_test_last_error', None) if row is not None else None,
        trial_duration_days=effective_int_from_row(row, 'trial_duration_days', settings.trial_duration_days, minimum=1),
        trial_traffic_gb=effective_int_from_row(row, 'trial_traffic_gb', settings.trial_traffic_gb, minimum=0),
        trial_device_count=effective_int_from_row(row, 'trial_device_count', settings.trial_device_count, minimum=1),
        anti_spam_enabled=effective_bool_from_row(row, 'anti_spam_enabled', settings.anti_spam_enabled),
        anti_spam_message_limit=effective_int_from_row(row, 'anti_spam_message_limit', settings.anti_spam_message_limit, minimum=1),
        anti_spam_message_window_seconds=effective_int_from_row(
            row, 'anti_spam_message_window_seconds', settings.anti_spam_message_window_seconds, minimum=1
        ),
        anti_spam_callback_limit=effective_int_from_row(row, 'anti_spam_callback_limit', settings.anti_spam_callback_limit, minimum=1),
        anti_spam_callback_window_seconds=effective_int_from_row(
            row, 'anti_spam_callback_window_seconds', settings.anti_spam_callback_window_seconds, minimum=1
        ),
        anti_spam_block_seconds=effective_int_from_row(row, 'anti_spam_block_seconds', settings.anti_spam_block_seconds, minimum=1),
        anti_spam_min_interval_seconds=min_interval,
        rules_service_url=_safe_public_url_for_display(
            getattr(row, 'rules_service_url', settings.rules_service_url) if row is not None else settings.rules_service_url,
            field_label='Ссылка на правила сервиса',
        ),
        rules_of_use_url=_safe_public_url_for_display(
            getattr(row, 'rules_of_use_url', settings.rules_of_use_url) if row is not None else settings.rules_of_use_url,
            field_label='Ссылка на пользовательское соглашение',
        ),
        rules_privacy_url=_safe_public_url_for_display(
            getattr(row, 'rules_privacy_url', settings.rules_privacy_url) if row is not None else settings.rules_privacy_url,
            field_label='Ссылка на политику конфиденциальности',
        ),
    )


def _first_runtime_recipient_tg_id(row, settings: Settings) -> int | None:
    recipient = _first_int_or_none(effective_list_from_row(row, 'startup_notify_ids', settings.startup_notify_ids))
    if recipient is not None:
        return recipient
    return _first_int_or_none(effective_list_from_row(row, 'admin_ids', settings.admin_ids))


def _coerce_int_list(values: list[object] | tuple[object, ...] | set[object] | None) -> list[int]:
    result: list[int] = []
    seen: set[int] = set()
    for value in values or []:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed in seen:
            continue
        seen.add(parsed)
        result.append(parsed)
    return result


def _parse_int_list_text(raw: str | None) -> list[int]:
    chunks = re.split(r"[\s,;]+", (raw or '').strip())
    return _coerce_int_list([chunk for chunk in chunks if chunk])


def _parse_optional_int_text(raw: str | None) -> int | None:
    normalized = (raw or '').strip()
    if not normalized:
        return None
    return int(normalized)


def _parse_optional_query_int(raw: str | None) -> int | None:
    normalized = (raw or '').strip()
    if not normalized:
        return None
    try:
        parsed = int(normalized)
    except ValueError:
        return None
    return parsed if parsed >= 1 else None




async def _mark_user_bot_blocked(sessionmaker, user_id: int, reason: str) -> None:
    async with sessionmaker.begin() as session:
        user_repo = UserRepository(session)
        user = await user_repo.get_by_id_for_update(user_id)
        if user is not None:
            await user_repo.set_bot_blocked(user, True, reason)


async def _notify_ticket_closed_from_web_admin(
    request: Request,
    *,
    ticket_id: int,
    user_id: int,
) -> None:
    bot = _get_bot_from_request(request)
    if bot is None:
        logger.warning('Skipping web-admin ticket-close notifications: bot is not attached to app.state')
        return

    sessionmaker = request.app.state.sessionmaker
    settings = request.app.state.settings

    async with sessionmaker() as session:
        app_settings_repo = AppSettingsRepository(session)
        user_repo = UserRepository(session)

        row = await app_settings_repo.get()
        user = await user_repo.get_by_id(user_id)

        support_chat_id = effective_optional_int_from_row(row, 'support_chat_id', settings.support_chat_id)
        support_ids = _coerce_int_list(effective_list_from_row(row, 'support_ids', settings.support_ids))
        admin_ids = _coerce_int_list(effective_list_from_row(row, 'admin_ids', settings.admin_ids))

    user_tg_id = getattr(user, 'tg_id', None) if user is not None else None
    recipient_ids = sorted({*support_ids, *admin_ids})
    if user_tg_id is not None:
        recipient_ids = [tg_id for tg_id in recipient_ids if tg_id != user_tg_id]

    user_text = '🔒 Ваша заявка закрыта.\n\nЕсли потребуется помощь, создайте новое обращение через раздел поддержки.'
    support_text = f'🔒 Заявка #{ticket_id} #ticket{ticket_id} закрыта (оператором).'

    if user_tg_id:
        try:
            await bot.send_message(user_tg_id, user_text)
        except Exception as exc:  # pragma: no cover - runtime safety
            logger.exception('Failed to notify user about web-admin ticket close: ticket_id=%s user_id=%s', ticket_id, user_id)
            exc_name = exc.__class__.__name__.lower()
            if 'forbidden' in exc_name:
                await _mark_user_bot_blocked(sessionmaker, user_id, str(exc))

    delivered_to_support = False
    if support_chat_id:
        try:
            await bot.send_message(support_chat_id, support_text)
            delivered_to_support = True
        except Exception:  # pragma: no cover - runtime safety
            logger.exception('Failed to notify support chat about web-admin ticket close: ticket_id=%s chat_id=%s', ticket_id, support_chat_id)

    if not delivered_to_support:
        for recipient_id in recipient_ids:
            try:
                await bot.send_message(recipient_id, support_text)
                delivered_to_support = True
            except Exception:  # pragma: no cover - runtime safety
                logger.exception('Failed to notify fallback support recipient about web-admin ticket close: ticket_id=%s recipient_tg_id=%s', ticket_id, recipient_id)


def _public_url_or_none(value: str | None, *, field_label: str) -> str | None:
    normalized = (value or '').strip()
    if not normalized:
        return None

    parsed = urlparse(normalized)
    if parsed.scheme.lower() != 'https' or not parsed.netloc:
        raise ValueError(f'{field_label} должна быть полным https URL')

    if parsed.username or parsed.password:
        raise ValueError(f'{field_label} не должна содержать username/password')

    hostname = (parsed.hostname or '').strip().lower()
    if not hostname:
        raise ValueError(f'{field_label} должна содержать hostname')
    if hostname == 'localhost':
        raise ValueError(f'{field_label} не может указывать на localhost')

    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        return normalized

    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        raise ValueError(f'{field_label} не может указывать на private/loopback IP')

    return normalized


def _safe_public_url_for_display(value: str | None, *, field_label: str) -> str | None:
    try:
        return _public_url_or_none(value, field_label=field_label)
    except ValueError:
        return None


def _nullable_public_url_form_value(raw: str | None, *, field_label: str) -> str | None:
    return _public_url_or_none(raw, field_label=field_label)


def _csv_writer_buffer() -> tuple[io.StringIO, csv.writer]:
    buffer = io.StringIO(newline='')
    buffer.write('\ufeff')
    writer = csv.writer(buffer, dialect='excel', lineterminator='\r\n')
    return buffer, writer


def _safe_csv_cell(value: object) -> str:
    if value is None:
        return ''

    text = str(value)
    if not text:
        return ''

    if text[0] in {'=', '+', '-', '@', '\t', '\r', '\n'}:
        return f"'{text}"
    return text


def require_web_admin(
    request: Request,
    creds: HTTPBasicCredentials = Depends(web_admin_security),
) -> None:
    settings = request.app.state.settings

    valid_username = secrets.compare_digest(creds.username, settings.web_admin_username)
    valid_password = secrets.compare_digest(creds.password, settings.web_admin_password_value)

    if not (valid_username and valid_password):
        raise HTTPException(
            status_code=401,
            detail='Unauthorized',
            headers={'WWW-Authenticate': 'Basic'},
        )


def _money(value: Decimal | None) -> str:
    value = value or Decimal('0.00')
    return f'{value:.2f} ₽'


def _subscription_status_label(sub) -> str:
    return 'Активна' if getattr(sub, 'is_active', False) else 'Неактивна'


def _traffic_label(sub) -> str:
    provided = 'Безлимит' if getattr(sub, 'monthly_traffic_bytes', None) in (None, 0) else bytes_to_gb(sub.monthly_traffic_bytes)
    used = bytes_to_gb(getattr(sub, 'used_traffic_bytes', 0) or 0)
    return f'{used} / {provided}'


def _expire_label(sub) -> str:
    if getattr(sub, 'expire_date', None) is None:
        return '—'
    return format_dt(sub.expire_date)


def _build_payment_provider(settings: Settings, provider_name: str | None = None) -> PaymentProvider:
    provider = (provider_name or settings.payment_provider or 'mock').strip().lower()
    if provider == 'platega':
        return PlategaProvider(settings)
    return MockPaymentProvider()


def _build_payment_service(session, settings: Settings, provider_name: str | None = None) -> tuple[PaymentService, MarzbanClient, PaymentProvider]:
    marzban = MarzbanClient(settings)
    payments = _build_payment_provider(settings, provider_name=provider_name)
    subscription_service = SubscriptionService(session, settings, marzban)
    service = PaymentService(session, settings, payments, subscription_service)
    return service, marzban, payments


async def _close_payment_stack(marzban: MarzbanClient, payments: PaymentProvider) -> None:
    with suppress(Exception):
        await marzban.close()

    close = getattr(payments, 'close', None)
    if callable(close):
        with suppress(Exception):
            await close()


def _redirect_with_message(path: str, *, success: str | None = None, error: str | None = None) -> RedirectResponse:
    params: list[str] = []
    if success:
        params.append(f'success={quote_plus(success)}')
    if error:
        params.append(f'error={quote_plus(error)}')
    url = path if not params else f'{path}?{"&".join(params)}'
    return RedirectResponse(url=url, status_code=303)


def _parse_id_list(raw: str | None) -> list[int]:
    if not raw:
        return []
    parts = re.split(r'[\s,;]+', raw.strip())
    result: list[int] = []
    seen: set[int] = set()
    for part in parts:
        if not part:
            continue
        value = int(part)
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _ids_to_text(values: list[int] | None) -> str:
    return ', '.join(str(v) for v in (values or []))


def _normalized_optional_form_text(value: str | None) -> str | None:
    normalized = (value or '').strip()
    return normalized or None


def _preserve_existing_id_list(raw: str | None, current: list[int] | None) -> list[int]:
    if not (raw or '').strip():
        return list(current or [])
    return _parse_id_list(raw)


def _preserve_existing_bigint(raw: str | None, current: int | None) -> int | None:
    normalized = _normalized_optional_form_text(raw)
    if normalized is None:
        return current
    return int(normalized)


def _managed_env_updates_from_form(form: Any, *, prefix: str = 'env__') -> dict[str, str | None]:
    updates: dict[str, str | None] = {}
    for key, value in form.items():
        if not str(key).startswith(prefix):
            continue
        normalized_key = str(key).removeprefix(prefix)
        if isinstance(value, str):
            updates[normalized_key] = value
        elif value is None:
            updates[normalized_key] = None
        else:
            updates[normalized_key] = str(value)
    return updates


def _normalize_local_command(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part for part in shlex.split(value) if part]
    if isinstance(value, (list, tuple)):
        return [str(part).strip() for part in value if str(part).strip()]
    return []


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode='w',
            encoding='utf-8',
            newline='',
            dir=str(path.parent),
            prefix=f'.{path.name}.',
            suffix='.tmp',
            delete=False,
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            with suppress(FileNotFoundError):
                temp_path.unlink()


def _backup_existing_file(path: Path, *, label: str) -> Path | None:
    if not path.exists():
        return None
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
    backup_path = path.with_name(f'{path.name}.{label}.{timestamp}.bak')
    shutil.copy2(path, backup_path)
    return backup_path


def _command_result_summary(result: Any | None) -> dict[str, Any]:
    if result is None:
        return {'attempted': False, 'returncode': None, 'stdout': '', 'stderr': ''}
    return {
        'attempted': True,
        'returncode': getattr(result, 'returncode', None),
        'stdout': (getattr(result, 'stdout', '') or '').strip(),
        'stderr': (getattr(result, 'stderr', '') or '').strip(),
    }


def _build_marzban_page_apply_health(
    *,
    preview_ok: bool,
    preview_message: str,
    template_paths_state: Any,
    env_path_state: Any,
    restart_command: list[str],
    pending_changes: bool,
) -> dict[str, Any]:
    blockers: list[str] = []
    warnings: list[str] = []

    if not preview_ok:
        blockers.append('Preview/рендер сейчас завершается с ошибкой; live apply заблокирован, пока шаблон и контекст не станут валидными.')
    if not getattr(template_paths_state, 'source_exists', False):
        blockers.append(f'Шаблон-источник не найден: {getattr(template_paths_state, "source_template_path", "?")}')
    if not getattr(template_paths_state, 'deployed_writable', False):
        blockers.append(
            f'Live deploy target недоступен для записи: {getattr(template_paths_state, "deployed_template_path", "?")}'
        )
    if not getattr(env_path_state, 'writable', False):
        warnings.append(f'Путь управляемого env недоступен для записи: {getattr(env_path_state, "path", "?")}')
    if not restart_command:
        warnings.append('Команда рестарта Marzban не настроена: файл страницы будет применён, но рестарт сервиса будет пропущен.')
    if not pending_changes:
        warnings.append('Сформированный preview совпадает с текущей live-страницей: ожидающих HTML-изменений сейчас нет.')

    ready = not blockers
    state = 'ready' if ready else 'blocked'
    message_parts = [preview_message]
    if blockers:
        message_parts.append('Блокирующие условия: ' + ' | '.join(blockers))
    if warnings:
        message_parts.append('Предупреждения: ' + ' | '.join(warnings))

    return {
        'ok': ready,
        'state': state,
        'message': ' '.join(part for part in message_parts if part).strip(),
        'blockers': blockers,
        'warnings': warnings,
        'pending_changes': pending_changes,
        'restart_command': restart_command,
        'restart_configured': bool(restart_command),
    }


def _support_delivery_health(app_settings, settings: Settings) -> tuple[bool, str, str]:
    support_chat_id = effective_optional_int_from_row(app_settings, 'support_chat_id', settings.support_chat_id)
    support_ids = [int(v) for v in effective_list_from_row(app_settings, 'support_ids', settings.support_ids) if str(v).strip()]
    admin_ids = [int(v) for v in effective_list_from_row(app_settings, 'admin_ids', settings.admin_ids) if str(v).strip()]
    fallback_recipients = list(dict.fromkeys([*support_ids, *admin_ids]))

    support_status = getattr(app_settings, 'support_chat_test_last_status', 'never')
    support_error = getattr(app_settings, 'support_chat_test_last_error', None)

    if support_chat_id is not None and support_status == 'ok':
        msg = f'Основной канал: support_chat_id={support_chat_id}. '
        if fallback_recipients:
            msg += f'Резервная доставка: прямое сообщение для {len(fallback_recipients)} получател(ей) поддержки/админов.'
        else:
            msg += 'Резервные получатели не настроены.'
        return True, msg, 'primary_ok'

    if support_chat_id is not None and fallback_recipients:
        msg = f'Основной чат настроен, но результат теста — {support_status}. '
        if support_error:
            msg += f'Последняя ошибка: {support_error}. '
        msg += f'Доступен резервный режим: прямое сообщение для {len(fallback_recipients)} получател(ей) поддержки/админов.'
        return True, msg, 'fallback_available'

    if support_chat_id is None and fallback_recipients:
        msg = f'support_chat_id is not configured. Доступен резервный режим: прямое сообщение для {len(fallback_recipients)} получател(ей) поддержки/админов.'
        return True, msg, 'fallback_only'

    if support_chat_id is not None:
        msg = f'Основной чат настроен (support_chat_id={support_chat_id}), но резервные получатели поддержки/админов не настроены.'
        if support_error:
            msg += f' Последняя ошибка: {support_error}.'
        return support_status == 'ok', msg, 'primary_only' if support_status == 'ok' else 'primary_unverified'

    return False, 'Цель доставки в поддержку не настроена: support_chat_id пуст, а списки получателей поддержки/админов пусты.', 'missing'


def _marzban_access_health(settings: Settings) -> tuple[bool, str, str]:
    if not getattr(settings, 'marzban_enabled', False):
        return False, 'Marzban отключён.', 'disabled'

    base_url = (getattr(settings, 'marzban_api_base_url', None) or '').strip()
    if not base_url:
        return False, 'MARZBAN_API_BASE_URL не настроен.', 'missing_base_url'

    lowered = base_url.lower()
    if '127.0.0.1' in lowered or 'localhost' in lowered:
        return (
            False,
            'API base URL указывает на localhost/127.0.0.1. Для оператора подойдёт SSH-туннель до http://127.0.0.1:8000 с рабочей станции, но bot внутри Docker обычно не может использовать localhost хоста напрямую.',
            'localhost_only',
        )

    if 'host.docker.internal' in lowered:
        return (
            True,
            'API base URL использует host.docker.internal. Если runtime-health всё ещё падает, вероятно Marzban доступен только на localhost хоста. Для оператора подойдёт SSH-туннель до http://127.0.0.1:8000. Для bot потребуется внутренний proxy или bind на адрес, доступный контейнеру.',
            'docker_host_gateway',
        )

    return True, f'Настроенный API base URL: {base_url}', 'direct_url'


def _web_admin_access_health(settings: Settings) -> tuple[bool, str, str]:
    host = (getattr(settings, 'web_admin_host', None) or '127.0.0.1').strip() or '127.0.0.1'
    port = int(getattr(settings, 'web_admin_port', 8001) or 8001)
    local_only = bool(getattr(settings, 'web_admin_local_only', False))
    trust_forwarded = bool(getattr(settings, 'web_admin_trust_forwarded_headers', False))
    allowed_ips = list(getattr(settings, 'web_admin_allowed_ips', []) or [])
    allowed_proxy_ips = list(getattr(settings, 'web_admin_allowed_proxy_ips', []) or [])
    loopback_hosts = {str(v).strip().lower() for v in getattr(settings, 'web_admin_loopback_hosts', set())}
    host_normalized = host.lower()

    bind_label = f'{host}:{port}'
    tunnel_hint = f'Резервный доступ для оператора: SSH-туннель до http://127.0.0.1:{port}/admin/'

    if host_normalized in loopback_hosts:
        msg = f'Bind: {bind_label}. Web-admin слушает только loopback/local-only адрес.'
        if local_only:
            msg += f' local_only=true; разрешённые IP: {", ".join(allowed_ips) if allowed_ips else "не заданы"}.'
        msg += f' {tunnel_hint}'
        if trust_forwarded:
            msg += f' Forwarded headers доверяются через allowlist proxy: {", ".join(allowed_proxy_ips) if allowed_proxy_ips else "не заданы"}.'
        return True, msg, 'ssh_tunnel_required'

    if local_only:
        msg = f'Bind: {bind_label}. web_admin_local_only=true, но host не loopback. Проверьте runtime-конфиг или ограничения reverse proxy.'
        if allowed_ips:
            msg += f' Разрешённые IP: {", ".join(allowed_ips)}.'
        msg += f' {tunnel_hint}'
        return False, msg, 'local_only_misconfigured'

    msg = f'Bind: {bind_label}. Web-admin is not loopback-bound in app config.'
    if allowed_ips:
        msg += f' Разрешённые IP: {", ".join(allowed_ips)}.'
    if trust_forwarded:
        msg += f' Forwarded headers доверяются через allowlist proxy: {", ".join(allowed_proxy_ips) if allowed_proxy_ips else "не заданы"}.'
    msg += ' Exposure must be restricted outside the app (Docker/host firewall/reverse proxy). Preferred operator access is still SSH tunnel.'
    return True, msg, 'non_loopback_bind'


def _marzban_ops_ui_health(settings: Settings, raw_status: dict[str, Any]) -> tuple[dict[str, str | bool], dict[str, str | bool]]:
    assets_dir = Path(getattr(settings, 'geodata_assets_dir', '') or '')
    dir_exists = assets_dir.exists()
    writable_target = assets_dir if dir_exists else assets_dir.parent
    dir_writable = bool(str(writable_target)) and os.access(writable_target, os.W_OK)

    statuses = {name: status for name, status in (raw_status or {}).items()}
    total_assets = len(statuses)
    present_assets = sum(1 for status in statuses.values() if getattr(status, 'exists', False))
    missing_assets = [name for name, status in statuses.items() if not getattr(status, 'exists', False)]

    if total_assets == 0:
        geodata_health = {
            'ok': False,
            'state': 'no_status',
            'message': 'Geodata status is unavailable: updater did not return geoip/geosite file metadata.',
        }
    else:
        state = 'ready' if dir_writable and present_assets == total_assets else 'degraded'
        geodata_health = {
            'ok': bool(dir_writable and present_assets == total_assets),
            'state': state,
            'message': (
                f'Assets dir: {assets_dir}. Exists: {"yes" if dir_exists else "no"}. '
                f'Writable target: {writable_target}. Writable: {"yes" if dir_writable else "no"}. '
                f'Present geodata files: {present_assets}/{total_assets}. '
                + (f'Missing: {", ".join(missing_assets)}. ' if missing_assets else '')
                + 'Use this page only for safe operational updates of geoip/geosite assets.'
            ),
        }

    access_ok, access_message, access_state = _marzban_access_health(settings)
    access_health = {
        'ok': access_ok,
        'state': access_state,
        'message': access_message,
    }
    return geodata_health, access_health


def _node_registry_ui_health(nodes: list[Any]) -> tuple[bool, str, str]:
    total = len(nodes or [])
    enabled = [node for node in nodes or [] if bool(getattr(node, 'is_enabled', False))]
    defaults = [node for node in nodes or [] if bool(getattr(node, 'is_default', False))]
    missing_api = [getattr(node, 'code', str(getattr(node, 'id', '?'))) for node in nodes or [] if not (getattr(node, 'api_base_url', None) or '').strip()]
    missing_sub = [getattr(node, 'code', str(getattr(node, 'id', '?'))) for node in nodes or [] if not (getattr(node, 'subscription_base_url', None) or '').strip()]
    health_map: dict[str, int] = {}
    sync_map: dict[str, int] = {}
    source_map: dict[str, int] = {}
    sync_errors = [getattr(node, 'code', str(getattr(node, 'id', '?'))) for node in nodes or [] if (getattr(node, 'sync_error', None) or '').strip()]
    synced = 0
    for node in nodes or []:
        raw_health = getattr(node, 'health_status', None)
        health_status = getattr(raw_health, 'value', raw_health) or NodeHealthStatus.unknown.value
        health_map[str(health_status)] = health_map.get(str(health_status), 0) + 1

        raw_sync = getattr(node, 'sync_state', None)
        sync_state = getattr(raw_sync, 'value', raw_sync) or NodeSyncState.never_synced.value
        sync_map[str(sync_state)] = sync_map.get(str(sync_state), 0) + 1
        if str(sync_state) == NodeSyncState.synced.value:
            synced += 1

        raw_source = getattr(node, 'source_status', None)
        source_status = getattr(raw_source, 'value', raw_source) or NodeSourceStatus.unknown.value
        source_map[str(source_status)] = source_map.get(str(source_status), 0) + 1

    if total == 0:
        return False, 'No nodes configured yet. Multi-node foundation exists, but registry is empty.', 'empty'

    parts = [
        f'Nodes: {total}',
        f'активных: {len(enabled)}',
        f'default: {len(defaults)}',
        f'синхронизировано: {synced}',
        'sync: ' + ', '.join(f'{k}={v}' for k, v in sorted(sync_map.items())),
        'source: ' + ', '.join(f'{k}={v}' for k, v in sorted(source_map.items())),
        'health: ' + ', '.join(f'{k}={v}' for k, v in sorted(health_map.items())),
    ]
    if missing_api:
        parts.append(f'без api_base_url: {", ".join(missing_api[:5])}{"..." if len(missing_api) > 5 else ""}')
    if missing_sub:
        parts.append(f'без subscription_base_url: {", ".join(missing_sub[:5])}{"..." if len(missing_sub) > 5 else ""}')
    if sync_errors:
        parts.append(f'with sync_error: {", ".join(sync_errors[:5])}{"..." if len(sync_errors) > 5 else ""}')

    ok = bool(enabled) and len(defaults) <= 1 and sync_map.get(NodeSyncState.error.value, 0) == 0
    state = 'ready' if ok and not missing_api else 'degraded'
    if len(defaults) > 1:
        state = 'multiple_defaults'
        ok = False
    elif not enabled:
        state = 'no_enabled_nodes'
        ok = False
    elif sync_map.get(NodeSyncState.error.value, 0):
        state = 'sync_errors'
        ok = False
    elif sync_map.get(NodeSyncState.missing.value, 0):
        state = 'source_missing'
    elif sync_map.get(NodeSyncState.never_synced.value, 0):
        state = 'never_synced'
    return ok, '. '.join(parts) + '.', state


def _node_source_status_label(value: str | None) -> str:
    mapping = {
        NodeSourceStatus.active.value: 'В источнике активен',
        NodeSourceStatus.disabled.value: 'В источнике отключён',
        NodeSourceStatus.unknown.value: 'Статус источника неизвестен',
    }
    return mapping.get((value or '').strip(), value or 'неизвестно')



def _node_source_status_tone(value: str | None) -> str:
    mapping = {
        NodeSourceStatus.active.value: 'emerald',
        NodeSourceStatus.disabled.value: 'rose',
        NodeSourceStatus.unknown.value: 'slate',
    }
    return mapping.get((value or '').strip(), 'slate')



def _node_sync_state_label(value: str | None) -> str:
    mapping = {
        NodeSyncState.synced.value: 'Синхронизирован',
        NodeSyncState.never_synced.value: 'Ещё не синхронизирован',
        NodeSyncState.missing.value: 'Пропал из источника',
        NodeSyncState.error.value: 'Ошибка синка',
    }
    return mapping.get((value or '').strip(), value or 'неизвестно')



def _node_sync_state_tone(value: str | None) -> str:
    mapping = {
        NodeSyncState.synced.value: 'emerald',
        NodeSyncState.never_synced.value: 'amber',
        NodeSyncState.missing.value: 'rose',
        NodeSyncState.error.value: 'rose',
    }
    return mapping.get((value or '').strip(), 'slate')



def _node_row_for_admin(node: Any) -> dict[str, Any]:
    raw_health = getattr(node, 'health_status', None)
    raw_source = getattr(node, 'source_status', None)
    raw_sync = getattr(node, 'sync_state', None)
    health_status = str(getattr(raw_health, 'value', raw_health) or NodeHealthStatus.unknown.value)
    source_status = str(getattr(raw_source, 'value', raw_source) or NodeSourceStatus.unknown.value)
    sync_state = str(getattr(raw_sync, 'value', raw_sync) or NodeSyncState.never_synced.value)
    return {
        'node': node,
        'health_status': health_status,
        'source_status': source_status,
        'sync_state': sync_state,
        'source_status_label': _node_source_status_label(source_status),
        'source_status_tone': _node_source_status_tone(source_status),
        'sync_state_label': _node_sync_state_label(sync_state),
        'sync_state_tone': _node_sync_state_tone(sync_state),
        'last_sync_at_display': format_dt(getattr(node, 'last_sync_at', None)),
        'sync_error': (getattr(node, 'sync_error', None) or '').strip() or None,
        'source_node_id': (getattr(node, 'source_node_id', None) or '').strip() or None,
    }


def _routing_profiles_ui_health(profiles: list[Any], default_profile_id: int | None) -> tuple[bool, str, str]:
    total = len(profiles or [])
    enabled = [profile for profile in profiles or [] if bool(getattr(profile, 'is_enabled', False))]
    default_profile = next((profile for profile in profiles or [] if getattr(profile, 'id', None) == default_profile_id), None)
    missing_config = [getattr(profile, 'code', str(getattr(profile, 'id', '?'))) for profile in profiles or [] if not isinstance(getattr(profile, 'config_json', None), dict)]
    empty_tags = [getattr(profile, 'code', str(getattr(profile, 'id', '?'))) for profile in profiles or [] if not list(getattr(profile, 'match_tags', None) or [])]

    if total == 0:
        return False, 'No routing profiles configured yet. Routing foundation exists, but there are no managed profiles to select.', 'empty'

    parts = [
        f'Profiles: {total}',
        f'активных: {len(enabled)}',
        f'default: {getattr(default_profile, "code", "not selected")}',
    ]
    if missing_config:
        parts.append(f'config_json не является объектом: {", ".join(missing_config[:5])}{"..." if len(missing_config) > 5 else ""}')
    if empty_tags:
        parts.append(f'without match_tags: {", ".join(empty_tags[:5])}{"..." if len(empty_tags) > 5 else ""}')

    ok = bool(enabled) and default_profile is not None and not missing_config
    state = 'ready' if ok else 'degraded'
    if default_profile is None:
        state = 'no_default'
    elif not enabled:
        state = 'no_enabled_profiles'
    elif missing_config:
        state = 'invalid_config_shape'
    return ok, '. '.join(parts) + '.', state


def _routing_profile_form(profile: Any | None = None) -> dict[str, Any]:
    config_json = getattr(profile, 'config_json', {}) if profile is not None else {}
    if not isinstance(config_json, dict):
        config_json = {}
    return {
        'profile_id': getattr(profile, 'id', None),
        'code': getattr(profile, 'code', '') if profile is not None else '',
        'title': getattr(profile, 'title', '') if profile is not None else '',
        'description': getattr(profile, 'description', '') if profile is not None else '',
        'sort_order': int(getattr(profile, 'sort_order', 100) or 100) if profile is not None else 100,
        'match_tags_text': ', '.join(list(getattr(profile, 'match_tags', None) or [])) if profile is not None else '',
        'config_json_text': json.dumps(config_json, ensure_ascii=False, indent=2, sort_keys=True),
        'notes': getattr(profile, 'notes', '') if profile is not None else '',
        'is_enabled': bool(getattr(profile, 'is_enabled', True)) if profile is not None else True,
        'is_default': bool(getattr(profile, 'is_default', False)) if profile is not None else False,
    }


def _routing_profile_summary(profile: Any) -> dict[str, Any]:
    config_json = getattr(profile, 'config_json', {})
    if not isinstance(config_json, dict):
        config_json = {}
    strategy = str(config_json.get('strategy') or 'prefer_healthy')
    required_tags = list(config_json.get('required_tags') or []) if isinstance(config_json.get('required_tags'), list) else []
    preferred_tags = list(config_json.get('preferred_tags') or []) if isinstance(config_json.get('preferred_tags'), list) else []
    return {
        'id': getattr(profile, 'id', None),
        'code': getattr(profile, 'code', None),
        'title': getattr(profile, 'title', None),
        'description': getattr(profile, 'description', None),
        'is_enabled': bool(getattr(profile, 'is_enabled', False)),
        'is_default': bool(getattr(profile, 'is_default', False)),
        'sort_order': int(getattr(profile, 'sort_order', 0) or 0),
        'match_tags': list(getattr(profile, 'match_tags', None) or []),
        'runtime_strategy': strategy,
        'required_tags': required_tags,
        'preferred_tags': preferred_tags,
        'has_xray_fragment': isinstance(config_json.get('xray_fragment'), dict) and bool(config_json.get('xray_fragment')),
        'config_json_text': json.dumps(config_json, ensure_ascii=False, indent=2, sort_keys=True),
    }


def _routing_node_request_to_dict(node_request: Any) -> dict[str, Any]:
    fields = [
        'policy_name',
        'required_tags',
        'preferred_tags',
        'avoid_tags',
        'location_allow',
        'preferred_locations',
        'transport_allow',
        'require_subscription_url',
        'require_api_url',
        'require_synced_source',
        'allow_missing_source',
        'allow_degraded',
        'premium_only',
        'reserve_only',
        'preferred_node_codes',
        'avoid_node_codes',
        'metadata',
    ]
    payload: dict[str, Any] = {}
    for name in fields:
        value = getattr(node_request, name, None)
        if isinstance(value, set):
            payload[name] = sorted(str(item) for item in value)
        elif isinstance(value, list):
            payload[name] = list(value)
        elif isinstance(value, dict):
            payload[name] = dict(value)
        else:
            payload[name] = value
    return payload


def _routing_xray_restart_warning_default() -> str:
    return (
        'Применение routing/Xray-конфигурации приведёт к перезапуску Xray. '
        'Перед apply убедитесь, что администратор и активные пользователи готовы к кратковременному рестарту.'
    )


async def _build_routing_preview_context(
    session: AsyncSession,
    settings: Settings,
    service: RoutingProfilesService,
    *,
    preview_profile_id: int | None,
    preview_tags_text: str | None,
) -> dict[str, Any] | None:
    if preview_profile_id is None:
        return None

    profile = await service.get_by_id(preview_profile_id)
    if profile is None:
        return {
            'profile_id': preview_profile_id,
            'requested_tags': _parse_csv_tags(preview_tags_text),
            'error': 'Routing profile не найден',
            'restart_required': False,
            'restart_warning': _routing_xray_restart_warning_default(),
        }

    requested_tags = _parse_csv_tags(preview_tags_text)
    config_json = getattr(profile, 'config_json', {})
    if not isinstance(config_json, dict):
        config_json = {}

    payload: dict[str, Any] = {
        'profile_id': profile.id,
        'profile_code': profile.code,
        'profile_title': profile.title,
        'requested_tags': requested_tags,
        'matched_by_default': False,
        'runtime_config_json': json.dumps(config_json, ensure_ascii=False, indent=2, sort_keys=True),
        'node_request_json': None,
        'selection_json': None,
        'selected_node_code': None,
        'selection_reason': None,
        'xray_fragment_json': None,
        'xray_apply_preview_json': None,
        'validation_command': [],
        'restart_command': [],
        'restart_required': False,
        'restart_warning': _routing_xray_restart_warning_default(),
        'error': None,
    }

    marzban: MarzbanClient | None = None
    try:
        resolved = None
        node_request = None
        if hasattr(service, 'preview'):
            preview = await service.preview(
                profile_code=profile.code,
                requested_tags=requested_tags,
                fallback_to_default=False,
            )
            resolved = getattr(preview, 'resolved', None)
            payload['matched_by_default'] = bool(getattr(resolved, 'matched_by_default', False))
            runtime_config = getattr(resolved, 'runtime_config', None)
            if runtime_config is not None and hasattr(runtime_config, 'as_dict'):
                payload['runtime_config_json'] = json.dumps(runtime_config.as_dict(), ensure_ascii=False, indent=2, sort_keys=True)
            node_request = getattr(preview, 'node_request', None)
            if node_request is not None:
                payload['node_request_json'] = json.dumps(_routing_node_request_to_dict(node_request), ensure_ascii=False, indent=2, sort_keys=True)

        selected_node = None
        if node_request is not None:
            policy = NodePolicyService(session)
            decision = await policy.select_node(node_request)
            payload['selected_node_code'] = getattr(decision, 'selected_node_code', None)
            payload['selection_reason'] = getattr(decision, 'reason', None)
            payload['selection_json'] = json.dumps(
                await policy.explain_selection(node_request),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            selected_node = getattr(decision, 'node', None)

        marzban = MarzbanClient(settings)
        if hasattr(marzban, 'xray_restart_warning_text'):
            payload['restart_warning'] = marzban.xray_restart_warning_text()

        if hasattr(marzban, 'build_xray_fragment_preview'):
            fragment_preview = marzban.build_xray_fragment_preview(config_json, selected_node=selected_node)
            fragment_patch = getattr(fragment_preview, 'fragment_patch', fragment_preview)
            payload['xray_fragment_json'] = json.dumps(fragment_patch, ensure_ascii=False, indent=2, sort_keys=True)
            payload['restart_required'] = bool(getattr(fragment_preview, 'restart_required', False))
            payload['restart_warning'] = str(getattr(fragment_preview, 'restart_warning', None) or payload['restart_warning'])

        if hasattr(marzban, 'build_xray_apply_preview'):
            apply_preview = marzban.build_xray_apply_preview(
                base_config={},
                profile_config=config_json,
                selected_node=selected_node,
                target_config_path=getattr(settings, 'xray_config_path', None),
            )
            payload['validation_command'] = list(getattr(apply_preview, 'validation_command', []) or [])
            payload['restart_command'] = list(getattr(apply_preview, 'restart_command', []) or [])
            payload['restart_required'] = bool(getattr(apply_preview, 'restart_required', False) or payload['restart_required'])
            payload['restart_warning'] = str(getattr(apply_preview, 'restart_warning', None) or payload['restart_warning'])
            payload['xray_apply_preview_json'] = json.dumps(
                {
                    'target_config_path': getattr(apply_preview, 'target_config_path', None),
                    'backup_path': getattr(apply_preview, 'backup_path', None),
                    'validation_command': payload['validation_command'],
                    'restart_command': payload['restart_command'],
                    'merged_config': getattr(apply_preview, 'merged_config', {}),
                    'restart_required': payload['restart_required'],
                    'restart_warning': payload['restart_warning'],
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
    except Exception as exc:
        payload['error'] = str(exc)
    finally:
        if marzban is not None:
            with suppress(Exception):
                await marzban.close()

    return payload


def _parse_json_object_text(raw: str | None, *, field_label: str) -> dict[str, Any]:
    normalized = (raw or '').strip()
    if not normalized:
        return {}
    try:
        payload = json.loads(normalized)
    except json.JSONDecodeError as exc:
        raise ValueError(f'{field_label} должен быть валидным JSON-объектом') from exc
    if not isinstance(payload, dict):
        raise ValueError(f'{field_label} должен быть JSON-объектом')
    return payload


def _parse_csv_tags(raw: str | None) -> list[str]:
    normalized = (raw or '').strip()
    if not normalized:
        return []
    return [part.strip() for part in normalized.replace(';', ',').split(',') if part.strip()]


def _format_file_size(size_bytes: int | None) -> str | None:
    if size_bytes is None:
        return None
    size = float(size_bytes)
    units = ['B', 'KB', 'MB', 'GB']
    unit_index = 0
    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1
    return f'{size:.1f} {units[unit_index]}' if unit_index else f'{int(size)} {units[unit_index]}'


def _canonical_subscription_redirect_or_bot(settings: Settings, token: str | None) -> str:
    canonical = build_canonical_subscription_url(
        token,
        public_origin=configured_public_subscription_origin(settings),
    )
    if canonical:
        return canonical
    return _bot_redirect_url_or_raise(settings)


def _preview_subscription_stub():
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        service_id='PREVIEW01',
        expire_date=now,
        is_active=True,
        traffic_cycle_start_at=now,
        traffic_cycle_end_at=now,
        cycle_extra_traffic_bytes=0,
        used_traffic_bytes=0,
        monthly_traffic_bytes=50 * 1024 * 1024 * 1024,
        data_limit_bytes=50 * 1024 * 1024 * 1024,
        subscription_url=None,
    )


def _parse_support_status(raw: str | None) -> SupportTicketStatus | None:
    normalized = (raw or '').strip()
    if normalized == SupportTicketStatus.waiting_operator.value:
        return SupportTicketStatus.waiting_operator
    if normalized == SupportTicketStatus.waiting_user.value:
        return SupportTicketStatus.waiting_user
    if normalized == SupportTicketStatus.closed.value:
        return SupportTicketStatus.closed
    return None


def _support_status_label(status: SupportTicketStatus | str | None) -> str:
    raw = getattr(status, 'value', status)
    if raw == SupportTicketStatus.waiting_operator.value:
        return '🟠 Ожидает оператора'
    if raw == SupportTicketStatus.waiting_user.value:
        return '🔵 Ожидает пользователя'
    if raw == SupportTicketStatus.closed.value:
        return '🔴 Закрыт'
    return '—'


def _support_status_badge_tone(status: SupportTicketStatus | str | None) -> str:
    raw = getattr(status, 'value', status)
    if raw == SupportTicketStatus.waiting_operator.value:
        return 'amber'
    if raw == SupportTicketStatus.waiting_user.value:
        return 'cyan'
    if raw == SupportTicketStatus.closed.value:
        return 'rose'
    return 'slate'


def _support_actor_label(actor_type: str | None, actor_tg_id: int | None = None) -> str:
    normalized = (actor_type or '').strip().lower()
    if normalized == 'user':
        return f'User (TG {actor_tg_id})' if actor_tg_id is not None else 'User'
    if normalized == 'admin':
        return f'Оператор/админ (TG {actor_tg_id})' if actor_tg_id is not None else 'Оператор/админ'
    return '—'


def _parse_datetime_local(raw: str | None) -> datetime | None:
    normalized = (raw or '').strip()
    if not normalized:
        return None

    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=DISPLAY_TIMEZONE)
    return parsed.astimezone(timezone.utc)


def _datetime_local_input_value(value: datetime | None) -> str:
    if value is None:
        return ''
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    local_value = value.astimezone(DISPLAY_TIMEZONE)
    return local_value.strftime('%Y-%m-%dT%H:%M')


def _promo_admin_status(promo) -> str:
    try:
        return PromoService.resolve_admin_status(promo)
    except Exception:  # pragma: no cover - defensive fallback
        if not getattr(promo, 'is_active', False):
            return 'archived'
        expires_at = getattr(promo, 'expires_at', None)
        if expires_at is not None:
            comparable = expires_at
            if comparable.tzinfo is None:
                comparable = comparable.replace(tzinfo=timezone.utc)
            else:
                comparable = comparable.astimezone(timezone.utc)
            if comparable <= datetime.now(timezone.utc):
                return 'expired'
        max_uses = getattr(promo, 'max_uses', None)
        used_count = int(getattr(promo, 'used_count', 0) or 0)
        if max_uses is not None and used_count >= max_uses:
            return 'exhausted'
        return 'active'


def _promo_status_label(status: str | None) -> str:
    normalized = (status or '').strip().lower()
    if normalized == 'active':
        return 'Активен'
    if normalized == 'archived':
        return 'В архиве'
    if normalized == 'expired':
        return 'Истёк'
    if normalized == 'exhausted':
        return 'Лимит исчерпан'
    return '—'


def _promo_status_badge_tone(status: str | None) -> str:
    normalized = (status or '').strip().lower()
    if normalized == 'active':
        return 'emerald'
    if normalized == 'archived':
        return 'slate'
    if normalized == 'expired':
        return 'amber'
    if normalized == 'exhausted':
        return 'violet'
    return 'slate'


def _normalize_promo_status_filter(raw: str | None) -> str:
    normalized = (raw or 'all').strip().lower()
    if normalized in {'all', 'active', 'archived', 'expired', 'exhausted'}:
        return normalized
    return 'all'


def _promo_matches_status_filter(promo, status_filter: str) -> bool:
    normalized = _normalize_promo_status_filter(status_filter)
    if normalized == 'all':
        return True
    return _promo_admin_status(promo) == normalized


def _promo_matches_query(promo, query: str | None) -> bool:
    normalized = (query or '').strip().upper()
    if not normalized:
        return True
    return normalized in str(getattr(promo, 'code', '') or '').upper()


def _promo_snapshot_for_audit(promo) -> dict[str, object]:
    status = _promo_admin_status(promo)
    return {
        'code': getattr(promo, 'code', None),
        'bonus_amount': str(getattr(promo, 'bonus_amount', '0')),
        'max_uses': getattr(promo, 'max_uses', None),
        'used_count': int(getattr(promo, 'used_count', 0) or 0),
        'expires_at': getattr(promo, 'expires_at', None).isoformat() if getattr(promo, 'expires_at', None) else None,
        'is_active': bool(getattr(promo, 'is_active', False)),
        'admin_status': status,
    }


async def _promo_list_recent_filtered(repo: PromoRepository, *, limit: int, offset: int, status_filter: str, query: str | None):
    normalized_status = _normalize_promo_status_filter(status_filter)
    normalized_query = (query or '').strip() or None

    try:
        signature = inspect.signature(repo.list_recent)
        kwargs = {'limit': limit, 'offset': offset}
        if 'status_filter' in signature.parameters:
            kwargs['status_filter'] = normalized_status
        if 'query' in signature.parameters:
            kwargs['query'] = normalized_query
        if len(kwargs) > 2:
            return await repo.list_recent(**kwargs)
    except (TypeError, ValueError):
        pass

    promos = await repo.list_all()
    filtered = [
        promo
        for promo in promos
        if _promo_matches_status_filter(promo, normalized_status) and _promo_matches_query(promo, normalized_query)
    ]
    return filtered[offset : offset + limit]


async def _promo_count_filtered(repo: PromoRepository, *, status_filter: str, query: str | None) -> int:
    normalized_status = _normalize_promo_status_filter(status_filter)
    normalized_query = (query or '').strip() or None

    try:
        signature = inspect.signature(repo.count)
        kwargs = {}
        if 'status_filter' in signature.parameters:
            kwargs['status_filter'] = normalized_status
        if 'query' in signature.parameters:
            kwargs['query'] = normalized_query
        if kwargs:
            return int(await repo.count(**kwargs))
    except (TypeError, ValueError):
        pass

    promos = await repo.list_all()
    return sum(
        1
        for promo in promos
        if _promo_matches_status_filter(promo, normalized_status) and _promo_matches_query(promo, normalized_query)
    )


async def _promo_summary_counts(repo: PromoRepository) -> dict[str, int]:
    promos = await repo.list_all()
    counts = {
        'total': len(promos),
        'active': 0,
        'archived': 0,
        'expired': 0,
        'exhausted': 0,
    }
    for promo in promos:
        status = _promo_admin_status(promo)
        counts[status] = counts.get(status, 0) + 1
    return counts


def _promo_edit_form_defaults(promo) -> dict[str, object]:
    return {
        'promo_id': getattr(promo, 'id', None),
        'code': getattr(promo, 'code', '') or '',
        'bonus_amount': str(getattr(promo, 'bonus_amount', '') or ''),
        'max_uses': getattr(promo, 'max_uses', None),
        'expires_at': _datetime_local_input_value(getattr(promo, 'expires_at', None)),
        'is_active': bool(getattr(promo, 'is_active', False)),
    }


def _normalize_broadcast_status_filter(raw: str | None) -> str:
    normalized = (raw or 'all').strip().lower()
    if normalized == 'pending':
        normalized = 'scheduled'
    if normalized in {'all', 'draft', 'scheduled', 'running', 'completed', 'failed', 'cancelled'}:
        return normalized
    return 'all'


def _broadcast_status_label(status: str | BroadcastJobStatus | None) -> str:
    normalized = getattr(status, 'value', status) or ''
    normalized = str(normalized).strip().lower()
    if normalized == 'pending':
        normalized = 'scheduled'
    if normalized == 'draft':
        return 'Черновик'
    if normalized == 'scheduled':
        return 'Запланирована'
    if normalized == 'running':
        return 'Выполняется'
    if normalized == 'completed':
        return 'Завершена'
    if normalized == 'failed':
        return 'Ошибка'
    if normalized == 'cancelled':
        return 'Отменена'
    return '—'


def _broadcast_status_badge_tone(status: str | BroadcastJobStatus | None) -> str:
    normalized = getattr(status, 'value', status) or ''
    normalized = str(normalized).strip().lower()
    if normalized == 'pending':
        normalized = 'scheduled'
    if normalized == 'draft':
        return 'slate'
    if normalized == 'scheduled':
        return 'amber'
    if normalized == 'running':
        return 'cyan'
    if normalized == 'completed':
        return 'emerald'
    if normalized == 'failed':
        return 'rose'
    if normalized == 'cancelled':
        return 'slate'
    return 'slate'


def _broadcast_matches_status_filter(job, status_filter: str) -> bool:
    normalized = _normalize_broadcast_status_filter(status_filter)
    if normalized == 'all':
        return True
    job_status = getattr(getattr(job, 'status', None), 'value', getattr(job, 'status', None)) or ''
    job_status = str(job_status).strip().lower()
    if job_status == 'pending':
        job_status = 'scheduled'
    return job_status == normalized


async def _broadcast_list_recent_filtered(repo: BroadcastJobRepository, *, limit: int, offset: int, status_filter: str):
    normalized_status = _normalize_broadcast_status_filter(status_filter)
    try:
        signature = inspect.signature(repo.list_recent)
        kwargs = {'limit': limit, 'offset': offset}
        if 'status_filter' in signature.parameters:
            kwargs['status_filter'] = normalized_status
        return await repo.list_recent(**kwargs)
    except (TypeError, ValueError):
        pass

    jobs = await repo.list_recent(limit=max(limit + offset, limit))
    filtered = [job for job in jobs if _broadcast_matches_status_filter(job, normalized_status)]
    return filtered[offset : offset + limit]


async def _broadcast_count_filtered(repo: BroadcastJobRepository, *, status_filter: str) -> int:
    normalized_status = _normalize_broadcast_status_filter(status_filter)
    try:
        signature = inspect.signature(repo.count)
        kwargs = {}
        if 'status_filter' in signature.parameters:
            kwargs['status_filter'] = normalized_status
        if kwargs:
            return int(await repo.count(**kwargs))
    except (TypeError, ValueError):
        pass

    jobs = await repo.list_recent(limit=1000, offset=0)
    return sum(1 for job in jobs if _broadcast_matches_status_filter(job, normalized_status))


async def _broadcast_summary_counts(repo: BroadcastJobRepository) -> dict[str, int]:
    counts = {
        'total': 0,
        'draft': 0,
        'scheduled': 0,
        'running': 0,
        'completed': 0,
        'failed': 0,
        'cancelled': 0,
        'active': 0,
    }
    try:
        counts['total'] = int(await _broadcast_count_filtered(repo, status_filter='all'))
        for key in ('draft', 'scheduled', 'running', 'completed', 'failed', 'cancelled'):
            counts[key] = int(await _broadcast_count_filtered(repo, status_filter=key))
        counts['active'] = counts['scheduled'] + counts['running']
        return counts
    except Exception:
        jobs = await repo.list_recent(limit=1000, offset=0)
        counts['total'] = len(jobs)
        for job in jobs:
            status = getattr(getattr(job, 'status', None), 'value', getattr(job, 'status', None)) or ''
            status = str(status).strip().lower()
            if status == 'pending':
                status = 'scheduled'
            if status in counts:
                counts[status] += 1
        counts['active'] = counts['scheduled'] + counts['running']
        return counts


def _broadcast_keyboard_json_pretty(job) -> str:
    keyboard_json = getattr(job, 'keyboard_json', None) or []
    if not keyboard_json:
        return ''
    try:
        return json.dumps(keyboard_json, ensure_ascii=False, indent=2)
    except Exception:
        return ''


def _broadcast_edit_form_defaults(job) -> dict[str, object]:
    status_value = getattr(getattr(job, 'status', None), 'value', getattr(job, 'status', None)) or 'scheduled'
    if status_value == 'pending':
        status_value = 'scheduled'
    return {
        'job_id': getattr(job, 'id', None),
        'text_value': getattr(job, 'text', '') or '',
        'run_at': _datetime_local_input_value(getattr(job, 'run_at', None)),
        'photo_file_id': getattr(job, 'photo_file_id', '') or '',
        'photo_file_unique_id': getattr(job, 'photo_file_unique_id', '') or '',
        'keyboard_json': _broadcast_keyboard_json_pretty(job),
        'status': status_value,
    }


def _broadcast_row(job) -> dict[str, object]:
    status_value = getattr(getattr(job, 'status', None), 'value', getattr(job, 'status', None)) or ''
    if status_value == 'pending':
        status_value = 'scheduled'
    preview = getattr(job, 'content_preview_text', None) or getattr(job, 'text', None) or ''
    if not (preview or '').strip() and getattr(job, 'photo_file_id', None):
        preview = 'Фото без подписи'
    return {
        'job': job,
        'id': getattr(job, 'id', None),
        'status': status_value,
        'status_label': _broadcast_status_label(status_value),
        'status_tone': _broadcast_status_badge_tone(status_value),
        'run_at_label': format_dt(getattr(job, 'run_at', None)) if getattr(job, 'run_at', None) else '—',
        'preview_text': preview,
        'has_media': bool(getattr(job, 'photo_file_id', None)),
        'has_keyboard': bool(getattr(job, 'keyboard_json', None)),
        'is_editable': bool(getattr(job, 'is_editable', False)),
        'can_request_cancel': bool(getattr(job, 'can_request_cancel', False)),
        'is_terminal': bool(getattr(job, 'is_terminal', False)),
        'cancel_requested': bool(getattr(job, 'cancel_requested_at', None)),
    }


def _invoice_status_label(status: str | InvoiceStatus | None) -> str:
    normalized = getattr(status, 'value', status) or ''
    normalized = str(normalized).strip().lower()
    if normalized == 'pending':
        return 'Ожидает оплаты'
    if normalized == 'paid':
        return 'Оплачен'
    if normalized == 'applying':
        return 'Применяется'
    if normalized == 'consumed':
        return 'Применён'
    if normalized == 'cancelled':
        return 'Отменён'
    return '—'


def _invoice_status_tone(status: str | InvoiceStatus | None) -> str:
    normalized = getattr(status, 'value', status) or ''
    normalized = str(normalized).strip().lower()
    if normalized == 'pending':
        return '#f59e0b'
    if normalized == 'paid':
        return '#22c55e'
    if normalized == 'applying':
        return '#06b6d4'
    if normalized == 'consumed':
        return '#10b981'
    if normalized == 'cancelled':
        return '#ef4444'
    return '#94a3b8'


def _invoice_status_filter_normalize(raw: str | None) -> str:
    value = (raw or 'all').strip().lower()
    return value if value in {'all', 'pending', 'paid', 'applying', 'consumed', 'cancelled'} else 'all'


def _invoice_matches_query(invoice, query: str) -> bool:
    normalized = (query or '').strip().lower()
    if not normalized:
        return True
    haystack = [
        str(getattr(invoice, 'id', '')),
        str(getattr(invoice, 'user_id', '')),
        str(getattr(getattr(invoice, 'purpose', None), 'value', getattr(invoice, 'purpose', ''))),
        str(getattr(getattr(invoice, 'status', None), 'value', getattr(invoice, 'status', ''))),
        str(getattr(invoice, 'provider', '') or ''),
        str(getattr(invoice, 'external_invoice_id', '') or ''),
        str(getattr(invoice, 'payment_url', '') or ''),
        str(getattr(invoice, 'tariff_plan_id', '') or ''),
    ]
    return normalized in ' '.join(h.lower() for h in haystack)


def _invoice_matches_status(invoice, status_filter: str) -> bool:
    normalized = _invoice_status_filter_normalize(status_filter)
    if normalized == 'all':
        return True
    status = str(getattr(getattr(invoice, 'status', None), 'value', getattr(invoice, 'status', None)) or '').strip().lower()
    return status == normalized


def _invoice_pretty_json(value: object) -> str:
    try:
        return json.dumps(value or {}, ensure_ascii=False, indent=2, sort_keys=True)
    except Exception:
        return '{}'


def _invoice_money_label(value: object, currency: str | None = None) -> str:
    suffix = f' {currency}' if currency else ''
    return f'{value}{suffix}'


def _safe_return_to(return_to: str | None, *, fallback: str) -> str:
    candidate = (return_to or '').strip()
    if candidate.startswith('/') and not candidate.startswith('//'):
        return candidate
    return fallback


async def _fetch_invoice_provider_diagnostic(settings: Settings, invoice) -> dict[str, Any]:
    external_invoice_id = str(getattr(invoice, 'external_invoice_id', '') or '').strip()
    provider_name = str(getattr(invoice, 'provider', '') or '').strip().lower() or settings.payment_provider
    if not external_invoice_id:
        return {
            'provider': provider_name or 'unknown',
            'available': False,
            'reason': 'У счета ещё нет external invoice id.',
            'status': None,
            'raw_status': None,
            'external_invoice_id': None,
            'payment_url': getattr(invoice, 'payment_url', None),
            'raw': None,
        }

    payments = _build_payment_provider(settings, provider_name=provider_name)
    try:
        snapshot_getter = getattr(payments, 'get_transaction_snapshot', None)
        if callable(snapshot_getter):
            snapshot = await snapshot_getter(external_invoice_id)
            return {
                'provider': provider_name,
                'available': True,
                'reason': None,
                'status': str(getattr(snapshot, 'status', None) or '').strip().lower() or None,
                'raw_status': getattr(snapshot, 'raw_status', None),
                'external_invoice_id': str(getattr(snapshot, 'transaction_id', None) or external_invoice_id),
                'payment_url': getattr(snapshot, 'payment_url', None) or getattr(invoice, 'payment_url', None),
                'raw': getattr(snapshot, 'raw', None),
            }

        status = await payments.get_status(external_invoice_id)
        return {
            'provider': provider_name,
            'available': True,
            'reason': None,
            'status': str(status or '').strip().lower() or None,
            'raw_status': status,
            'external_invoice_id': external_invoice_id,
            'payment_url': getattr(invoice, 'payment_url', None),
            'raw': {'status': status},
        }
    except Exception as exc:  # pragma: no cover - runtime safety
        logger.exception('Failed to fetch provider diagnostic for invoice id=%s provider=%s external_id=%s', getattr(invoice, 'id', None), provider_name, external_invoice_id)
        return {
            'provider': provider_name,
            'available': False,
            'reason': str(exc),
            'status': None,
            'raw_status': None,
            'external_invoice_id': external_invoice_id,
            'payment_url': getattr(invoice, 'payment_url', None),
            'raw': None,
        }
    finally:
        close = getattr(payments, 'close', None)
        if callable(close):
            with suppress(Exception):
                await close()


def _invoice_history_pretty_rows(rows: list[AuditLog]) -> list[dict[str, str]]:
    prepared: list[dict[str, str]] = []
    for row in rows:
        prepared.append({
            'created_at': format_dt(getattr(row, 'created_at', None)),
            'action': getattr(getattr(row, 'action', None), 'value', getattr(row, 'action', None)) or '—',
            'actor_type': getattr(getattr(row, 'actor_type', None), 'value', getattr(row, 'actor_type', None)) or '—',
            'actor_tg_id': str(getattr(row, 'actor_tg_id', '') or '—'),
            'details': _invoice_pretty_json(getattr(row, 'details', None) or {}),
        })
    return prepared


def _invoice_detail_html(*, invoice, user, provider_diag: dict[str, Any], audit_rows: list[AuditLog], success_message: str | None, error_message: str | None) -> str:
    status_value = getattr(getattr(invoice, 'status', None), 'value', getattr(invoice, 'status', None))
    status_label = _invoice_status_label(status_value)
    status_tone = _invoice_status_tone(status_value)
    provider_status_label = _invoice_status_label(provider_diag.get('status')) if provider_diag.get('status') else '—'
    provider_status_tone = _invoice_status_tone(provider_diag.get('status')) if provider_diag.get('status') else '#94a3b8'
    purpose_value = getattr(getattr(invoice, 'purpose', None), 'value', getattr(invoice, 'purpose', None)) or '—'
    payload_pretty = html.escape(_invoice_pretty_json(getattr(invoice, 'payload_json', None) or {}))
    snapshot_pretty = html.escape(_invoice_pretty_json(getattr(invoice, 'tariff_snapshot_json', None) or {}))
    provider_raw_pretty = html.escape(_invoice_pretty_json(provider_diag.get('raw') or {}))
    history_html = ''.join(
        f"<details style='margin-bottom:12px;border:1px solid #334155;border-radius:12px;padding:12px;background:#0f172a;'><summary style='cursor:pointer;color:#e2e8f0;font-weight:600'>{html.escape(item['created_at'])} · {html.escape(item['action'])} · {html.escape(item['actor_type'])} ({html.escape(item['actor_tg_id'])})</summary><pre style='margin-top:12px;white-space:pre-wrap;color:#cbd5e1;font-size:12px'>{html.escape(item['details'])}</pre></details>"
        for item in _invoice_history_pretty_rows(audit_rows)
    ) or "<div style='color:#94a3b8'>Аудит по счету пока отсутствует.</div>"

    success_block = f"<div style='margin-bottom:16px;padding:12px 14px;border-radius:12px;background:#052e16;color:#bbf7d0;border:1px solid #166534'>{html.escape(success_message)}</div>" if success_message else ''
    error_block = f"<div style='margin-bottom:16px;padding:12px 14px;border-radius:12px;background:#450a0a;color:#fecaca;border:1px solid #991b1b'>{html.escape(error_message)}</div>" if error_message else ''
    payment_url = getattr(invoice, 'payment_url', None) or provider_diag.get('payment_url')
    payment_link = f"<a href='{html.escape(payment_url)}' target='_blank' rel='noopener noreferrer' style='color:#67e8f9'>Открыть платежную ссылку</a>" if payment_url else '—'
    user_label = f"ID {getattr(user, 'id', '—')} / tg_id {getattr(user, 'tg_id', '—')}" if user is not None else 'User не найден'
    provider_reason = provider_diag.get('reason') or 'Провайдер ответил успешно.'
    return f"""<!doctype html>
<html lang='ru'>
<head>
  <meta charset='utf-8' />
  <title>Invoice #{getattr(invoice, 'id', '—')}</title>
  <style>
    body {{ background:#020617; color:#e2e8f0; font-family:Inter,Arial,sans-serif; margin:0; padding:24px; }}
    .wrap {{ max-width:1180px; margin:0 auto; }}
    .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:16px; }}
    .card {{ background:#0f172a; border:1px solid #1e293b; border-radius:16px; padding:18px; }}
    .muted {{ color:#94a3b8; font-size:13px; }}
    .value {{ margin-top:8px; font-size:18px; color:#fff; word-break:break-word; }}
    .actions {{ display:flex; gap:12px; flex-wrap:wrap; margin:20px 0; }}
    .btn, button {{ border:none; border-radius:12px; padding:10px 14px; font-weight:600; cursor:pointer; }}
    .btn-primary {{ background:#0891b2; color:white; }}
    .btn-danger {{ background:#b91c1c; color:white; }}
    .btn-ghost {{ background:#1e293b; color:#e2e8f0; text-decoration:none; display:inline-flex; align-items:center; }}
    pre {{ margin:0; white-space:pre-wrap; word-break:break-word; }}
    .badge {{ display:inline-flex; padding:6px 10px; border-radius:999px; font-size:12px; font-weight:700; }}
  </style>
</head>
<body>
  <div class='wrap'>
    <div style='display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:18px;'>
      <div>
        <div class='muted'>Диагностика платежа</div>
        <h1 style='margin:6px 0 0;font-size:30px;'>Счёт #{getattr(invoice, 'id', '—')}</h1>
      </div>
      <a class='btn btn-ghost' href='/admin/invoices/'>← К списку счетов</a>
    </div>
    {success_block}
    {error_block}
    <div class='actions'>
      <form method='post' action='/admin/invoices/{getattr(invoice, 'id', 0)}/refresh'>
        <button class='btn btn-primary' type='submit'>Проверить статус у провайдера</button>
      </form>
      <form method='post' action='/admin/invoices/{getattr(invoice, 'id', 0)}/approve' onsubmit="return confirm('Подтвердить и применить счёт вручную?');">
        <button class='btn btn-primary' type='submit'>Подтвердить вручную</button>
      </form>
      <form method='post' action='/admin/invoices/{getattr(invoice, 'id', 0)}/cancel' onsubmit="return confirm('Отменить счёт?');">
        <button class='btn btn-danger' type='submit'>Отменить счёт</button>
      </form>
      <a class='btn btn-ghost' href='/admin/export/invoices.csv'>Экспорт счетов (CSV)</a>
    </div>
    <div class='grid'>
      <section class='card'><div class='muted'>Внутренний статус</div><div class='value'><span class='badge' style='background:{status_tone}22;color:{status_tone};border:1px solid {status_tone}55'>{html.escape(status_label)}</span></div></section>
      <section class='card'><div class='muted'>Внешний статус провайдера</div><div class='value'><span class='badge' style='background:{provider_status_tone}22;color:{provider_status_tone};border:1px solid {provider_status_tone}55'>{html.escape(provider_status_label)}</span></div><div class='muted' style='margin-top:10px'>{html.escape(str(provider_reason))}</div></section>
      <section class='card'><div class='muted'>External invoice id</div><div class='value'>{html.escape(str(getattr(invoice, 'external_invoice_id', None) or '—'))}</div></section>
      <section class='card'><div class='muted'>User</div><div class='value'>{html.escape(user_label)}</div></section>
      <section class='card'><div class='muted'>Провайдер</div><div class='value'>{html.escape(str(getattr(invoice, 'provider', '—') or '—'))}</div></section>
      <section class='card'><div class='muted'>Назначение</div><div class='value'>{html.escape(str(purpose_value))}</div></section>
      <section class='card'><div class='muted'>Сумма счёта</div><div class='value'>{html.escape(_invoice_money_label(getattr(invoice, 'amount', '—'), getattr(invoice, 'currency', None)))}</div></section>
      <section class='card'><div class='muted'>К оплате</div><div class='value'>{html.escape(_invoice_money_label(getattr(invoice, 'payable_amount', '—'), getattr(invoice, 'currency', None)))}</div></section>
      <section class='card'><div class='muted'>Использовано баланса</div><div class='value'>{html.escape(_invoice_money_label(getattr(invoice, 'balance_used', '—'), getattr(invoice, 'currency', None)))}</div></section>
      <section class='card'><div class='muted'>Платёжная ссылка</div><div class='value'>{payment_link}</div></section>
      <section class='card'><div class='muted'>Создан</div><div class='value'>{html.escape(format_dt(getattr(invoice, 'created_at', None)))}</div></section>
      <section class='card'><div class='muted'>Оплачен / применён</div><div class='value'>{html.escape(format_dt(getattr(invoice, 'paid_at', None)))} / {html.escape(format_dt(getattr(invoice, 'consumed_at', None)))}</div></section>
    </div>
    <div class='grid' style='margin-top:18px;'>
      <section class='card'>
        <h2 style='margin-top:0;'>Payload JSON</h2>
        <pre>{payload_pretty}</pre>
      </section>
      <section class='card'>
        <h2 style='margin-top:0;'>Tariff snapshot JSON</h2>
        <pre>{snapshot_pretty}</pre>
      </section>
      <section class='card'>
        <h2 style='margin-top:0;'>Провайдер raw snapshot</h2>
        <pre>{provider_raw_pretty}</pre>
      </section>
    </div>
    <section class='card' style='margin-top:18px;'>
      <h2 style='margin-top:0;'>Audit history</h2>
      {history_html}
    </section>
  </div>
</body>
</html>"""


def _invoice_list_html(*, rows: list[dict[str, Any]], status_filter: str, query: str, success_message: str | None, error_message: str | None, page: int, has_next_page: bool) -> str:
    success_block = f"<div style='margin-bottom:16px;padding:12px 14px;border-radius:12px;background:#052e16;color:#bbf7d0;border:1px solid #166534'>{html.escape(success_message)}</div>" if success_message else ''
    error_block = f"<div style='margin-bottom:16px;padding:12px 14px;border-radius:12px;background:#450a0a;color:#fecaca;border:1px solid #991b1b'>{html.escape(error_message)}</div>" if error_message else ''
    body_rows = ''.join(
        f"<tr><td style='padding:10px;border-top:1px solid #1e293b'>#{row['id']}</td><td style='padding:10px;border-top:1px solid #1e293b'>{html.escape(str(row['user_id']))}</td><td style='padding:10px;border-top:1px solid #1e293b'>{html.escape(str(row['purpose']))}</td><td style='padding:10px;border-top:1px solid #1e293b'><span style='color:{row['status_tone']};font-weight:700'>{html.escape(row['status_label'])}</span></td><td style='padding:10px;border-top:1px solid #1e293b'>{html.escape(str(row['provider']))}</td><td style='padding:10px;border-top:1px solid #1e293b'>{html.escape(str(row['external_invoice_id']))}</td><td style='padding:10px;border-top:1px solid #1e293b'>{html.escape(str(row['payable_amount']))}</td><td style='padding:10px;border-top:1px solid #1e293b'>{html.escape(row['created_at'])}</td><td style='padding:10px;border-top:1px solid #1e293b'><a href='/admin/invoices/{row['id']}' style='color:#67e8f9'>Открыть</a></td></tr>"
        for row in rows
    ) or "<tr><td colspan='9' style='padding:16px;color:#94a3b8'>Счета не найдены.</td></tr>"
    prev_link = f"/admin/invoices/?page={page - 1}&status={quote_plus(status_filter)}&q={quote_plus(query)}" if page > 1 else None
    next_link = f"/admin/invoices/?page={page + 1}&status={quote_plus(status_filter)}&q={quote_plus(query)}" if has_next_page else None
    return f"""<!doctype html>
<html lang='ru'>
<head><meta charset='utf-8' /><title>Счета</title><style>body{{background:#020617;color:#e2e8f0;font-family:Inter,Arial,sans-serif;margin:0;padding:24px}}.wrap{{max-width:1280px;margin:0 auto}}.card{{background:#0f172a;border:1px solid #1e293b;border-radius:16px;padding:18px}}input,select{{background:#020617;color:#e2e8f0;border:1px solid #334155;border-radius:10px;padding:10px 12px}}button,a.btn{{background:#0891b2;color:#fff;border:none;border-radius:10px;padding:10px 14px;text-decoration:none;font-weight:600}}</style></head>
<body><div class='wrap'><h1 style='margin:0 0 18px'>Диагностика счетов</h1>{success_block}{error_block}<form method='get' class='card' style='display:flex;gap:12px;flex-wrap:wrap;margin-bottom:18px'><input type='text' name='q' value='{html.escape(query)}' placeholder='invoice_id / user_id / внешний id' style='min-width:280px'><select name='status'><option value='all'{' selected' if status_filter=='all' else ''}>Все статусы</option><option value='pending'{' selected' if status_filter=='pending' else ''}>Ожидает оплаты</option><option value='paid'{' selected' if status_filter=='paid' else ''}>Оплачен</option><option value='applying'{' selected' if status_filter=='applying' else ''}>Применяется</option><option value='consumed'{' selected' if status_filter=='consumed' else ''}>Применён</option><option value='cancelled'{' selected' if status_filter=='cancelled' else ''}>Отменён</option></select><button type='submit'>Фильтровать</button><a class='btn' href='/admin/export/invoices.csv'>Экспорт счетов (CSV)</a></form><div class='card'><table style='width:100%;border-collapse:collapse'><thead><tr style='text-align:left;color:#94a3b8'><th style='padding:10px'>ID</th><th style='padding:10px'>User</th><th style='padding:10px'>Назначение</th><th style='padding:10px'>Статус</th><th style='padding:10px'>Провайдер</th><th style='padding:10px'>Внешний ID</th><th style='padding:10px'>К оплате</th><th style='padding:10px'>Создан</th><th style='padding:10px'>Действие</th></tr></thead><tbody>{body_rows}</tbody></table><div style='display:flex;justify-content:space-between;align-items:center;margin-top:16px'>{f"<a class='btn' href='{html.escape(prev_link)}'>← Назад</a>" if prev_link else '<span></span>'}{f"<a class='btn' href='{html.escape(next_link)}'>Вперёд →</a>" if next_link else '<span></span>'}</div></div></div></body></html>"""


def _coerce_optional_form_int(value: str | int | None, *, field_label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        if value < 1:
            raise ValueError(f'{field_label} должно быть не меньше 1.')
        return value
    normalized = value.strip()
    if not normalized:
        return None
    try:
        parsed = int(normalized)
    except ValueError as exc:
        raise ValueError(f'{field_label} должно быть целым числом.') from exc
    if parsed < 1:
        raise ValueError(f'{field_label} должно быть не меньше 1.')
    return parsed


def _bool_query(value: str | None) -> bool:
    return (value or '').strip().lower() in {'1', 'true', 'yes', 'on'}


def _get_bot_from_request(request: Request):
    return (
        getattr(request.app.state, 'bot', None)
        or getattr(request.app.state, 'telegram_bot', None)
        or getattr(request.app.state, 'main_bot', None)
    )

def _bot_username_or_none(settings: Settings) -> str | None:
    username = (getattr(settings, 'bot_username', None) or '').strip()
    if not username:
        return None
    return username.lstrip('@')

def _bot_redirect_url_or_raise(settings: Settings) -> str:
    username = _bot_username_or_none(settings)
    if not username:
        raise HTTPException(status_code=503, detail='Bot username is not configured')
    return f'https://t.me/{username}'


async def _maybe_call(value):
    if inspect.isawaitable(value):
        return await value
    return value


async def _redis_health(request: Request) -> tuple[bool, str | None]:
    redis_client = (
        getattr(request.app.state, 'redis', None)
        or getattr(request.app.state, 'redis_client', None)
        or getattr(request.app.state, 'cache', None)
    )
    if redis_client is None:
        return False, 'Redis-клиент не привязан к app.state'
    ping = getattr(redis_client, 'ping', None)
    if ping is None or not callable(ping):
        return False, 'У Redis-клиента нет метода ping()'
    try:
        result = await _maybe_call(ping())
        return bool(result), None if result else 'Redis ping вернул ложное значение'
    except Exception as exc:
        return False, str(exc)


async def _marzban_health(settings: Settings) -> tuple[bool, str | None]:
    if not getattr(settings, 'marzban_enabled', False):
        return False, 'Marzban отключён'
    client = MarzbanClient(settings)
    try:
        await client._ensure_token()  # noqa: SLF001
        return True, None
    except Exception as exc:
        message = str(exc)
        base_url = (getattr(settings, 'marzban_api_base_url', None) or '').strip()
        lowered = message.lower()
        if 'connection refused' in lowered or 'all connection attempts failed' in lowered:
            if 'host.docker.internal' in base_url:
                message = (
                    f'{message}. Проверьте внутренний доступ bot → Marzban: '
                    'если Marzban работает только на localhost, потребуется внутренний proxy или иной доступный для контейнера адрес. '
                    'Для Marzban это часто происходит в localhost-only режиме без UVICORN_SSL_CERTFILE/UVICORN_SSL_KEYFILE.'
                )
            elif '127.0.0.1' in base_url or 'localhost' in base_url:
                message = (
                    f'{message}. MARZBAN_API_BASE_URL указывает на localhost/127.0.0.1; '
                    'для Docker-контейнера bot это обычно означает сам контейнер, а не хост или отдельный сервис Marzban.'
                )
        return False, message
    finally:
        with suppress(Exception):
            await client.close()


async def _build_health_snapshot(request: Request, session: AsyncSession, app_settings) -> dict[str, dict[str, object]]:
    health: dict[str, dict[str, object]] = {}

    try:
        await session.execute(select(text('1')))
        health['db'] = {'ok': True, 'message': 'OK'}
    except Exception as exc:
        health['db'] = {'ok': False, 'message': str(exc)}

    redis_ok, redis_message = await _redis_health(request)
    health['redis'] = {'ok': redis_ok, 'message': redis_message or ('OK' if redis_ok else 'Недоступно')}

    bot = _get_bot_from_request(request)
    health['bot'] = {'ok': bot is not None, 'message': 'Подключён' if bot is not None else 'Bot не привязан к app.state'}

    scheduler = getattr(request.app.state, 'scheduler', None)
    scheduler_state_info = getattr(request.app.state, 'scheduler_state_info', None) or {}
    scheduler_running = bool(getattr(scheduler, 'running', False) or scheduler_state_info.get('running'))
    scheduler_message = scheduler_state_info.get('message') or ('Работает' if scheduler_running else 'Остановлен')
    owner = scheduler_state_info.get('lock_owner')
    ttl = scheduler_state_info.get('lock_ttl_seconds')
    reclaimed = bool(scheduler_state_info.get('reclaimed_stale_lock'))
    scheduler_suffix_parts: list[str] = []
    if owner:
        scheduler_suffix_parts.append(f'owner={owner}')
    if ttl is not None:
        scheduler_suffix_parts.append(f'ttl={ttl}s')
    if reclaimed:
        scheduler_suffix_parts.append('устаревший lock возвращён')
    if scheduler_suffix_parts:
        scheduler_message = f"{scheduler_message} ({', '.join(scheduler_suffix_parts)})"
    health['scheduler'] = {
        'ok': scheduler_running,
        'message': scheduler_message,
        'state': scheduler_state_info.get('state') or ('running' if scheduler_running else 'stopped'),
    }

    settings = request.app.state.settings
    webhook_enabled = bool(
        getattr(settings, 'webhook_enabled', False)
        or getattr(settings, 'use_webhook', False)
        or getattr(settings, 'telegram_use_webhook', False)
    )
    health['webhook'] = {'ok': webhook_enabled, 'message': 'Включён' if webhook_enabled else 'Отключён'}

    web_admin_access_ok, web_admin_access_message, web_admin_access_state = _web_admin_access_health(settings)
    health['web_admin_access'] = {
        'ok': web_admin_access_ok,
        'message': web_admin_access_message,
        'state': web_admin_access_state,
    }

    marzban_ok, marzban_message = await _marzban_health(settings)
    access_ok, access_message, access_state = _marzban_access_health(settings)
    combined_marzban_message = marzban_message or ('OK' if marzban_ok else 'Недоступно')
    if access_message:
        combined_marzban_message = f"{combined_marzban_message}\n\nРежим доступа: {access_message}"
    health['marzban'] = {
        'ok': marzban_ok,
        'message': combined_marzban_message,
        'state': 'ok' if marzban_ok else 'error',
    }
    health['marzban_access'] = {
        'ok': access_ok,
        'message': access_message,
        'state': access_state,
    }

    support_status = getattr(app_settings, 'support_chat_test_last_status', 'never')
    support_message = getattr(app_settings, 'support_chat_test_last_error', None) or support_status
    support_delivery_ok, support_delivery_message, support_delivery_state = _support_delivery_health(app_settings, settings)
    if support_delivery_ok and support_message and support_message != 'ok':
        support_message = f"{support_message}\n\nDelivery fallback: {support_delivery_message}"
    health['support_chat'] = {
        'ok': support_status == 'ok',
        'message': support_message,
        'state': support_status,
    }
    health['support_delivery'] = {
        'ok': support_delivery_ok,
        'message': support_delivery_message,
        'state': support_delivery_state,
    }

    return health


async def _list_active_tariffs_for_preview(session: AsyncSession) -> list[object]:
    repo = TariffRepository(session)
    list_public_active = getattr(repo, 'list_public_active', None)
    if callable(list_public_active):
        return list(await list_public_active())
    list_active = getattr(repo, 'list_active', None)
    if callable(list_active):
        return list(await list_active())
    list_all = getattr(repo, 'list_all', None)
    if callable(list_all):
        return [plan for plan in await list_all() if bool(getattr(plan, 'is_active', True))]
    return []


async def _build_pricing_preview(session: AsyncSession, trial_settings) -> list[dict[str, object]]:
    tariffs = await _list_active_tariffs_for_preview(session)
    preview_rows: list[dict[str, object]] = []
    months_options = (1, 3, 6)

    for tariff in tariffs[:3]:
        for months in months_options:
            try:
                basket = await PricingService.calculate_tariff_basket(
                    session=session,
                    plan_code=getattr(tariff, 'code', ''),
                    months=months,
                    user_balance=Decimal('0.00'),
                    use_balance=False,
                    device_mode='single',
                    device_count=1,
                )
            except Exception:
                continue
            preview_rows.append(
                {
                    'tariff_code': getattr(tariff, 'code', ''),
                    'tariff_title': getattr(tariff, 'title', getattr(basket.plan, 'title', 'Тариф')),
                    'months': months,
                    'device_label': basket.device_label,
                    'monthly_price': basket.effective_monthly_price,
                    'subtotal': basket.subtotal,
                    'discount_percent': basket.discount_percent,
                    'payable': basket.payable,
                }
            )

    preview_rows.append(
        {
            'tariff_code': 'trial',
            'tariff_title': 'Trial preview',
            'months': 0,
            'device_label': f'{trial_settings.trial_device_count} устройств',
            'monthly_price': Decimal('0.00'),
            'subtotal': Decimal('0.00'),
            'discount_percent': Decimal('0.00'),
            'payable': Decimal('0.00'),
            'trial_duration_days': trial_settings.trial_duration_days,
            'trial_traffic_gb': trial_settings.trial_traffic_gb,
        }
    )
    return preview_rows


_TARIFF_ADMIN_STATUS_OPTIONS = ('all', 'active', 'inactive', 'archived')


def _normalize_tariff_admin_status_filter(raw: str | None) -> str:
    value = (raw or '').strip().lower()
    return value if value in _TARIFF_ADMIN_STATUS_OPTIONS else 'all'


def _parse_money_form(raw: str | None, *, field_label: str, minimum: Decimal = Decimal('0.00'), allow_none: bool = False) -> Decimal | None:
    normalized = _normalized_optional_form_text(raw)
    if normalized is None:
        if allow_none:
            return None
        raise ValueError(f'{field_label} не указано')
    value = Decimal(normalized)
    quantized = value.quantize(Decimal('0.01'))
    if quantized < minimum:
        raise ValueError(f'{field_label} не может быть меньше {minimum}')
    return quantized


def _parse_optional_positive_int(raw: str | None, *, field_label: str, minimum: int = 1) -> int | None:
    normalized = _normalized_optional_form_text(raw)
    if normalized is None:
        return None
    value = int(normalized)
    if value < minimum:
        raise ValueError(f'{field_label} не может быть меньше {minimum}')
    return value


def _parse_period_months_form(values: list[str] | None, raw_csv: str | None, *, max_months: int) -> list[int]:
    result: list[int] = []
    seen: set[int] = set()
    merged: list[str] = []
    merged.extend(values or [])
    if raw_csv:
        merged.extend(part.strip() for part in raw_csv.split(','))
    for item in merged:
        if not item:
            continue
        months = int(item)
        if months < 1 or months > max_months:
            raise ValueError(f'Срок тарифа должен быть в диапазоне 1..{max_months} месяцев')
        if months in seen:
            continue
        seen.add(months)
        result.append(months)
    return sorted(result)


def _pricing_max_months(pricing) -> int:
    try:
        return max(1, int(getattr(pricing, 'max_months', 12) or 12))
    except Exception:
        return 12


def _tariff_is_archived(plan) -> bool:
    return bool(getattr(plan, 'is_archived', False))


def _tariff_status_for_admin(plan) -> str:
    if _tariff_is_archived(plan):
        return 'archived'
    if not bool(getattr(plan, 'is_active', True)):
        return 'inactive'
    return 'active'


def _tariff_status_label(status: str) -> str:
    return {
        'active': 'Активен',
        'inactive': 'Неактивен',
        'archived': 'В архиве',
    }.get(status, status)


def _tariff_status_tone(status: str) -> str:
    return {
        'active': 'emerald',
        'inactive': 'amber',
        'archived': 'slate',
    }.get(status, 'slate')


async def _tariff_usage_count_compat(repo: TariffRepository, plan) -> int | None:
    count_usage = getattr(repo, 'count_usage', None)
    if callable(count_usage):
        try:
            return int(await count_usage(plan))
        except Exception:
            return None
    return None


async def _tariff_list_archived_compat(repo: TariffRepository) -> list[object]:
    list_archived = getattr(repo, 'list_archived', None)
    if callable(list_archived):
        try:
            return list(await list_archived())
        except Exception:
            return []
    list_all = getattr(repo, 'list_all', None)
    if callable(list_all):
        return [plan for plan in await list_all() if _tariff_is_archived(plan)]
    return []


async def _tariff_get_for_update_compat(repo: TariffRepository, code: str):
    getter = getattr(repo, 'get_by_code_for_update', None)
    if callable(getter):
        plan = await getter(code)
        if plan is not None:
            return plan
    return await repo.get_by_code(code)


def _tariff_period_months_for_display(plan, pricing) -> list[int]:
    period_options = getattr(plan, 'period_options', None)
    if period_options:
        months: list[int] = []
        for option in period_options:
            if not bool(getattr(option, 'is_enabled', True)):
                continue
            try:
                months.append(int(getattr(option, 'months')))
            except Exception:
                continue
        if months:
            return sorted(dict.fromkeys(months))
    max_months = _pricing_max_months(pricing)
    return list(range(1, max_months + 1))


def _normalize_tariff_pricing_mode_for_admin(value: object | None) -> str:
    raw = str(getattr(value, 'value', value) or '').strip().lower()
    if raw in {'legacy_fixed', 'flat', 'fixed'}:
        return 'fixed'
    if raw == 'constructor':
        return 'constructor'
    return 'fixed'


def _tariff_admin_snapshot(plan, pricing) -> dict[str, object]:
    status = _tariff_status_for_admin(plan)
    return {
        'id': getattr(plan, 'id', None),
        'code': getattr(plan, 'code', None),
        'title': getattr(plan, 'title', None),
        'description': getattr(plan, 'description', None),
        'badge_text': getattr(plan, 'badge_text', None),
        'is_active': bool(getattr(plan, 'is_active', True)),
        'is_public': bool(getattr(plan, 'is_public', True)),
        'is_archived': _tariff_is_archived(plan),
        'sort_order': getattr(plan, 'sort_order', 100),
        'monthly_traffic_gb': getattr(plan, 'monthly_traffic_gb', None),
        'price_single': str(getattr(plan, 'price_single', Decimal('0.00'))),
        'price_unlimited': str(getattr(plan, 'price_unlimited', Decimal('0.00'))),
        'pricing_mode': _normalize_tariff_pricing_mode_for_admin(getattr(plan, 'pricing_mode', 'fixed')),
        'traffic_mode': str(getattr(getattr(plan, 'traffic_mode', 'fixed'), 'value', getattr(plan, 'traffic_mode', 'fixed')) or 'fixed'),
        'device_mode': str(getattr(getattr(plan, 'device_mode', 'fixed'), 'value', getattr(plan, 'device_mode', 'fixed')) or 'fixed'),
        'base_monthly_price': str(getattr(plan, 'base_monthly_price', getattr(plan, 'price_single', Decimal('0.00')))),
        'base_traffic_gb': getattr(plan, 'base_traffic_gb', getattr(plan, 'monthly_traffic_gb', None)),
        'fixed_traffic_gb': getattr(plan, 'fixed_traffic_gb', getattr(plan, 'monthly_traffic_gb', None)),
        'min_traffic_gb': getattr(plan, 'min_traffic_gb', None),
        'max_traffic_gb': getattr(plan, 'max_traffic_gb', None),
        'traffic_step_gb': getattr(plan, 'traffic_step_gb', None),
        'traffic_step_price': str(getattr(plan, 'traffic_step_price', Decimal('0.00'))),
        'base_device_count': getattr(plan, 'base_device_count', getattr(plan, 'online_limit_single', 1)),
        'fixed_device_count': getattr(plan, 'fixed_device_count', getattr(plan, 'online_limit_single', 1)),
        'min_device_count': getattr(plan, 'min_device_count', None),
        'max_device_count': getattr(plan, 'max_device_count', None),
        'device_step': getattr(plan, 'device_step', None),
        'device_step_price': str(getattr(plan, 'device_step_price', pricing.device_step_price)),
        'allow_unlimited_devices': bool(getattr(plan, 'allow_unlimited_devices', True)),
        'unlimited_devices_surcharge': str(getattr(plan, 'unlimited_devices_surcharge', pricing.unlimited_devices_price)),
        'online_limit_single': getattr(plan, 'online_limit_single', 1),
        'online_limit_unlimited': getattr(plan, 'online_limit_unlimited', None),
        'period_months': _tariff_period_months_for_display(plan, pricing),
        'status': status,
        'status_label': _tariff_status_label(status),
        'status_tone': _tariff_status_tone(status),
    }


def _tariff_form_from_plan(plan, pricing) -> dict[str, object]:
    if plan is None:
        max_months = _pricing_max_months(pricing)
        return {
            'code': '',
            'title': '',
            'description': '',
            'badge_text': '',
            'is_active': True,
            'is_public': True,
            'is_archived': False,
            'sort_order': 100,
            'monthly_traffic_gb': pricing.base_traffic_gb,
            'price_single': str(pricing.base_price),
            'price_unlimited': str(pricing.base_price + pricing.unlimited_devices_price),
            'pricing_mode': 'fixed',
            'traffic_mode': 'fixed',
            'device_mode': 'fixed',
            'base_monthly_price': str(pricing.base_price),
            'base_traffic_gb': pricing.base_traffic_gb,
            'fixed_traffic_gb': pricing.base_traffic_gb,
            'min_traffic_gb': pricing.base_traffic_gb,
            'max_traffic_gb': pricing.base_traffic_gb,
            'traffic_step_gb': pricing.traffic_step_gb,
            'traffic_step_price': str(pricing.traffic_step_price),
            'base_device_count': 1,
            'fixed_device_count': 1,
            'min_device_count': 1,
            'max_device_count': 1,
            'device_step': 1,
            'device_step_price': str(pricing.device_step_price),
            'allow_unlimited_devices': True,
            'unlimited_devices_surcharge': str(pricing.unlimited_devices_price),
            'online_limit_single': 1,
            'online_limit_unlimited': None,
            'period_months': [1, 3, 6] if max_months >= 6 else list(range(1, max_months + 1)),
            'period_months_csv': '1,3,6' if max_months >= 6 else ','.join(str(i) for i in range(1, max_months + 1)),
        }
    snapshot = _tariff_admin_snapshot(plan, pricing)
    snapshot['period_months_csv'] = ','.join(str(item) for item in snapshot['period_months'])
    return snapshot


def _tariff_matches_status(plan, status_filter: str) -> bool:
    if status_filter == 'all':
        return True
    return _tariff_status_for_admin(plan) == status_filter


async def _build_tariff_rows_for_admin(repo: TariffRepository, pricing, *, status_filter: str) -> tuple[list[dict[str, object]], dict[str, int], list[object]]:
    tariffs = list(await repo.list_all())
    archived_extra = await _tariff_list_archived_compat(repo)
    merged: dict[object, object] = {}
    for plan in [*tariffs, *archived_extra]:
        key = getattr(plan, 'id', None) or getattr(plan, 'code', None)
        merged[key] = plan
    all_tariffs = list(merged.values())

    counts = {'all': 0, 'active': 0, 'inactive': 0, 'archived': 0}
    rows: list[dict[str, object]] = []
    for plan in all_tariffs:
        status = _tariff_status_for_admin(plan)
        counts['all'] += 1
        counts[status] = counts.get(status, 0) + 1
        if not _tariff_matches_status(plan, status_filter):
            continue
        row = _tariff_admin_snapshot(plan, pricing)
        row['usage_count'] = await _tariff_usage_count_compat(repo, plan)
        rows.append(row)
    rows.sort(key=lambda item: (int(item.get('sort_order') or 100), str(item.get('code') or '')))
    return rows, counts, all_tariffs


async def _save_tariff_via_repo(
    repo: TariffRepository,
    *,
    existing,
    pricing,
    code: str,
    title: str,
    description: str | None,
    badge_text: str | None,
    is_active: bool,
    is_public: bool,
    is_archived: bool,
    sort_order: int,
    pricing_mode: str,
    traffic_mode: str,
    device_mode: str,
    base_monthly_price: Decimal,
    monthly_traffic_gb: int | None,
    price_single: Decimal,
    price_unlimited: Decimal,
    base_traffic_gb: int | None,
    fixed_traffic_gb: int | None,
    min_traffic_gb: int | None,
    max_traffic_gb: int | None,
    traffic_step_gb: int | None,
    traffic_step_price: Decimal | None,
    base_device_count: int | None,
    fixed_device_count: int | None,
    min_device_count: int | None,
    max_device_count: int | None,
    device_step: int | None,
    device_step_price: Decimal | None,
    allow_unlimited_devices: bool,
    unlimited_devices_surcharge: Decimal | None,
    online_limit_single: int,
    online_limit_unlimited: int | None,
    period_months: list[int],
):
    create_plan = getattr(repo, 'create_plan', None)
    update_plan = getattr(repo, 'update_plan', None)
    replace_period_options = getattr(repo, 'replace_period_options', None)

    if callable(create_plan) and callable(update_plan):
        payload = {
            'code': code,
            'title': title,
            'description': description,
            'badge_text': badge_text,
            'is_active': is_active,
            'is_public': is_public,
            'is_archived': is_archived,
            'sort_order': sort_order,
            'pricing_mode': pricing_mode,
            'traffic_mode': traffic_mode,
            'device_mode': device_mode,
            'base_monthly_price': base_monthly_price,
            'legacy_monthly_traffic_gb': monthly_traffic_gb,
            'legacy_price_single': price_single,
            'legacy_price_unlimited': price_unlimited,
            'base_traffic_gb': base_traffic_gb,
            'fixed_traffic_gb': fixed_traffic_gb,
            'min_traffic_gb': min_traffic_gb,
            'max_traffic_gb': max_traffic_gb,
            'traffic_step_gb': traffic_step_gb,
            'traffic_step_price': traffic_step_price,
            'base_device_count': base_device_count,
            'fixed_device_count': fixed_device_count,
            'min_device_count': min_device_count,
            'max_device_count': max_device_count,
            'device_step': device_step,
            'device_step_price': device_step_price,
            'allow_unlimited_devices': allow_unlimited_devices,
            'unlimited_devices_surcharge': unlimited_devices_surcharge,
            'legacy_online_limit_single': online_limit_single,
            'legacy_online_limit_unlimited': online_limit_unlimited,
        }
        plan = await (update_plan(existing, **payload) if existing is not None else create_plan(**payload))
        if callable(replace_period_options):
            await replace_period_options(plan, period_months)
        return plan

    plan = await repo.upsert(
        code=code,
        title=title,
        monthly_traffic_gb=monthly_traffic_gb,
        price_single=price_single,
        price_unlimited=price_unlimited,
        online_limit_single=online_limit_single,
        online_limit_unlimited=online_limit_unlimited,
        is_active=is_active and not is_archived,
        sort_order=sort_order,
    )
    for attr, value in {
        'description': description,
        'badge_text': badge_text,
        'is_public': is_public,
        'is_archived': is_archived,
        'pricing_mode': pricing_mode,
        'traffic_mode': traffic_mode,
        'device_mode': device_mode,
        'base_monthly_price': base_monthly_price,
        'base_traffic_gb': base_traffic_gb,
        'fixed_traffic_gb': fixed_traffic_gb,
        'min_traffic_gb': min_traffic_gb,
        'max_traffic_gb': max_traffic_gb,
        'traffic_step_gb': traffic_step_gb,
        'traffic_step_price': traffic_step_price,
        'base_device_count': base_device_count,
        'fixed_device_count': fixed_device_count,
        'min_device_count': min_device_count,
        'max_device_count': max_device_count,
        'device_step': device_step,
        'device_step_price': device_step_price,
        'allow_unlimited_devices': allow_unlimited_devices,
        'unlimited_devices_surcharge': unlimited_devices_surcharge,
    }.items():
        if hasattr(plan, attr):
            setattr(plan, attr, value)
    if hasattr(plan, 'is_active'):
        setattr(plan, 'is_active', is_active and not is_archived)
    if hasattr(plan, 'sort_order'):
        setattr(plan, 'sort_order', sort_order)
    if hasattr(repo, 'session'):
        await repo.session.flush()
    return plan


async def _archive_tariff_via_repo(repo: TariffRepository, plan) -> object:
    archive = getattr(repo, 'archive', None)
    if callable(archive):
        return await archive(plan)
    if hasattr(plan, 'is_archived'):
        setattr(plan, 'is_archived', True)
    if hasattr(plan, 'is_active'):
        setattr(plan, 'is_active', False)
    await repo.session.flush()
    return plan


async def _reactivate_tariff_via_repo(repo: TariffRepository, plan) -> object:
    reactivate = getattr(repo, 'reactivate', None)
    if callable(reactivate):
        return await reactivate(plan)
    if hasattr(plan, 'is_archived'):
        setattr(plan, 'is_archived', False)
    if hasattr(plan, 'is_active'):
        setattr(plan, 'is_active', True)
    await repo.session.flush()
    return plan


async def _delete_tariff_via_repo(repo: TariffRepository, plan) -> bool:
    delete_by_id = getattr(repo, 'delete_by_id', None)
    if callable(delete_by_id):
        return bool(await delete_by_id(plan))
    delete = getattr(repo, 'delete', None)
    if callable(delete):
        return bool(await delete(getattr(plan, 'code')))
    return False

@router.get('/admin', include_in_schema=False)
async def admin_index_redirect_noslash() -> RedirectResponse:
    return RedirectResponse(url='/admin/system/', status_code=307)

@router.get('/admin/system/', response_class=HTMLResponse, dependencies=[Depends(require_web_admin)])
async def admin_system(request: Request):
    templates = request.app.state.templates
    sessionmaker = request.app.state.sessionmaker
    settings = request.app.state.settings

    async with sessionmaker() as session:
        app_settings_repo = AppSettingsRepository(session)
        user_repo = UserRepository(session)
        ticket_repo = SupportTicketRepository(session)
        invoice_repo = InvoiceRepository(session)
        broadcast_repo = BroadcastJobRepository(session)
        promo_repo = PromoRepository(session)
        audit_repo = AuditLogRepository(session)

        app_settings = _app_settings_view(await app_settings_repo.get(), settings)
        counts = {
            'users_total': await user_repo.count(),
            'tickets_total': await ticket_repo.count(),
            'tickets_open': await ticket_repo.count_for_admin(status=SupportTicketStatus.waiting_operator)
                            + await ticket_repo.count_for_admin(status=SupportTicketStatus.waiting_user),
            'invoices_total': await invoice_repo.count(),
            'broadcasts_total': await broadcast_repo.count(),
            'broadcasts_active': await broadcast_repo.count_active(),
            'promocodes_total': await promo_repo.count(),
        }
        users = [
            SimpleNamespace(
                id=user.id,
                tg_id=user.tg_id,
                username=user.username,
                first_name=getattr(user, 'first_name', None),
                last_name=getattr(user, 'last_name', None),
                balance=user.balance,
                created_at=format_dt(getattr(user, 'created_at', None)),
            )
            for user in await user_repo.list_recent(limit=8, offset=0)
        ]
        recent_audit = await audit_repo.list_recent(limit=10, offset=0)
        health = await _build_health_snapshot(request, session, app_settings)

    return templates.TemplateResponse(
        request,
        'dashboard.html',
        {
            'current_page': 'system',
            'app_settings': app_settings,
            'counts': counts,
            'users': users,
            'recent_audit': recent_audit,
            'health': health,
            'success_message': request.query_params.get('success'),
            'error_message': request.query_params.get('error'),
            'query': '',
            'search_invoice': None,
            'search_ticket': None,
        },
    )


@router.get('/admin/search', response_class=HTMLResponse, dependencies=[Depends(require_web_admin)])
async def admin_search(request: Request, q: str | None = Query(default=None)):
    templates = request.app.state.templates
    sessionmaker = request.app.state.sessionmaker
    settings = request.app.state.settings
    query = (q or '').strip()

    async with sessionmaker() as session:
        app_settings_repo = AppSettingsRepository(session)
        user_repo = UserRepository(session)
        ticket_repo = SupportTicketRepository(session)
        invoice_repo = InvoiceRepository(session)
        broadcast_repo = BroadcastJobRepository(session)
        promo_repo = PromoRepository(session)
        audit_repo = AuditLogRepository(session)

        app_settings = _app_settings_view(await app_settings_repo.get(), settings)
        counts = {
            'users_total': await user_repo.count(),
            'tickets_total': await ticket_repo.count(),
            'tickets_open': await ticket_repo.count_for_admin(status=SupportTicketStatus.waiting_operator)
                            + await ticket_repo.count_for_admin(status=SupportTicketStatus.waiting_user),
            'invoices_total': await invoice_repo.count(),
            'broadcasts_total': await broadcast_repo.count(),
            'broadcasts_active': await broadcast_repo.count_active(),
            'promocodes_total': await promo_repo.count(),
        }
        health = await _build_health_snapshot(request, session, app_settings)
        recent_audit = await audit_repo.list_recent(limit=10, offset=0)

        if query:
            matched_users = await user_repo.search_extended(query, limit=20, offset=0)
        else:
            matched_users = []
        users = [
            SimpleNamespace(
                id=user.id,
                tg_id=user.tg_id,
                username=user.username,
                first_name=getattr(user, 'first_name', None),
                last_name=getattr(user, 'last_name', None),
                balance=user.balance,
                created_at=format_dt(getattr(user, 'created_at', None)),
            )
            for user in matched_users
        ]

        search_invoice = None
        search_ticket = None
        if query.isdigit():
            search_invoice = await invoice_repo.get_by_id(int(query))
            search_ticket = await ticket_repo.get_by_id(int(query))

    return templates.TemplateResponse(
        request,
        'dashboard.html',
        {
            'current_page': 'search',
            'app_settings': app_settings,
            'counts': counts,
            'users': users,
            'recent_audit': recent_audit,
            'health': health,
            'success_message': request.query_params.get('success'),
            'error_message': request.query_params.get('error'),
            'query': query,
            'search_invoice': search_invoice,
            'search_ticket': search_ticket,
        },
    )


@router.get('/admin/users/', response_class=HTMLResponse, dependencies=[Depends(require_web_admin)])
async def admin_users(request: Request, q: str | None = Query(default=None), page: int = Query(default=1, ge=1)):
    templates = request.app.state.templates
    sessionmaker = request.app.state.sessionmaker
    query = (q or '').strip()
    offset = (page - 1) * ADMIN_PAGE_SIZE

    async with sessionmaker() as session:
        repo = UserRepository(session)
        if query:
            total = await repo.count_search_extended(query)
            users_raw = await repo.search_extended(query, limit=ADMIN_PAGE_SIZE + 1, offset=offset)
        else:
            total = await repo.count()
            users_raw = await repo.list_recent(limit=ADMIN_PAGE_SIZE + 1, offset=offset)

    has_next = len(users_raw) > ADMIN_PAGE_SIZE
    users_raw = users_raw[:ADMIN_PAGE_SIZE]
    users = [
        SimpleNamespace(
            id=user.id,
            tg_id=user.tg_id,
            username=user.username,
            first_name=getattr(user, 'first_name', None),
            last_name=getattr(user, 'last_name', None),
            balance=user.balance,
            created_at=format_dt(getattr(user, 'created_at', None)),
        )
        for user in users_raw
    ]

    return templates.TemplateResponse(
        request,
        'admin_users.html',
        {
            'current_page': 'users',
            'users': users,
            'query': query,
            'page': page,
            'page_size': ADMIN_PAGE_SIZE,
            'total': total,
            'has_prev': page > 1,
            'has_next': has_next,
            'success_message': request.query_params.get('success'),
            'error_message': request.query_params.get('error'),
        },
    )


@router.get('/admin/users/{user_id}', response_class=HTMLResponse, dependencies=[Depends(require_web_admin)])
async def admin_user_detail(request: Request, user_id: int):
    templates = request.app.state.templates
    sessionmaker = request.app.state.sessionmaker

    async with sessionmaker() as session:
        user_repo = UserRepository(session)
        subscription_repo = SubscriptionRepository(session)
        invoice_repo = InvoiceRepository(session)
        user = await user_repo.get_by_id(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail='User not found')
        subscriptions_raw = await subscription_repo.list_by_user_id(user_id)
        invoices_raw = await invoice_repo.list_by_user_id(user_id, limit=100)

    subscriptions = []
    for sub in subscriptions_raw:
        monthly_traffic_bytes = getattr(sub, 'monthly_traffic_bytes', None)
        if monthly_traffic_bytes is None:
            traffic_label = '♾️ Безлимит'
        else:
            traffic_label = f"{bytes_to_gb(monthly_traffic_bytes)} ГБ / мес."
        subscriptions.append(
            SimpleNamespace(
                service_id=getattr(sub, 'service_id', '—'),
                is_trial=bool(getattr(sub, 'is_trial', False)),
                is_active=bool(getattr(sub, 'is_active', False)),
                status_label='Активна' if bool(getattr(sub, 'is_active', False)) else 'Неактивна',
                traffic_label=traffic_label,
                expire_label=format_dt(getattr(sub, 'expire_date', None)) or '—',
            )
        )

    invoices = []
    for invoice in invoices_raw:
        status_value = getattr(getattr(invoice, 'status', None), 'value', getattr(invoice, 'status', None))
        invoices.append(
            SimpleNamespace(
                id=invoice.id,
                created_at=format_dt(getattr(invoice, 'created_at', None)),
                purpose=getattr(getattr(invoice, 'purpose', None), 'value', getattr(invoice, 'purpose', None)) or '—',
                status=_invoice_status_label(status_value),
                provider=getattr(invoice, 'provider', None),
                external_invoice_id=getattr(invoice, 'external_invoice_id', None),
                payable_amount=_invoice_money_label(getattr(invoice, 'payable_amount', '—'), getattr(invoice, 'currency', None)),
                balance_used=_invoice_money_label(getattr(invoice, 'balance_used', '—'), getattr(invoice, 'currency', None)),
                payment_url=getattr(invoice, 'payment_url', None),
                can_approve=str(status_value or '').lower() in {'pending', 'paid'},
                can_cancel=str(status_value or '').lower() == 'pending',
            )
        )

    return templates.TemplateResponse(
        request,
        'user_detail.html',
        {
            'current_page': 'users',
            'user': SimpleNamespace(
                id=user.id,
                tg_id=user.tg_id,
                username=user.username,
                first_name=getattr(user, 'first_name', None),
                last_name=getattr(user, 'last_name', None),
                balance=user.balance,
                created_at=format_dt(getattr(user, 'created_at', None)),
            ),
            'subscriptions': subscriptions,
            'invoices': invoices,
            'success_message': request.query_params.get('success'),
            'error_message': request.query_params.get('error'),
        },
    )


@router.post('/admin/users/{user_id}/balance', dependencies=[Depends(require_web_admin)])
async def admin_user_balance_update(
    request: Request,
    user_id: int,
    direction: str = Form(...),
    amount: str = Form(...),
    comment: str | None = Form(default=None),
):
    sessionmaker = request.app.state.sessionmaker
    try:
        delta = Decimal((amount or '').strip())
    except Exception:
        return _redirect_with_message(f'/admin/users/{user_id}', error='Сумма должна быть числом')
    if delta <= Decimal('0.00'):
        return _redirect_with_message(f'/admin/users/{user_id}', error='Сумма должна быть больше нуля')
    normalized_direction = (direction or '').strip().lower()
    if normalized_direction not in {'increase', 'decrease'}:
        return _redirect_with_message(f'/admin/users/{user_id}', error='Некорректное направление изменения баланса')

    async with sessionmaker.begin() as session:
        user_repo = UserRepository(session)
        tx_repo = TransactionRepository(session)
        audit_repo = AuditLogRepository(session)
        user = await user_repo.get_by_id_for_update(user_id)
        if user is None:
            return _redirect_with_message('/admin/users/', error='Пользователь не найден')
        before_balance = Decimal(str(user.balance or '0.00'))
        description = (comment or '').strip() or 'Корректировка баланса из web-admin'
        if normalized_direction == 'increase':
            await user_repo.add_balance(user, delta)
            tx_type = TransactionType.income
        else:
            await user_repo.subtract_balance(user, delta)
            tx_type = TransactionType.outcome
        await tx_repo.create(user.id, delta, tx_type, description)
        await audit_repo.create(
            action=AuditAction.balance_adjusted,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            entity_type='user',
            entity_id=str(user.id),
            details={
                'direction': normalized_direction,
                'amount': str(delta),
                'before_balance': str(before_balance),
                'after_balance': str(user.balance),
                'comment': description,
            },
        )

    return _redirect_with_message(f'/admin/users/{user_id}', success='Баланс пользователя обновлён')


@router.get('/admin/people/', response_class=HTMLResponse, dependencies=[Depends(require_web_admin)])
async def admin_people(request: Request):
    templates = request.app.state.templates
    sessionmaker = request.app.state.sessionmaker
    settings = request.app.state.settings

    async with sessionmaker() as session:
        repo = AppSettingsRepository(session)
        row = _app_settings_view(await repo.get(), settings)

    return templates.TemplateResponse(
        request,
        'admin_people.html',
        {
            'current_page': 'people',
            'settings_row': row,
            'admin_ids_text': ', '.join(str(item) for item in row.admin_ids),
            'support_ids_text': ', '.join(str(item) for item in row.support_ids),
            'startup_notify_ids_text': ', '.join(str(item) for item in row.startup_notify_ids),
            'success_message': request.query_params.get('success'),
            'error_message': request.query_params.get('error'),
        },
    )


@router.post('/admin/people/', dependencies=[Depends(require_web_admin)])
async def admin_people_update(
    request: Request,
    admin_ids: str | None = Form(default=None),
    support_ids: str | None = Form(default=None),
    startup_notify_ids: str | None = Form(default=None),
    support_chat_id: str | None = Form(default=None),
):
    sessionmaker = request.app.state.sessionmaker
    async with sessionmaker.begin() as session:
        repo = AppSettingsRepository(session)
        audit_repo = AuditLogRepository(session)
        row = await repo.ensure()
        try:
            parsed_admin_ids = _parse_int_list_text(admin_ids)
            parsed_support_ids = _parse_int_list_text(support_ids)
            parsed_startup_ids = _parse_int_list_text(startup_notify_ids)
            parsed_support_chat_id = _parse_optional_int_text(support_chat_id)
        except Exception as exc:
            return _redirect_with_message('/admin/people/', error=str(exc))
        await repo.update_people_settings(
            row,
            admin_ids=parsed_admin_ids,
            support_ids=parsed_support_ids,
            support_chat_id=parsed_support_chat_id,
            startup_notify_ids=parsed_startup_ids,
        )
        await audit_repo.create(
            action=AuditAction.people_settings_updated,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            entity_type='app_settings',
            entity_id='1',
            details={
                'admin_ids': parsed_admin_ids,
                'support_ids': parsed_support_ids,
                'startup_notify_ids': parsed_startup_ids,
                'support_chat_id': parsed_support_chat_id,
            },
        )
    return _redirect_with_message('/admin/people/', success='Настройки людей и уведомлений обновлены')


@router.post('/admin/people/test-support-chat', dependencies=[Depends(require_web_admin)])
async def admin_people_test_support_chat(request: Request):
    sessionmaker = request.app.state.sessionmaker
    settings = request.app.state.settings
    bot = _get_bot_from_request(request)
    if bot is None:
        return _redirect_with_message('/admin/people/', error='Bot не привязан к app.state')

    async with sessionmaker.begin() as session:
        repo = AppSettingsRepository(session)
        audit_repo = AuditLogRepository(session)
        row = await repo.ensure()
        view = _app_settings_view(row, settings)
        support_chat_id = view.support_chat_id
        if support_chat_id is None:
            await repo.update_support_chat_test_status(row, status='error', error='support_chat_id не задан')
            return _redirect_with_message('/admin/people/', error='support_chat_id не задан')
        try:
            await bot.send_message(support_chat_id, '✅ Тестовое сообщение из web-admin SwoiVPN')
            await repo.update_support_chat_test_status(row, status='ok', error=None)
            await audit_repo.create(
                action=AuditAction.support_chat_tested,
                actor_type=AuditActorType.admin,
                actor_tg_id=None,
                entity_type='app_settings',
                entity_id='1',
                details={'support_chat_id': support_chat_id, 'status': 'ok'},
            )
            return _redirect_with_message('/admin/people/', success='Тестовое сообщение успешно отправлено в чат поддержки')
        except Exception as exc:
            await repo.update_support_chat_test_status(row, status='error', error=str(exc))
            await audit_repo.create(
                action=AuditAction.support_chat_tested,
                actor_type=AuditActorType.admin,
                actor_tg_id=None,
                entity_type='app_settings',
                entity_id='1',
                details={'support_chat_id': support_chat_id, 'status': 'error', 'error': str(exc)},
            )
            return _redirect_with_message('/admin/people/', error=f'Не удалось отправить тестовое сообщение: {exc}')


@router.get('/admin/pricing/', response_class=HTMLResponse, dependencies=[Depends(require_web_admin)])
async def admin_pricing(
    request: Request,
    status: str = Query(default='all'),
    edit_code: str | None = Query(default=None),
):
    templates = request.app.state.templates
    sessionmaker = request.app.state.sessionmaker
    status_filter = _normalize_tariff_admin_status_filter(status)

    async with sessionmaker.begin() as session:
        pricing_repo = PricingRuleRepository(session)
        tariff_repo = TariffRepository(session)
        app_settings_repo = AppSettingsRepository(session)

        pricing = await pricing_repo.ensure()
        app_settings = _app_settings_view(await app_settings_repo.get(), request.app.state.settings)
        preview_rows = await _build_pricing_preview(session, app_settings)
        tariff_rows, tariff_counts, all_tariffs = await _build_tariff_rows_for_admin(tariff_repo, pricing, status_filter=status_filter)
        tariffs = [row for row in tariff_rows if row['status'] != 'archived']
        archived_tariffs = [row for row in tariff_rows if row['status'] == 'archived']

        edit_plan = None
        if edit_code:
            normalized_edit_code = edit_code.strip()
            for plan in all_tariffs:
                if str(getattr(plan, 'code', '')).strip() == normalized_edit_code:
                    edit_plan = plan
                    break
        edit_form = _tariff_form_from_plan(edit_plan, pricing)

    return templates.TemplateResponse(
        request,
        'admin_pricing.html',
        {
            'current_page': 'pricing',
            'pricing': pricing,
            'tariffs': tariffs,
            'tariff_rows': tariff_rows,
            'archived_tariffs': archived_tariffs,
            'tariff_counts': tariff_counts,
            'status_filter': status_filter,
            'status_options': [
                {'value': 'all', 'label': 'Все'},
                {'value': 'active', 'label': 'Активные'},
                {'value': 'inactive', 'label': 'Неактивные'},
                {'value': 'archived', 'label': 'Архив'},
            ],
            'edit_code': edit_code,
            'edit_form': edit_form,
            'edit_tariff': _tariff_admin_snapshot(edit_plan, pricing) if edit_plan is not None else None,
            'constructor_supported': all(callable(getattr(tariff_repo, name, None)) for name in ('create_plan', 'update_plan')),
            'preview_rows': preview_rows,
            'success_message': request.query_params.get('success'),
            'error_message': request.query_params.get('error'),
        },
    )


@router.post('/admin/pricing/', dependencies=[Depends(require_web_admin)])
async def admin_pricing_update(
    request: Request,
    action: str | None = Form(default=None),
    base_price: str | None = Form(default=None),
    base_traffic_gb: int | None = Form(default=None),
    traffic_step_gb: int | None = Form(default=None),
    traffic_step_price: str | None = Form(default=None),
    device_step_price: str | None = Form(default=None),
    unlimited_devices_price: str | None = Form(default=None),
    unlimited_combo_price: str | None = Form(default=None),
    max_discount_percent: str | None = Form(default=None),
    max_months: int | None = Form(default=None),
    min_topup_amount: str | None = Form(default=None),
    tariff_code: str | None = Form(default=None),
    tariff_title: str | None = Form(default=None),
    tariff_description: str | None = Form(default=None),
    tariff_badge_text: str | None = Form(default=None),
    tariff_monthly_traffic_gb: int | None = Form(default=None),
    tariff_price_single: str | None = Form(default=None),
    tariff_price_unlimited: str | None = Form(default=None),
    tariff_online_limit_single: int = Form(default=1),
    tariff_online_limit_unlimited: int | None = Form(default=None),
    tariff_is_active: bool = Form(default=False),
    tariff_is_public: bool = Form(default=False),
    tariff_is_archived: bool = Form(default=False),
    tariff_sort_order: int = Form(default=100),
    tariff_pricing_mode: str | None = Form(default=None),
    tariff_traffic_mode: str | None = Form(default=None),
    tariff_device_mode: str | None = Form(default=None),
    tariff_base_monthly_price: str | None = Form(default=None),
    tariff_base_traffic_gb: str | None = Form(default=None),
    tariff_fixed_traffic_gb: str | None = Form(default=None),
    tariff_min_traffic_gb: str | None = Form(default=None),
    tariff_max_traffic_gb: str | None = Form(default=None),
    tariff_constructor_traffic_step_gb: str | None = Form(default=None),
    tariff_traffic_step_price: str | None = Form(default=None),
    tariff_base_device_count: str | None = Form(default=None),
    tariff_fixed_device_count: str | None = Form(default=None),
    tariff_min_device_count: str | None = Form(default=None),
    tariff_max_device_count: str | None = Form(default=None),
    tariff_device_step: str | None = Form(default=None),
    tariff_constructor_device_step_price: str | None = Form(default=None),
    tariff_allow_unlimited_devices: bool = Form(default=False),
    tariff_unlimited_devices_surcharge: str | None = Form(default=None),
    tariff_period_months: list[str] | None = Form(default=None),
    tariff_period_months_csv: str | None = Form(default=None),
):
    sessionmaker = request.app.state.sessionmaker

    if not (action or '').strip():
        return _redirect_with_message('/admin/pricing/', error='Действие не указано')

    try:
        async with sessionmaker.begin() as session:
            pricing_repo = PricingRuleRepository(session)
            tariff_repo = TariffRepository(session)
            audit_repo = AuditLogRepository(session)
            pricing = await pricing_repo.ensure()

            if action == 'update_rules':
                row = await pricing_repo.ensure()
                await pricing_repo.update(
                    row,
                    base_price=base_price or row.base_price,
                    base_traffic_gb=base_traffic_gb if base_traffic_gb is not None else row.base_traffic_gb,
                    traffic_step_gb=traffic_step_gb if traffic_step_gb is not None else row.traffic_step_gb,
                    traffic_step_price=traffic_step_price or row.traffic_step_price,
                    device_step_price=device_step_price or row.device_step_price,
                    unlimited_devices_price=unlimited_devices_price or row.unlimited_devices_price,
                    unlimited_combo_price=unlimited_combo_price or row.unlimited_combo_price,
                    max_discount_percent=max_discount_percent or row.max_discount_percent,
                    max_months=max_months if max_months is not None else row.max_months,
                    min_topup_amount=min_topup_amount or row.min_topup_amount,
                )
                await audit_repo.create(
                    action=AuditAction.pricing_updated,
                    actor_type=AuditActorType.admin,
                    actor_tg_id=None,
                    entity_type='pricing_rules',
                    entity_id='1',
                    details={
                        'base_price': str(row.base_price),
                        'base_traffic_gb': row.base_traffic_gb,
                        'traffic_step_gb': row.traffic_step_gb,
                        'traffic_step_price': str(row.traffic_step_price),
                        'device_step_price': str(row.device_step_price),
                        'unlimited_devices_price': str(row.unlimited_devices_price),
                        'unlimited_combo_price': str(row.unlimited_combo_price),
                        'max_discount_percent': str(row.max_discount_percent),
                        'max_months': row.max_months,
                        'min_topup_amount': str(row.min_topup_amount),
                    },
                )
                return _redirect_with_message('/admin/pricing/', success='Правила ценообразования обновлены')

            if action in {'save_tariff', 'upsert_tariff'}:
                code = (tariff_code or '').strip()
                title = (tariff_title or '').strip()
                if not code:
                    return _redirect_with_message('/admin/pricing/', error='Укажите код тарифа')
                if not title:
                    return _redirect_with_message('/admin/pricing/', error='Укажите название тарифа')

                existing = await _tariff_get_for_update_compat(tariff_repo, code)
                before_snapshot = _tariff_admin_snapshot(existing, pricing) if existing is not None else None
                max_allowed_months = _pricing_max_months(pricing)
                period_months = _parse_period_months_form(tariff_period_months, tariff_period_months_csv, max_months=max_allowed_months)
                if not period_months:
                    period_months = [1]

                base_monthly_price = _parse_money_form(
                    tariff_base_monthly_price or tariff_price_single,
                    field_label='Базовая цена тарифа',
                )
                price_single = _parse_money_form(
                    tariff_price_single or tariff_base_monthly_price,
                    field_label='Цена тарифа на 1 устройство',
                )
                unlimited_surcharge = _parse_money_form(
                    tariff_unlimited_devices_surcharge,
                    field_label='Доплата за безлимит устройств',
                    allow_none=True,
                )
                if unlimited_surcharge is None:
                    unlimited_surcharge = _parse_money_form(
                        str(getattr(pricing, 'unlimited_devices_price', '0.00')),
                        field_label='Доплата за безлимит устройств',
                    )
                price_unlimited = _parse_money_form(
                    tariff_price_unlimited,
                    field_label='Цена тарифа с безлимитом устройств',
                    allow_none=True,
                )
                if price_unlimited is None:
                    price_unlimited = (price_single or Decimal('0.00')) + (unlimited_surcharge or Decimal('0.00'))

                monthly_traffic_gb = tariff_monthly_traffic_gb
                if monthly_traffic_gb is None:
                    monthly_traffic_gb = _parse_optional_positive_int(tariff_fixed_traffic_gb, field_label='Фиксированный трафик, ГБ', minimum=1)
                if monthly_traffic_gb is None:
                    monthly_traffic_gb = _parse_optional_positive_int(tariff_base_traffic_gb, field_label='Базовый трафик, ГБ', minimum=1)

                plan = await _save_tariff_via_repo(
                    tariff_repo,
                    existing=existing,
                    pricing=pricing,
                    code=code,
                    title=title,
                    description=_normalized_optional_form_text(tariff_description),
                    badge_text=_normalized_optional_form_text(tariff_badge_text),
                    is_active=bool(tariff_is_active),
                    is_public=bool(tariff_is_public),
                    is_archived=bool(tariff_is_archived),
                    sort_order=int(tariff_sort_order),
                    pricing_mode=_normalize_tariff_pricing_mode_for_admin(tariff_pricing_mode),
                    traffic_mode=(tariff_traffic_mode or 'fixed').strip() or 'fixed',
                    device_mode=(tariff_device_mode or 'fixed').strip() or 'fixed',
                    base_monthly_price=base_monthly_price,
                    monthly_traffic_gb=monthly_traffic_gb,
                    price_single=price_single,
                    price_unlimited=price_unlimited,
                    base_traffic_gb=_parse_optional_positive_int(tariff_base_traffic_gb, field_label='Базовый трафик, ГБ', minimum=1),
                    fixed_traffic_gb=_parse_optional_positive_int(tariff_fixed_traffic_gb, field_label='Фиксированный трафик, ГБ', minimum=1),
                    min_traffic_gb=_parse_optional_positive_int(tariff_min_traffic_gb, field_label='Минимальный трафик, ГБ', minimum=1),
                    max_traffic_gb=_parse_optional_positive_int(tariff_max_traffic_gb, field_label='Максимальный трафик, ГБ', minimum=1),
                    traffic_step_gb=_parse_optional_positive_int(tariff_constructor_traffic_step_gb, field_label='Шаг трафика, ГБ', minimum=1),
                    traffic_step_price=_parse_money_form(tariff_traffic_step_price, field_label='Цена шага трафика', allow_none=True),
                    base_device_count=_parse_optional_positive_int(tariff_base_device_count, field_label='Базовое количество устройств', minimum=1),
                    fixed_device_count=_parse_optional_positive_int(tariff_fixed_device_count, field_label='Фиксированное количество устройств', minimum=1),
                    min_device_count=_parse_optional_positive_int(tariff_min_device_count, field_label='Минимальное количество устройств', minimum=1),
                    max_device_count=_parse_optional_positive_int(tariff_max_device_count, field_label='Максимальное количество устройств', minimum=1),
                    device_step=_parse_optional_positive_int(tariff_device_step, field_label='Шаг устройств', minimum=1),
                    device_step_price=_parse_money_form(tariff_constructor_device_step_price, field_label='Цена шага устройств', allow_none=True),
                    allow_unlimited_devices=bool(tariff_allow_unlimited_devices),
                    unlimited_devices_surcharge=unlimited_surcharge,
                    online_limit_single=max(1, int(tariff_online_limit_single or 1)),
                    online_limit_unlimited=tariff_online_limit_unlimited,
                    period_months=period_months,
                )
                plan = await tariff_repo.get_by_id(getattr(plan, 'id')) or plan
                after_snapshot = _tariff_admin_snapshot(plan, pricing)
                await audit_repo.create(
                    action=AuditAction.pricing_updated,
                    actor_type=AuditActorType.admin,
                    actor_tg_id=None,
                    entity_type='tariff_plan',
                    entity_id=str(getattr(plan, 'id', code)),
                    details={
                        'action': 'update' if before_snapshot is not None else 'create',
                        'before': before_snapshot,
                        'after': after_snapshot,
                    },
                )
                return _redirect_with_message('/admin/pricing/', success='Тариф сохранён')

            if action == 'archive_tariff':
                code = (tariff_code or '').strip()
                if not code:
                    return _redirect_with_message('/admin/pricing/', error='Код тарифа не указан')
                plan = await _tariff_get_for_update_compat(tariff_repo, code)
                if plan is None:
                    return _redirect_with_message('/admin/pricing/', error='Тариф не найден')
                before_snapshot = _tariff_admin_snapshot(plan, pricing)
                await _archive_tariff_via_repo(tariff_repo, plan)
                after_snapshot = _tariff_admin_snapshot(plan, pricing)
                await audit_repo.create(
                    action=AuditAction.pricing_updated,
                    actor_type=AuditActorType.admin,
                    actor_tg_id=None,
                    entity_type='tariff_plan',
                    entity_id=str(getattr(plan, 'id', code)),
                    details={'action': 'archive', 'before': before_snapshot, 'after': after_snapshot},
                )
                return _redirect_with_message('/admin/pricing/', success='Тариф архивирован')

            if action == 'reactivate_tariff':
                code = (tariff_code or '').strip()
                if not code:
                    return _redirect_with_message('/admin/pricing/', error='Код тарифа не указан')
                plan = await _tariff_get_for_update_compat(tariff_repo, code)
                if plan is None:
                    return _redirect_with_message('/admin/pricing/', error='Тариф не найден')
                before_snapshot = _tariff_admin_snapshot(plan, pricing)
                await _reactivate_tariff_via_repo(tariff_repo, plan)
                after_snapshot = _tariff_admin_snapshot(plan, pricing)
                await audit_repo.create(
                    action=AuditAction.pricing_updated,
                    actor_type=AuditActorType.admin,
                    actor_tg_id=None,
                    entity_type='tariff_plan',
                    entity_id=str(getattr(plan, 'id', code)),
                    details={'action': 'reactivate', 'before': before_snapshot, 'after': after_snapshot},
                )
                return _redirect_with_message('/admin/pricing/', success='Тариф снова активен')

            if action == 'delete_tariff':
                code = (tariff_code or '').strip()
                if not code:
                    return _redirect_with_message('/admin/pricing/', error='Код тарифа не указан')
                plan = await _tariff_get_for_update_compat(tariff_repo, code)
                if plan is None:
                    return _redirect_with_message('/admin/pricing/', error='Тариф не найден')
                usage_count = await _tariff_usage_count_compat(tariff_repo, plan)
                if usage_count and usage_count > 0:
                    return _redirect_with_message('/admin/pricing/', error='Использованный тариф нельзя удалить физически. Переведите его в архив.')
                before_snapshot = _tariff_admin_snapshot(plan, pricing)
                deleted = await _delete_tariff_via_repo(tariff_repo, plan)
                if not deleted:
                    return _redirect_with_message('/admin/pricing/', error='Не удалось удалить тариф')
                await audit_repo.create(
                    action=AuditAction.pricing_updated,
                    actor_type=AuditActorType.admin,
                    actor_tg_id=None,
                    entity_type='tariff_plan',
                    entity_id=str(getattr(plan, 'id', code)),
                    details={'action': 'delete', 'before': before_snapshot},
                )
                return _redirect_with_message('/admin/pricing/', success='Тариф удалён')
    except Exception as exc:
        return _redirect_with_message('/admin/pricing/', error=str(exc))

    return _redirect_with_message('/admin/pricing/', error='Неизвестное действие')

@router.get('/admin/trial/', response_class=HTMLResponse, dependencies=[Depends(require_web_admin)])
async def admin_trial(request: Request):
    templates = request.app.state.templates
    sessionmaker = request.app.state.sessionmaker

    settings = request.app.state.settings

    async with sessionmaker.begin() as session:
        repo = AppSettingsRepository(session)
        row = _app_settings_view(await repo.get(), settings)

    return templates.TemplateResponse(
        request,
        'admin_trial.html',
        {
            'current_page': 'trial',
            'settings_row': row,
            'trial_preview': {
                'duration_days': row.trial_duration_days,
                'traffic_gb': row.trial_traffic_gb,
                'device_count': row.trial_device_count,
            },
            'success_message': request.query_params.get('success'),
            'error_message': request.query_params.get('error'),
        },
    )


@router.post('/admin/trial/', dependencies=[Depends(require_web_admin)])
async def admin_trial_update(
    request: Request,
    trial_duration_days: int | None = Form(default=None),
    trial_traffic_gb: int | None = Form(default=None),
    trial_device_count: int | None = Form(default=None),
):
    sessionmaker = request.app.state.sessionmaker

    if trial_duration_days is None or trial_traffic_gb is None or trial_device_count is None:
        return _redirect_with_message('/admin/trial/', error='Заполните все параметры тестовой подписки')

    async with sessionmaker.begin() as session:
        repo = AppSettingsRepository(session)
        audit_repo = AuditLogRepository(session)
        row = await repo.ensure()
        await repo.update_trial_settings(
            row,
            trial_duration_days=trial_duration_days,
            trial_traffic_gb=trial_traffic_gb,
            trial_device_count=trial_device_count,
        )
        await audit_repo.create(
            action=AuditAction.trial_settings_updated,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            entity_type='app_settings',
            entity_id='1',
            details={
                'trial_duration_days': row.trial_duration_days,
                'trial_traffic_gb': row.trial_traffic_gb,
                'trial_device_count': row.trial_device_count,
            },
        )

    return _redirect_with_message('/admin/trial/', success='Trial-настройки обновлены')

@router.get('/admin/antispam/', response_class=HTMLResponse, dependencies=[Depends(require_web_admin)])
async def admin_antispam(request: Request):
    templates = request.app.state.templates
    sessionmaker = request.app.state.sessionmaker

    settings = request.app.state.settings

    async with sessionmaker.begin() as session:
        repo = AppSettingsRepository(session)
        row = _app_settings_view(await repo.get(), settings)

    return templates.TemplateResponse(
        request,
        'admin_antispam.html',
        {
            'current_page': 'antispam',
            'settings_row': row,
            'success_message': request.query_params.get('success'),
            'error_message': request.query_params.get('error'),
        },
    )


@router.post('/admin/antispam/', dependencies=[Depends(require_web_admin)])
async def admin_antispam_update(
    request: Request,
    anti_spam_enabled: bool = Form(default=False),
    anti_spam_message_limit: int | None = Form(default=None),
    anti_spam_message_window_seconds: int | None = Form(default=None),
    anti_spam_callback_limit: int | None = Form(default=None),
    anti_spam_callback_window_seconds: int | None = Form(default=None),
    anti_spam_block_seconds: int | None = Form(default=None),
    anti_spam_min_interval_seconds: Decimal | str | None = Form(default=None),
):
    sessionmaker = request.app.state.sessionmaker

    required_values = [
        anti_spam_message_limit,
        anti_spam_message_window_seconds,
        anti_spam_callback_limit,
        anti_spam_callback_window_seconds,
        anti_spam_block_seconds,
        anti_spam_min_interval_seconds,
    ]
    if any(value is None for value in required_values):
        return _redirect_with_message('/admin/antispam/', error='Заполните все anti-spam параметры')

    async with sessionmaker.begin() as session:
        repo = AppSettingsRepository(session)
        audit_repo = AuditLogRepository(session)
        row = await repo.ensure()
        await repo.update_antispam_settings(
            row,
            anti_spam_enabled=anti_spam_enabled,
            anti_spam_message_limit=int(anti_spam_message_limit),
            anti_spam_message_window_seconds=int(anti_spam_message_window_seconds),
            anti_spam_callback_limit=int(anti_spam_callback_limit),
            anti_spam_callback_window_seconds=int(anti_spam_callback_window_seconds),
            anti_spam_block_seconds=int(anti_spam_block_seconds),
            anti_spam_min_interval_seconds=anti_spam_min_interval_seconds,
        )
        await audit_repo.create(
            action=AuditAction.antispam_settings_updated,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            entity_type='app_settings',
            entity_id='1',
            details={
                'anti_spam_enabled': row.anti_spam_enabled,
                'anti_spam_message_limit': row.anti_spam_message_limit,
                'anti_spam_message_window_seconds': row.anti_spam_message_window_seconds,
                'anti_spam_callback_limit': row.anti_spam_callback_limit,
                'anti_spam_callback_window_seconds': row.anti_spam_callback_window_seconds,
                'anti_spam_block_seconds': row.anti_spam_block_seconds,
                'anti_spam_min_interval_seconds': str(row.anti_spam_min_interval_seconds),
            },
        )

    return _redirect_with_message('/admin/antispam/', success='Anti-spam настройки обновлены')

@router.get('/admin/rules/', response_class=HTMLResponse, dependencies=[Depends(require_web_admin)])
async def admin_rules(request: Request):
    templates = request.app.state.templates
    sessionmaker = request.app.state.sessionmaker

    settings = request.app.state.settings

    async with sessionmaker.begin() as session:
        repo = AppSettingsRepository(session)
        row = _app_settings_view(await repo.get(), settings)

    return templates.TemplateResponse(
        request,
        'admin_rules.html',
        {
            'current_page': 'rules',
            'settings_row': row,
            'success_message': request.query_params.get('success'),
            'error_message': request.query_params.get('error'),
        },
    )


@router.post('/admin/rules/', dependencies=[Depends(require_web_admin)])
async def admin_rules_update(
    request: Request,
    rules_service_url: str | None = Form(default=None),
    rules_of_use_url: str | None = Form(default=None),
    rules_privacy_url: str | None = Form(default=None),
):
    sessionmaker = request.app.state.sessionmaker
    settings = request.app.state.settings

    async with sessionmaker.begin() as session:
        repo = AppSettingsRepository(session)
        audit_repo = AuditLogRepository(session)
        existing_row = await repo.get()
        current = _app_settings_view(existing_row, settings)
        row = existing_row or await repo.ensure()

        try:
            resolved_rules_service_url = _nullable_public_url_form_value(
                rules_service_url,
                field_label='Ссылка на правила сервиса',
            )
            resolved_rules_of_use_url = _nullable_public_url_form_value(
                rules_of_use_url,
                field_label='Ссылка на пользовательское соглашение',
            )
            resolved_rules_privacy_url = _nullable_public_url_form_value(
                rules_privacy_url,
                field_label='Ссылка на политику конфиденциальности',
            )
        except Exception as exc:
            return _redirect_with_message('/admin/rules/', error=str(exc))

        await repo.update_rules_links(
            row,
            rules_service_url=resolved_rules_service_url,
            rules_of_use_url=resolved_rules_of_use_url,
            rules_privacy_url=resolved_rules_privacy_url,
        )
        await audit_repo.create(
            action=AuditAction.rules_links_updated,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            entity_type='app_settings',
            entity_id='1',
            details={
                'rules_service_url': row.rules_service_url,
                'rules_of_use_url': row.rules_of_use_url,
                'rules_privacy_url': row.rules_privacy_url,
            },
        )

    return _redirect_with_message('/admin/rules/', success='Ссылки правил обновлены')

@router.get('/admin/tickets/', response_class=HTMLResponse, dependencies=[Depends(require_web_admin)])
async def admin_tickets(
    request: Request,
    q: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    unanswered_first: bool = Query(default=False),
):
    templates = request.app.state.templates
    sessionmaker = request.app.state.sessionmaker

    status_enum = _parse_support_status(status)
    offset = (page - 1) * ADMIN_PAGE_SIZE
    status_options = [
        {'value': '', 'label': 'Все'},
        {'value': SupportTicketStatus.waiting_operator.value, 'label': 'Ожидает оператора'},
        {'value': SupportTicketStatus.waiting_user.value, 'label': 'Ожидает пользователя'},
        {'value': SupportTicketStatus.closed.value, 'label': 'Закрытые'},
    ]

    async with sessionmaker() as session:
        repo = SupportTicketRepository(session)
        total = await repo.count_for_admin(query=q, status=status_enum)
        rows = await repo.list_for_admin_with_meta(
            query=q,
            status=status_enum,
            limit=ADMIN_PAGE_SIZE,
            offset=offset,
            unanswered_first=unanswered_first,
        )

    now = datetime.now(timezone.utc)
    ticket_rows = []
    for ticket, last_message_at, has_admin_reply in rows:
        opened_for = now - (ticket.created_at or now)
        ticket_rows.append(
            {
                'ticket': ticket,
                'last_message_at': last_message_at,
                'last_message_label': format_dt(last_message_at) if last_message_at else '—',
                'opened_for_seconds': int(opened_for.total_seconds()),
                'has_admin_reply': has_admin_reply,
                'status_label': _support_status_label(ticket.status),
                'status_tone': _support_status_badge_tone(ticket.status),
            }
        )

    return templates.TemplateResponse(
        request,
        'admin_tickets.html',
        {
            'current_page': 'tickets',
            'tickets': ticket_rows,
            'query': q or '',
            'status_filter': status or '',
            'unanswered_first': unanswered_first,
            'status_options': status_options,
            'page': page,
            'page_size': ADMIN_PAGE_SIZE,
            'total': total,
            'has_prev': page > 1,
            'has_next': offset + len(ticket_rows) < total,
            'success_message': request.query_params.get('success'),
            'error_message': request.query_params.get('error'),
        },
    )


@router.get('/admin/tickets/{ticket_id}', response_class=HTMLResponse, dependencies=[Depends(require_web_admin)])
async def admin_ticket_detail(request: Request, ticket_id: int):
    templates = request.app.state.templates
    sessionmaker = request.app.state.sessionmaker

    async with sessionmaker() as session:
        ticket_repo = SupportTicketRepository(session)
        msg_repo = SupportMessageRepository(session)
        user_repo = UserRepository(session)

        ticket = await ticket_repo.get_by_id(ticket_id)
        if ticket is None:
            raise HTTPException(status_code=404, detail='Ticket not found')

        messages = await msg_repo.list_by_ticket(ticket_id)
        user = await user_repo.get_by_id(ticket.user_id)
        last_message_at = await ticket_repo.get_last_message_timestamp(ticket_id)
        has_admin_reply = await ticket_repo.has_admin_reply(ticket_id)

    return templates.TemplateResponse(
        request,
        'admin_ticket_detail.html',
        {
            'current_page': 'tickets',
            'ticket': ticket,
            'ticket_user': user,
            'messages': messages,
            'last_message_at': last_message_at,
            'has_admin_reply': has_admin_reply,
            'ticket_status_label': _support_status_label(ticket.status),
            'ticket_status_tone': _support_status_badge_tone(ticket.status),
            'ticket_last_actor_label': _support_actor_label(
                getattr(ticket, 'last_actor_type', None),
                getattr(ticket, 'last_actor_tg_id', None),
            ),
            'ticket_closed_by_label': _support_actor_label(
                'admin' if getattr(ticket, 'closed_by_admin_tg_id', None) is not None else None,
                getattr(ticket, 'closed_by_admin_tg_id', None),
            ),
            'ticket_close_reason': _normalized_optional_form_text(getattr(ticket, 'close_reason', None)),
            'success_message': request.query_params.get('success'),
            'error_message': request.query_params.get('error'),
        },
    )


@router.post('/admin/tickets/{ticket_id}/close', dependencies=[Depends(require_web_admin)])
async def admin_ticket_close(
    request: Request,
    ticket_id: int,
    close_reason: str | None = Form(default=None),
):
    sessionmaker = request.app.state.sessionmaker
    closed_user_id: int | None = None
    normalized_close_reason = _normalized_optional_form_text(close_reason)

    async with sessionmaker.begin() as session:
        repo = SupportTicketRepository(session)
        audit_repo = AuditLogRepository(session)
        ticket = await repo.get_by_id_for_update(ticket_id)
        if ticket is None:
            raise HTTPException(status_code=404, detail='Ticket not found')

        previous_status = getattr(ticket.status, 'value', ticket.status)
        closed_now = await repo.close(
            ticket,
            reason=normalized_close_reason,
            closed_by_admin_tg_id=None,
            actor_type='admin',
            actor_tg_id=None,
        )
        if not closed_now:
            return _redirect_with_message(f'/admin/tickets/{ticket_id}', error='Тикет уже закрыт')

        closed_user_id = ticket.user_id

        await audit_repo.create(
            action=AuditAction.ticket_closed,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            entity_type='support_ticket',
            entity_id=str(ticket.id),
            details={
                'reason': normalized_close_reason,
                'closed_via': 'web_admin',
                'previous_status': previous_status,
                'new_status': getattr(ticket.status, 'value', ticket.status),
            },
        )

    if closed_user_id is not None:
        with suppress(Exception):
            await _notify_ticket_closed_from_web_admin(request, ticket_id=ticket_id, user_id=closed_user_id)

    return _redirect_with_message(f'/admin/tickets/{ticket_id}', success='Тикет закрыт')


@router.get('/admin/promocodes/', response_class=HTMLResponse, dependencies=[Depends(require_web_admin)])
async def admin_promocodes(
    request: Request,
    page: int = Query(default=1, ge=1),
    status: str = Query(default='all'),
    q: str | None = Query(default=None),
    edit_id: str | None = Query(default=None),
):
    templates = request.app.state.templates
    sessionmaker = request.app.state.sessionmaker
    offset = (page - 1) * ADMIN_PAGE_SIZE
    status_filter = _normalize_promo_status_filter(status)
    search_query = (q or '').strip() or None
    parsed_edit_id = _parse_optional_query_int(edit_id)

    async with sessionmaker() as session:
        repo = PromoRepository(session)
        promos = await _promo_list_recent_filtered(
            repo,
            limit=ADMIN_PAGE_SIZE,
            offset=offset,
            status_filter=status_filter,
            query=search_query,
        )
        total = await _promo_count_filtered(repo, status_filter=status_filter, query=search_query)
        counts = await _promo_summary_counts(repo)
        edit_promo = await repo.get_by_id(parsed_edit_id) if parsed_edit_id is not None else None

    promo_rows = []
    for promo in promos:
        admin_status = _promo_admin_status(promo)
        used_count = int(getattr(promo, 'used_count', 0) or 0)
        promo_rows.append(
            SimpleNamespace(
                id=getattr(promo, 'id', None),
                code=getattr(promo, 'code', None),
                bonus_amount=getattr(promo, 'bonus_amount', None),
                used_count=used_count,
                max_uses=getattr(promo, 'max_uses', None),
                expires_at=getattr(promo, 'expires_at', None),
                created_at=getattr(promo, 'created_at', None),
                admin_status=admin_status,
                status_label=_promo_status_label(admin_status),
                status_tone=_promo_status_badge_tone(admin_status),
                can_archive=admin_status in {'active', 'expired', 'exhausted'},
                can_activate=admin_status in {'archived', 'expired', 'exhausted'},
                can_delete=used_count == 0,
                is_exhausted=admin_status == 'exhausted',
                is_expired=admin_status == 'expired',
            )
        )

    edit_form = _promo_edit_form_defaults(edit_promo) if edit_promo is not None else None
    status_options = [
        SimpleNamespace(value='all', label='Все'),
        SimpleNamespace(value='active', label='Активные'),
        SimpleNamespace(value='archived', label='В архиве'),
        SimpleNamespace(value='expired', label='Истёкшие'),
        SimpleNamespace(value='exhausted', label='Исчерпавшие лимит'),
    ]

    return templates.TemplateResponse(
        request,
        'admin_promocodes.html',
        {
            'current_page': 'promocodes',
            'promos': promos,
            'promo_rows': promo_rows,
            'page': page,
            'page_size': ADMIN_PAGE_SIZE,
            'total': total,
            'has_prev': page > 1,
            'has_next': offset + len(promos) < total,
            'success_message': request.query_params.get('success'),
            'error_message': request.query_params.get('error'),
            'status_filter': status_filter,
            'search_query': search_query or '',
            'status_options': status_options,
            'counts': counts,
            'edit_promo': edit_promo,
            'edit_form': edit_form,
        },
    )


@router.post('/admin/promocodes/', dependencies=[Depends(require_web_admin)])
async def admin_promocodes_update(
    request: Request,
    action: str | None = Form(default=None),
    code: str | None = Form(default=None),
    bonus_amount: str | None = Form(default=None),
    max_uses: str | None = Form(default=None),
    expires_at: str | None = Form(default=None),
    promo_id: int | None = Form(default=None),
    is_active: bool = Form(default=False),
):
    sessionmaker = request.app.state.sessionmaker

    normalized_action = (action or '').strip().lower()
    if not normalized_action:
        return _redirect_with_message('/admin/promocodes/', error='Действие не указано')

    try:
        async with sessionmaker.begin() as session:
            promo_service = PromoService(session)
            repo = PromoRepository(session)
            audit_repo = AuditLogRepository(session)

            if normalized_action == 'create':
                if bonus_amount is None or not (bonus_amount or '').strip():
                    return _redirect_with_message('/admin/promocodes/', error='Укажите бонусную сумму')

                promo = await promo_service.create_promo(
                    code=(code or '').strip() or None,
                    bonus_amount=bonus_amount,
                    max_uses=_coerce_optional_form_int(max_uses, field_label='Максимум использований'),
                    expires_at=_parse_datetime_local(expires_at),
                    is_active=is_active,
                    created_by_tg_id=None,
                )

                await audit_repo.create(
                    action=AuditAction.promo_created,
                    actor_type=AuditActorType.admin,
                    actor_tg_id=None,
                    entity_type='promo_code',
                    entity_id=str(promo.id),
                    details={
                        'operation': 'create',
                        **_promo_snapshot_for_audit(promo),
                    },
                )
                return _redirect_with_message('/admin/promocodes/', success='Промокод создан')

            if promo_id is None:
                return _redirect_with_message('/admin/promocodes/', error='promo_id не указан')

            promo_before = await repo.get_by_id(promo_id)
            if promo_before is None:
                return _redirect_with_message('/admin/promocodes/', error='Промокод не найден')
            before_snapshot = _promo_snapshot_for_audit(promo_before)

            if normalized_action == 'update':
                if not (code or '').strip():
                    return _redirect_with_message('/admin/promocodes/?edit_id=' + str(promo_id), error='Код промокода не может быть пустым')
                if bonus_amount is None or not (bonus_amount or '').strip():
                    return _redirect_with_message('/admin/promocodes/?edit_id=' + str(promo_id), error='Укажите бонусную сумму')

                promo = await promo_service.update_promo(
                    promo_id=promo_id,
                    code=(code or '').strip(),
                    bonus_amount=bonus_amount,
                    max_uses=_coerce_optional_form_int(max_uses, field_label='Максимум использований'),
                    expires_at=_parse_datetime_local(expires_at),
                    is_active=is_active,
                )
                await audit_repo.create(
                    action=AuditAction.admin_action,
                    actor_type=AuditActorType.admin,
                    actor_tg_id=None,
                    entity_type='promo_code',
                    entity_id=str(promo.id),
                    details={
                        'operation': 'update',
                        'before': before_snapshot,
                        'after': _promo_snapshot_for_audit(promo),
                    },
                )
                return _redirect_with_message('/admin/promocodes/', success='Промокод обновлён')

            if normalized_action in {'toggle_active', 'archive', 'activate'}:
                target_active = is_active if normalized_action == 'toggle_active' else normalized_action == 'activate'
                promo = await promo_service.set_active(promo_id=promo_id, is_active=target_active)
                operation = 'activate' if target_active else 'archive'
                await audit_repo.create(
                    action=AuditAction.admin_action,
                    actor_type=AuditActorType.admin,
                    actor_tg_id=None,
                    entity_type='promo_code',
                    entity_id=str(promo.id),
                    details={
                        'operation': operation,
                        'before': before_snapshot,
                        'after': _promo_snapshot_for_audit(promo),
                    },
                )
                message = 'Промокод активирован' if target_active else 'Промокод архивирован'
                return _redirect_with_message('/admin/promocodes/', success=message)

            if normalized_action == 'delete':
                await promo_service.delete_promo(promo_id)
                await audit_repo.create(
                    action=AuditAction.admin_action,
                    actor_type=AuditActorType.admin,
                    actor_tg_id=None,
                    entity_type='promo_code',
                    entity_id=str(promo_id),
                    details={
                        'operation': 'delete',
                        'before': before_snapshot,
                    },
                )
                return _redirect_with_message('/admin/promocodes/', success='Промокод удалён')
    except (LookupError, ValueError) as exc:
        target = f'/admin/promocodes/?edit_id={promo_id}' if normalized_action == 'update' and promo_id else '/admin/promocodes/'
        return _redirect_with_message(target, error=str(exc))
    except Exception as exc:
        target = f'/admin/promocodes/?edit_id={promo_id}' if normalized_action == 'update' and promo_id else '/admin/promocodes/'
        return _redirect_with_message(target, error=str(exc))

    return _redirect_with_message('/admin/promocodes/', error='Неизвестное действие')


@router.get('/admin/broadcasts/', response_class=HTMLResponse, dependencies=[Depends(require_web_admin)])
async def admin_broadcasts(
    request: Request,
    page: int = Query(default=1, ge=1),
    status: str = Query(default='all'),
    edit_id: int | None = Query(default=None),
):
    templates = request.app.state.templates
    sessionmaker = request.app.state.sessionmaker
    offset = (page - 1) * ADMIN_PAGE_SIZE
    status_filter = _normalize_broadcast_status_filter(status)

    async with sessionmaker() as session:
        repo = BroadcastJobRepository(session)
        jobs = await _broadcast_list_recent_filtered(repo, limit=ADMIN_PAGE_SIZE, offset=offset, status_filter=status_filter)
        total = await _broadcast_count_filtered(repo, status_filter=status_filter)
        summary_counts = await _broadcast_summary_counts(repo)
        edit_job = await repo.get_by_id(edit_id) if edit_id else None

    rows = [_broadcast_row(job) for job in jobs]
    edit_form = _broadcast_edit_form_defaults(edit_job) if edit_job is not None else {
        'job_id': None,
        'text_value': '',
        'run_at': '',
        'photo_file_id': '',
        'photo_file_unique_id': '',
        'keyboard_json': '',
        'status': 'scheduled',
    }
    preview_payload = None
    if edit_job is not None:
        try:
            preview_payload = BroadcastService.preview_text(
                BroadcastService.payload_from_job(edit_job),
                run_at=getattr(edit_job, 'run_at', None),
                status=getattr(getattr(edit_job, 'status', None), 'value', getattr(edit_job, 'status', None)),
            )
        except Exception:
            preview_payload = None

    return templates.TemplateResponse(
        request,
        'admin_broadcasts.html',
        {
            'current_page': 'broadcasts',
            'jobs': jobs,
            'job_rows': rows,
            'page': page,
            'page_size': ADMIN_PAGE_SIZE,
            'total': total,
            'active_count': summary_counts.get('active', 0),
            'summary_counts': summary_counts,
            'status_filter': status_filter,
            'status_options': [
                ('all', 'Все'),
                ('draft', 'Черновики'),
                ('scheduled', 'Запланированные'),
                ('running', 'Выполняются'),
                ('completed', 'Завершённые'),
                ('failed', 'С ошибкой'),
                ('cancelled', 'Отменённые'),
            ],
            'edit_job': edit_job,
            'edit_form': edit_form,
            'preview_payload': preview_payload,
            'has_prev': page > 1,
            'has_next': offset + len(jobs) < total,
            'success_message': request.query_params.get('success'),
            'error_message': request.query_params.get('error'),
        },
    )


@router.post('/admin/broadcasts/', dependencies=[Depends(require_web_admin)])
async def admin_broadcasts_upsert(
    request: Request,
    action: str = Form(default='create'),
    job_id: int | None = Form(default=None),
    text_value: str | None = Form(default=None),
    run_at: str | None = Form(default=None),
    photo_file_id: str | None = Form(default=None),
    photo_file_unique_id: str | None = Form(default=None),
    keyboard_json: str | None = Form(default=None),
    status_value: str | None = Form(default='scheduled', alias='status'),
):
    sessionmaker = request.app.state.sessionmaker
    settings = request.app.state.settings
    normalized_action = (action or 'create').strip().lower()
    normalized_status = (status_value or 'scheduled').strip().lower()
    if normalized_status not in {'draft', 'scheduled'}:
        normalized_status = 'scheduled'

    edit_target = f'/admin/broadcasts/?edit_id={job_id}' if job_id else '/admin/broadcasts/'
    scheduled_at = _parse_datetime_local(run_at) if normalized_action in {'create', 'update', 'clone'} else None

    async with sessionmaker.begin() as session:
        repo = BroadcastJobRepository(session)
        app_settings_repo = AppSettingsRepository(session)
        service = BroadcastService(session)

        app_settings_row = await app_settings_repo.get()
        created_by_tg_id = _first_runtime_recipient_tg_id(app_settings_row, settings) or 0

        try:
            if normalized_action == 'create':
                job = await service.create_job(
                    created_by_tg_id=int(created_by_tg_id),
                    text=text_value,
                    run_at=scheduled_at,
                    photo_file_id=photo_file_id,
                    photo_file_unique_id=photo_file_unique_id,
                    media_type='photo' if (photo_file_id or '').strip() else None,
                    keyboard_json_raw=keyboard_json,
                    status=BroadcastJobStatus.draft if normalized_status == 'draft' else BroadcastJobStatus.scheduled,
                )
                message = 'Черновик создан' if normalized_status == 'draft' else 'Рассылка создана'
                return _redirect_with_message('/admin/broadcasts/', success=message)

            if normalized_action == 'update':
                if job_id is None:
                    return _redirect_with_message('/admin/broadcasts/', error='Не указан ID рассылки для редактирования')
                job = await service.update_job(
                    job_id=job_id,
                    text=text_value,
                    run_at=scheduled_at,
                    photo_file_id=photo_file_id,
                    photo_file_unique_id=photo_file_unique_id,
                    media_type='photo' if (photo_file_id or '').strip() else None,
                    keyboard_json_raw=keyboard_json,
                )
                # optional draft -> scheduled promotion/demotion by direct status write for editable jobs
                target_status = BroadcastJobStatus.draft if normalized_status == 'draft' else BroadcastJobStatus.scheduled
                if job.status != target_status and getattr(job, 'is_editable', False):
                    job.status = target_status
                    await session.flush()
                return _redirect_with_message('/admin/broadcasts/', success='Рассылка обновлена')

            if normalized_action == 'delete':
                if job_id is None:
                    return _redirect_with_message('/admin/broadcasts/', error='Не указан ID рассылки для удаления')
                await service.delete_job(job_id=job_id)
                return _redirect_with_message('/admin/broadcasts/', success='Рассылка удалена')

            if normalized_action == 'cancel':
                if job_id is None:
                    return _redirect_with_message('/admin/broadcasts/', error='Не указан ID рассылки для отмены')
                await service.request_cancel(job_id=job_id, reason='cancelled_via_web_admin')
                return _redirect_with_message('/admin/broadcasts/', success='Рассылка отменена')

            if normalized_action == 'clone':
                if job_id is None:
                    return _redirect_with_message('/admin/broadcasts/', error='Не указан ID исходной рассылки')
                clone = await service.clone_job(
                    job_id=job_id,
                    created_by_tg_id=int(created_by_tg_id),
                    run_at=scheduled_at,
                    status=BroadcastJobStatus.draft if normalized_status == 'draft' else BroadcastJobStatus.scheduled,
                )
                message = 'Черновик-копия создан' if clone.status == BroadcastJobStatus.draft else 'Копия рассылки создана'
                return _redirect_with_message('/admin/broadcasts/', success=message)

        except BroadcastValidationError as exc:
            return _redirect_with_message(edit_target, error=str(exc))
        except LookupError as exc:
            return _redirect_with_message(edit_target, error=str(exc))
        except ValueError as exc:
            return _redirect_with_message(edit_target, error=str(exc))

    return _redirect_with_message('/admin/broadcasts/', error='Неизвестное действие для рассылки')


@router.post('/admin/broadcasts/test', dependencies=[Depends(require_web_admin)])
async def admin_broadcasts_test_send(
    request: Request,
    text_value: str | None = Form(default=None),
    test_tg_id: str | None = Form(default=None),
    photo_file_id: str | None = Form(default=None),
    photo_file_unique_id: str | None = Form(default=None),
    keyboard_json: str | None = Form(default=None),
):
    bot = _get_bot_from_request(request)
    sessionmaker = request.app.state.sessionmaker

    if bot is None:
        return _redirect_with_message('/admin/broadcasts/', error='Bot не прикреплён к app.state')

    settings = request.app.state.settings

    async with sessionmaker.begin() as session:
        app_settings_repo = AppSettingsRepository(session)
        service = BroadcastService(session)
        app_settings = await app_settings_repo.get()

        resolved_test_tg_id: int | None = None
        if (test_tg_id or '').strip():
            try:
                resolved_test_tg_id = int((test_tg_id or '').strip())
            except ValueError:
                return _redirect_with_message('/admin/broadcasts/', error='Некорректный test_tg_id')
        else:
            resolved_test_tg_id = _first_runtime_recipient_tg_id(app_settings, settings)

        if resolved_test_tg_id is None:
            return _redirect_with_message('/admin/broadcasts/', error='Не удалось определить Telegram ID для тестовой отправки')

        try:
            await service.send_test(
                bot,
                target_tg_id=resolved_test_tg_id,
                text=text_value,
                photo_file_id=photo_file_id,
                photo_file_unique_id=photo_file_unique_id,
                media_type='photo' if (photo_file_id or '').strip() else None,
                keyboard_json_raw=keyboard_json,
            )
        except BroadcastValidationError as exc:
            return _redirect_with_message('/admin/broadcasts/', error=str(exc))
        except Exception as exc:
            return _redirect_with_message('/admin/broadcasts/', error=f'Ошибка тестовой отправки: {exc}')

    return _redirect_with_message('/admin/broadcasts/', success='Тестовое сообщение отправлено')

@router.get('/admin/links/', response_class=HTMLResponse, dependencies=[Depends(require_web_admin)])
async def admin_app_links(request: Request):
    templates = request.app.state.templates
    sessionmaker = request.app.state.sessionmaker

    async with sessionmaker() as session:
        repo = AppLinkRepository(session)
        links = await repo.list_all()

    link_rows = [
        {
            'id': link.id,
            'os_name': link.os_name,
            'download_url': _safe_public_url_for_display(getattr(link, 'download_url', None), field_label='Ссылка на приложение'),
            'guide_url': _safe_public_url_for_display(getattr(link, 'guide_url', None), field_label='Ссылка на инструкцию'),
        }
        for link in links
    ]

    return templates.TemplateResponse(
        request,
        'app_links.html',
        {
            'current_page': 'links',
            'links': link_rows,
            'success_message': request.query_params.get('success'),
            'error_message': request.query_params.get('error'),
        },
    )


@router.post('/admin/links/{link_id}', dependencies=[Depends(require_web_admin)])
async def admin_app_links_update(
    request: Request,
    link_id: int,
    download_url: str | None = Form(default=None),
    guide_url: str | None = Form(default=None),
):
    sessionmaker = request.app.state.sessionmaker

    try:
        async with sessionmaker.begin() as session:
            repo = AppLinkRepository(session)
            audit_repo = AuditLogRepository(session)

            link = await repo.get_by_id(link_id)
            if link is None:
                raise HTTPException(status_code=404, detail='AppLink not found')

            normalized_download_url = _nullable_public_url_form_value(
                download_url,
                field_label='Ссылка на приложение',
            )
            normalized_guide_url = _nullable_public_url_form_value(
                guide_url,
                field_label='Ссылка на инструкцию',
            )

            old_download_url = getattr(link, 'download_url', None)
            old_guide_url = getattr(link, 'guide_url', None)
            await repo.update_urls(
                link,
                download_url=normalized_download_url,
                guide_url=normalized_guide_url,
            )

            await audit_repo.create(
                action=AuditAction.admin_action,
                actor_type=AuditActorType.admin,
                actor_tg_id=None,
                entity_type='app_link',
                entity_id=str(link.id),
                details={
                    'os_name': link.os_name,
                    'old_download_url': old_download_url,
                    'old_guide_url': old_guide_url,
                    'new_download_url': link.download_url,
                    'new_guide_url': link.guide_url,
                },
            )
    except HTTPException:
        raise
    except Exception as exc:
        return _redirect_with_message('/admin/links/', error=str(exc))

    return _redirect_with_message('/admin/links/', success='Ссылки платформы обновлены')



@router.get('/admin/marzban-page/', response_class=HTMLResponse, dependencies=[Depends(require_web_admin)])
async def admin_marzban_page(request: Request):
    templates = request.app.state.templates
    sessionmaker = request.app.state.sessionmaker
    settings = request.app.state.settings

    preview_health: dict[str, Any] = {'ok': False, 'state': 'unchecked', 'message': 'Preview not checked yet.'}
    managed_env_health: dict[str, Any] = {'ok': False, 'state': 'unchecked', 'message': 'Managed env not checked yet.'}
    live_apply_health: dict[str, Any] = {'ok': False, 'state': 'unchecked', 'message': 'Live apply not checked yet.'}
    template_paths_state: Any = None
    env_path_state: Any = None
    deployed_template_excerpt: str | None = None
    managed_env_preview: Any = None
    managed_env_items: list[Any] = []
    settings_row = None

    async with sessionmaker.begin() as session:
        repo = MarzbanPageSettingsRepository(session)
        settings_row = await repo.ensure()
        env_manager = MarzbanEnvManager(settings)

        def _read_managed_env():
            return (
                env_manager.list_items(),
                env_manager.preview_updates({}),
                env_manager.path_state(),
            )

        managed_env_items, managed_env_preview, env_path_state = await asyncio.to_thread(_read_managed_env)
        renderer = MarzbanTemplateRenderer(session, settings)
        template_paths_state = renderer.template_paths_state()

        preview_result = None
        deploy_template_result = None
        try:
            preview_result = await renderer.render_preview()
            deploy_template_result = await renderer.render_deploy_template()
            subscription_url = (preview_result.context.get('subscription_url') or 'not generated') if isinstance(preview_result.context, dict) else 'not generated'
            platform_cards = list(preview_result.context.get('platform_cards') or []) if isinstance(preview_result.context, dict) else []
            linked_cards = sum(1 for card in platform_cards if isinstance(card, dict) and card.get('has_any_link'))
            preview_health = {
                'ok': True,
                'state': 'ready',
                'message': f'Preview context + template render OK. Canonical subscription URL: {subscription_url}. Platform cards with links: {linked_cards}/{len(platform_cards)}.',
            }
        except Exception as exc:
            preview_health = {
                'ok': False,
                'state': 'render_error',
                'message': f'Preview render failed: {exc}',
            }

        managed_count = len(managed_env_items)
        readonly_count = sum(1 for item in managed_env_items if item.readonly)
        editable_count = managed_count - readonly_count
        present_count = sum(1 for item in managed_env_items if item.present)
        if not managed_env_items:
            managed_env_health = {
                'ok': False,
                'state': 'no_allowlist',
                'message': 'Managed env allowlist is empty: UI cannot edit Marzban/Xray env keys until allowlist is configured.',
            }
        else:
            state = 'ready' if env_path_state.exists and env_path_state.writable else 'degraded'
            managed_env_health = {
                'ok': bool(env_path_state.writable),
                'state': state,
                'message': (
                    f'Env file: {env_path_state.path}. Exists: {"yes" if env_path_state.exists else "no"}. '
                    f'Writable target: {env_path_state.writable_target}. Writable: {"yes" if env_path_state.writable else "no"}. '
                    f'Managed keys: {managed_count}; editable: {editable_count}; readonly: {readonly_count}; currently set: {present_count}.'
                ),
            }

        restart_command = _normalize_local_command(getattr(settings, 'marzban_restart_command', None))
        pending_changes = False
        if preview_result is not None and preview_result.paths.deployed_exists:
            try:
                deployed_template = renderer.read_deployed_template()
                deployed_template_excerpt = deployed_template[:4000]
                pending_changes = deployed_template != (deploy_template_result.rendered_html if deploy_template_result is not None else preview_result.source_template)
            except Exception as exc:
                deployed_template_excerpt = f'Failed to read deployed template: {exc}'
        elif preview_result is not None:
            pending_changes = True

        live_apply_health = _build_marzban_page_apply_health(
            preview_ok=bool(preview_health.get('ok')),
            preview_message=str(preview_health.get('message') or ''),
            template_paths_state=template_paths_state,
            env_path_state=env_path_state,
            restart_command=restart_command,
            pending_changes=pending_changes,
        )

    return templates.TemplateResponse(
        request,
        'admin_marzban_page.html',
        {
            'current_page': 'marzban_page',
            'settings_row': settings_row,
            'managed_env_items': managed_env_items,
            'managed_env_form_action': '/admin/marzban-page/env/',
            'managed_env_field_prefix': 'env__',
            'preview_iframe_url': '/admin/marzban-page/preview',
            'preview_health': preview_health,
            'managed_env_health': managed_env_health,
            'managed_env_preview': managed_env_preview,
            'managed_env_path_state': env_path_state,
            'template_paths_state': template_paths_state,
            'live_apply_health': live_apply_health,
            'deployed_template_excerpt': deployed_template_excerpt,
            'page_apply_form_action': '/admin/marzban-page/apply/',
            'success_message': request.query_params.get('success'),
            'error_message': request.query_params.get('error'),
        },
    )


@router.post('/admin/marzban-page/', dependencies=[Depends(require_web_admin)])
async def admin_marzban_page_update(
    request: Request,
    brand_name: str | None = Form(default=None),
    page_title: str | None = Form(default=None),
    hero_title: str | None = Form(default=None),
    hero_text: str | None = Form(default=None),
    connect_button_text: str | None = Form(default=None),
    connect_hint_text: str | None = Form(default=None),
    support_text: str | None = Form(default=None),
    platforms_title: str | None = Form(default=None),
    platforms_subtitle: str | None = Form(default=None),
    show_usage_block: bool = Form(default=False),
    show_subscription_copy_button: bool = Form(default=False),
    show_platform_cards: bool = Form(default=False),
    show_primary_connect_button: bool = Form(default=False),
    show_one_click_block: bool = Form(default=False),
    show_hiddify_button: bool = Form(default=False),
    show_v2raytun_button: bool = Form(default=False),
    show_happ_button: bool = Form(default=False),
    show_qr_button: bool = Form(default=False),
):
    required_texts = [brand_name, page_title, hero_title, hero_text, connect_button_text, platforms_title]
    if any(not (value or '').strip() for value in required_texts):
        return _redirect_with_message('/admin/marzban-page/', error='Заполните все обязательные поля страницы подписки')

    sessionmaker = request.app.state.sessionmaker
    async with sessionmaker.begin() as session:
        repo = MarzbanPageSettingsRepository(session)
        audit_repo = AuditLogRepository(session)
        row = await repo.ensure()
        await repo.update(
            row,
            brand_name=brand_name or '',
            page_title=page_title or '',
            hero_title=hero_title or '',
            hero_text=hero_text or '',
            connect_button_text=connect_button_text or '',
            connect_hint_text=connect_hint_text,
            support_text=support_text,
            platforms_title=platforms_title or '',
            platforms_subtitle=platforms_subtitle,
            show_usage_block=show_usage_block,
            show_subscription_copy_button=show_subscription_copy_button,
            show_platform_cards=show_platform_cards,
            show_primary_connect_button=show_primary_connect_button,
            show_one_click_block=show_one_click_block,
            show_hiddify_button=show_hiddify_button,
            show_v2raytun_button=show_v2raytun_button,
            show_happ_button=show_happ_button,
            show_qr_button=show_qr_button,
        )
        await audit_repo.create(
            action=AuditAction.admin_action,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            entity_type='marzban_page_settings',
            entity_id='1',
            details={
                'brand_name': row.brand_name,
                'page_title': row.page_title,
                'hero_title': row.hero_title,
                'platforms_title': row.platforms_title,
                'show_usage_block': row.show_usage_block,
                'show_subscription_copy_button': row.show_subscription_copy_button,
                'show_platform_cards': row.show_platform_cards,
                'show_primary_connect_button': row.show_primary_connect_button,
                'show_one_click_block': row.show_one_click_block,
                'show_hiddify_button': row.show_hiddify_button,
                'show_v2raytun_button': row.show_v2raytun_button,
                'show_happ_button': row.show_happ_button,
                'show_qr_button': row.show_qr_button,
            },
        )
    return _redirect_with_message('/admin/marzban-page/', success='Настройки страницы подписки обновлены')


@router.post('/admin/marzban-page/env/', dependencies=[Depends(require_web_admin)])
async def admin_marzban_page_env_update(request: Request):
    settings = request.app.state.settings
    sessionmaker = request.app.state.sessionmaker
    form = await request.form()
    updates = _managed_env_updates_from_form(form)
    manager = MarzbanEnvManager(settings)

    try:
        apply_result = await asyncio.to_thread(
            manager.apply_updates, updates, backup_suffix='admin_marzban_page_env'
        )
    except Exception as exc:
        return _redirect_with_message('/admin/marzban-page/', error=str(exc))

    changed_keys = list(apply_result.changed_keys)
    async with sessionmaker.begin() as session:
        await AuditLogRepository(session).create(
            action=AuditAction.admin_action,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            entity_type='marzban_page_managed_env',
            entity_id='managed_env',
            details={
                'changed_keys': changed_keys,
                'changed_count': len(changed_keys),
                'backup_path': str(apply_result.backup_path) if apply_result.backup_path else None,
                'path': str(apply_result.path),
            },
        )

    if not changed_keys:
        return _redirect_with_message('/admin/marzban-page/', success='Изменений в managed env не было')
    return _redirect_with_message(
        '/admin/marzban-page/',
        success=f'Управляемые env-настройки обновлены: {", ".join(changed_keys)}',
    )


@router.post('/admin/marzban-page/apply/', dependencies=[Depends(require_web_admin)])
async def admin_marzban_page_apply(request: Request):
    settings = request.app.state.settings
    sessionmaker = request.app.state.sessionmaker

    render_result = None
    deploy_template_text: str | None = None
    page_snapshot: dict[str, Any] | None = None
    async with sessionmaker.begin() as session:
        repo = MarzbanPageSettingsRepository(session)
        row = await repo.ensure()
        page_snapshot = {
            'brand_name': row.brand_name,
            'page_title': row.page_title,
            'hero_title': row.hero_title,
            'platforms_title': row.platforms_title,
            'show_usage_block': row.show_usage_block,
            'show_subscription_copy_button': row.show_subscription_copy_button,
            'show_platform_cards': row.show_platform_cards,
            'show_primary_connect_button': row.show_primary_connect_button,
            'show_one_click_block': row.show_one_click_block,
            'show_hiddify_button': row.show_hiddify_button,
            'show_v2raytun_button': row.show_v2raytun_button,
            'show_happ_button': row.show_happ_button,
            'show_qr_button': row.show_qr_button,
        }
        renderer = MarzbanTemplateRenderer(session, settings)
        try:
            render_result = await renderer.render_preview()
            if hasattr(renderer, 'render_deploy_template'):
                deploy_result = await renderer.render_deploy_template()
                if isinstance(deploy_result, str):
                    deploy_template_text = deploy_result
                elif hasattr(deploy_result, 'rendered_html'):
                    deploy_template_text = str(deploy_result.rendered_html)
                elif hasattr(deploy_result, 'source_template'):
                    deploy_template_text = str(deploy_result.source_template)
                else:
                    raise TypeError('render_deploy_template() must return str or MarzbanTemplateRenderResult-like object')
            else:
                deploy_template_text = render_result.source_template
        except Exception as exc:
            return _redirect_with_message('/admin/marzban-page/', error=f'Preview/render failed; live apply aborted: {exc}')

    if render_result is None or deploy_template_text is None:
        return _redirect_with_message('/admin/marzban-page/', error='Не удалось подготовить live apply для страницы Marzban')

    paths = render_result.paths
    deployed_path = paths.deployed_template_path
    if not paths.source_exists:
        return _redirect_with_message('/admin/marzban-page/', error=f'Source template not found: {paths.source_template_path}')
    if not paths.deployed_writable:
        return _redirect_with_message('/admin/marzban-page/', error=f'Deploy target is not writable: {deployed_path}')

    restart_command = _normalize_local_command(getattr(settings, 'marzban_restart_command', None))

    def _read_previous_state() -> tuple[Path | None, str | None]:
        backup = _backup_existing_file(deployed_path, label='before_admin_apply')
        previous = deployed_path.read_text(encoding='utf-8') if deployed_path.exists() else None
        return backup, previous

    backup_path, previous_text = await asyncio.to_thread(_read_previous_state)

    restart_result: Any | None = None
    try:
        await asyncio.to_thread(_atomic_write_text, deployed_path, deploy_template_text)
        if restart_command:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *restart_command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except (OSError, FileNotFoundError) as spawn_exc:
                raise RuntimeError(
                    f'Marzban restart command failed to spawn: command={restart_command!r}; error={spawn_exc}'
                ) from spawn_exc

            timed_out = False
            try:
                stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=120)
            except asyncio.TimeoutError:
                timed_out = True
                proc.kill()
                with suppress(BaseException):
                    await proc.wait()
                stdout_b = b''
                stderr_b = b'restart timed out after 120s'

            restart_result = SimpleNamespace(
                returncode=-1 if timed_out else proc.returncode,
                stdout=stdout_b.decode(errors='replace') if stdout_b else '',
                stderr=stderr_b.decode(errors='replace') if stderr_b else '',
            )
            if restart_result.returncode != 0:
                stderr = (restart_result.stderr or '').strip()
                stdout = (restart_result.stdout or '').strip()
                raise RuntimeError(
                    'Marzban restart failed after deploy. '
                    f'command={restart_command!r}; returncode={restart_result.returncode}; stdout={stdout}; stderr={stderr}'
                )
    except Exception as exc:
        def _do_rollback() -> None:
            try:
                if backup_path is not None and backup_path.exists():
                    _atomic_write_text(deployed_path, backup_path.read_text(encoding='utf-8'))
                elif previous_text is not None:
                    _atomic_write_text(deployed_path, previous_text)
                else:
                    with suppress(FileNotFoundError):
                        deployed_path.unlink()
            except Exception as rollback_exc:
                logger.exception('Rollback of Marzban page apply failed: %s', rollback_exc)

        await asyncio.to_thread(_do_rollback)

        async with sessionmaker.begin() as session:
            await AuditLogRepository(session).create(
                action=AuditAction.admin_action,
                actor_type=AuditActorType.admin,
                actor_tg_id=None,
                entity_type='marzban_page_apply',
                entity_id='live_page',
                details={
                    'status': 'failed',
                    'error': str(exc),
                    'backup_path': str(backup_path) if backup_path else None,
                    'deployed_path': str(deployed_path),
                    'restart_command': restart_command,
                    'restart_result': _command_result_summary(restart_result),
                    'page_snapshot': page_snapshot or {},
                },
            )
        return _redirect_with_message('/admin/marzban-page/', error=f'Live apply failed and was rolled back: {exc}')

    async with sessionmaker.begin() as session:
        await AuditLogRepository(session).create(
            action=AuditAction.admin_action,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            entity_type='marzban_page_apply',
            entity_id='live_page',
            details={
                'status': 'applied',
                'backup_path': str(backup_path) if backup_path else None,
                'deployed_path': str(deployed_path),
                'restart_command': restart_command,
                'restart_result': _command_result_summary(restart_result),
                'rendered_bytes': len(render_result.rendered_html.encode('utf-8')),
                'deployed_template_bytes': len(deploy_template_text.encode('utf-8')),
                'page_snapshot': page_snapshot or {},
            },
        )

    restart_note = ' с рестартом Marzban' if restart_command else ' без рестарта Marzban (команда не настроена)'
    return _redirect_with_message(
        '/admin/marzban-page/',
        success=f'Live-страница Marzban применена{restart_note}. Backup: {backup_path if backup_path else "не создавался"}',
    )


@router.get('/admin/marzban-page/preview', response_class=HTMLResponse, dependencies=[Depends(require_web_admin)])
async def admin_marzban_page_preview(request: Request):
    sessionmaker = request.app.state.sessionmaker
    settings = request.app.state.settings

    try:
        async with sessionmaker.begin() as session:
            renderer = MarzbanTemplateRenderer(session, settings)
            render_result = await renderer.render_preview()
        return HTMLResponse(render_result.rendered_html, status_code=200)
    except Exception as exc:
        escaped = html.escape(str(exc))
        body = f"""<!doctype html>
<html lang='ru'>
  <head>
    <meta charset='utf-8'>
    <title>Marzban preview failed</title>
    <style>
      body {{ font-family: Inter, Arial, sans-serif; background: #020617; color: #e2e8f0; margin: 0; padding: 32px; }}
      .card {{ max-width: 920px; margin: 0 auto; background: #0f172a; border: 1px solid #334155; border-radius: 20px; padding: 24px; }}
      h1 {{ margin-top: 0; font-size: 24px; }}
      p {{ color: #94a3b8; line-height: 1.6; }}
      pre {{ white-space: pre-wrap; word-break: break-word; background: #020617; border: 1px solid #334155; border-radius: 14px; padding: 16px; color: #fda4af; }}
    </style>
  </head>
  <body>
    <div class='card'>
      <h1>❌ Preview render failed</h1>
      <p>Jinja preview or Marzban page context build failed. The admin page should still stay usable; fix the error below and reload preview.</p>
      <pre>{escaped}</pre>
    </div>
  </body>
</html>"""
        return HTMLResponse(body, status_code=500)


@router.get('/admin/marzban-ops/', response_class=HTMLResponse, dependencies=[Depends(require_web_admin)])
async def admin_marzban_ops(request: Request):
    templates = request.app.state.templates
    settings = request.app.state.settings
    updater = GeodataUpdater(settings)
    try:
        raw_status = await updater.get_status()
    finally:
        await updater.close()

    geodata_status = {
        key: SimpleNamespace(
            **status.to_dict(),
            size_label=_format_file_size(status.size_bytes),
            modified_at=format_dt(status.updated_at),
        )
        for key, status in raw_status.items()
    }
    geodata_health, marzban_access_health = _marzban_ops_ui_health(settings, raw_status)

    return templates.TemplateResponse(
        request,
        'admin_marzban_ops.html',
        {
            'current_page': 'marzban_ops',
            'geodata_status': SimpleNamespace(**geodata_status),
            'geodata_health': geodata_health,
            'marzban_access_health': marzban_access_health,
            'success_message': request.query_params.get('success'),
            'error_message': request.query_params.get('error'),
        },
    )


@router.post('/admin/marzban-ops/geodata/update', dependencies=[Depends(require_web_admin)])
async def admin_marzban_geodata_update(
    request: Request,
    target: str | None = Form(default='all'),
    force: bool = Form(default=False),
):
    settings = request.app.state.settings
    updater = GeodataUpdater(settings)
    try:
        if target == 'geoip':
            result = await updater.update_geoip(force=force)
            if result.error:
                return _redirect_with_message('/admin/marzban-ops/', error=result.error)
            return _redirect_with_message('/admin/marzban-ops/', success='geoip.dat обновлен')
        if target == 'geosite':
            result = await updater.update_geosite(force=force)
            if result.error:
                return _redirect_with_message('/admin/marzban-ops/', error=result.error)
            return _redirect_with_message('/admin/marzban-ops/', success='geosite.dat обновлен')
        summary = await updater.update_all(force=force)
        if not summary.ok:
            errors = '; '.join(filter(None, [summary.geoip.error, summary.geosite.error])) or 'Ошибка обновления geodata'
            return _redirect_with_message('/admin/marzban-ops/', error=errors)
        return _redirect_with_message('/admin/marzban-ops/', success='Geodata обновлены')
    except Exception as exc:
        return _redirect_with_message('/admin/marzban-ops/', error=str(exc))
    finally:
        await updater.close()


@router.get('/admin/nodes/', response_class=HTMLResponse, dependencies=[Depends(require_web_admin)])
async def admin_nodes(request: Request):
    templates = request.app.state.templates
    sessionmaker = request.app.state.sessionmaker

    async with sessionmaker.begin() as session:
        repo = NodeRegistryRepository(session)
        nodes = await repo.list_all()

    node_rows = [_node_row_for_admin(node) for node in nodes]
    nodes_health_ok, nodes_health_message, nodes_health_state = _node_registry_ui_health(nodes)
    sync_counts = {
        'total': len(node_rows),
        'synced': sum(1 for row in node_rows if row['sync_state'] == NodeSyncState.synced.value),
        'never_synced': sum(1 for row in node_rows if row['sync_state'] == NodeSyncState.never_synced.value),
        'missing': sum(1 for row in node_rows if row['sync_state'] == NodeSyncState.missing.value),
        'error': sum(1 for row in node_rows if row['sync_state'] == NodeSyncState.error.value),
    }

    return templates.TemplateResponse(
        request,
        'admin_nodes.html',
        {
            'current_page': 'nodes',
            'nodes': nodes,
            'node_rows': node_rows,
            'health_status_options': [item.value for item in NodeHealthStatus],
            'sync_state_options': [item.value for item in NodeSyncState],
            'source_status_options': [item.value for item in NodeSourceStatus],
            'node_sync_counts': sync_counts,
            'nodes_health': {
                'ok': nodes_health_ok,
                'state': nodes_health_state,
                'message': nodes_health_message,
            },
            'success_message': request.query_params.get('success'),
            'error_message': request.query_params.get('error'),
        },
    )


@router.post('/admin/nodes/', dependencies=[Depends(require_web_admin)])
async def admin_nodes_update(
    request: Request,
    action: str | None = Form(default=None),
    node_id: int | None = Form(default=None),
    code: str | None = Form(default=None),
    display_name: str | None = Form(default=None),
    api_base_url: str | None = Form(default=None),
    subscription_base_url: str | None = Form(default=None),
    location_code: str | None = Form(default=None),
    provider_name: str | None = Form(default=None),
    transport_hint: str | None = Form(default=None),
    policy_tags_text: str | None = Form(default=None),
    capabilities_json_text: str | None = Form(default=None),
    is_enabled: bool = Form(default=False),
    is_default: bool = Form(default=False),
    priority: int | None = Form(default=None),
    weight: int | None = Form(default=None),
    sort_order: int | None = Form(default=None),
    health_status: str | None = Form(default=None),
    notes: str | None = Form(default=None),
):
    action_value = (action or '').strip()
    if not action_value:
        return _redirect_with_message('/admin/nodes/', error='Действие не указано')

    sessionmaker = request.app.state.sessionmaker
    settings = request.app.state.settings
    marzban: MarzbanClient | None = None
    try:
        if action_value == 'sync_now':
            marzban = MarzbanClient(settings)
            async with sessionmaker.begin() as session:
                audit_repo = AuditLogRepository(session)
                result = await marzban.sync_node_registry(session)
                await audit_repo.create(
                    action=AuditAction.node_registry_updated,
                    actor_type=AuditActorType.admin,
                    actor_tg_id=None,
                    entity_type='node_registry',
                    entity_id='sync',
                    details={
                        'action': 'sync_now',
                        'total_seen': result.total_seen,
                        'created': result.created,
                        'updated': result.updated,
                        'missing_marked': result.missing_marked,
                        'error_count': result.error_count,
                        'source_status_counts': result.source_status_counts,
                    },
                )
            return _redirect_with_message(
                '/admin/nodes/',
                success=(
                    f'Синхронизация узлов завершена: всего {result.total_seen}, '
                    f'создано {result.created}, обновлено {result.updated}, '
                    f'помечено missing {result.missing_marked}, ошибок {result.error_count}'
                ),
            )

        async with sessionmaker.begin() as session:
            repo = NodeRegistryRepository(session)
            audit_repo = AuditLogRepository(session)

            if action_value == 'upsert':
                capabilities_json = _parse_json_object_text(capabilities_json_text, field_label='Capabilities JSON')
                policy_tags = _parse_csv_tags(policy_tags_text)
                existing = await repo.get_by_code_for_update((code or '').strip()) if (code or '').strip() else None
                if existing is None:
                    if not (code or '').strip() or not (display_name or '').strip():
                        return _redirect_with_message('/admin/nodes/', error='Для нового node заполните code и display_name')
                    row = await repo.create(
                        code=code or '',
                        display_name=display_name or '',
                        api_base_url=api_base_url,
                        subscription_base_url=subscription_base_url,
                        location_code=location_code,
                        provider_name=provider_name,
                        transport_hint=transport_hint,
                        policy_tags=policy_tags,
                        capabilities_json=capabilities_json,
                        is_enabled=is_enabled,
                        is_default=is_default,
                        priority=priority or 100,
                        weight=weight or 100,
                        sort_order=sort_order or 100,
                        health_status=health_status or NodeHealthStatus.unknown.value,
                        notes=notes,
                    )
                else:
                    row = await repo.update(
                        existing,
                        display_name=(display_name or '').strip() and display_name or existing.display_name,
                        api_base_url=api_base_url if api_base_url is not None else existing.api_base_url,
                        subscription_base_url=subscription_base_url if subscription_base_url is not None else existing.subscription_base_url,
                        location_code=location_code if location_code is not None else existing.location_code,
                        provider_name=provider_name if provider_name is not None else existing.provider_name,
                        transport_hint=transport_hint if transport_hint is not None else existing.transport_hint,
                        policy_tags=policy_tags if policy_tags_text is not None else existing.policy_tags,
                        capabilities_json=capabilities_json if capabilities_json_text is not None else existing.capabilities_json,
                        is_enabled=is_enabled,
                        is_default=is_default,
                        priority=priority if priority is not None else existing.priority,
                        weight=weight if weight is not None else existing.weight,
                        sort_order=sort_order if sort_order is not None else existing.sort_order,
                        health_status=health_status if health_status is not None else existing.health_status,
                        notes=notes if notes is not None else existing.notes,
                    )
                await audit_repo.create(
                    action=AuditAction.node_registry_updated,
                    actor_type=AuditActorType.admin,
                    actor_tg_id=None,
                    entity_type='node_registry',
                    entity_id=str(row.id),
                    details={
                        'action': 'upsert',
                        'code': row.code,
                        'health_status': str(row.health_status),
                        'sync_state': str(getattr(getattr(row, 'sync_state', None), 'value', getattr(row, 'sync_state', None)) or NodeSyncState.never_synced.value),
                        'source_node_id': (getattr(row, 'source_node_id', None) or '').strip() or None,
                    },
                )
                return _redirect_with_message('/admin/nodes/', success='Node сохранен')

            if action_value == 'set_default':
                if node_id is None:
                    return _redirect_with_message('/admin/nodes/', error='node_id не указан')
                row = await repo.get_by_id_for_update(node_id)
                if row is None:
                    return _redirect_with_message('/admin/nodes/', error='Node не найден')
                await repo.update(row, is_default=True)
                await audit_repo.create(action=AuditAction.node_registry_updated, actor_type=AuditActorType.admin, actor_tg_id=None, entity_type='node_registry', entity_id=str(row.id), details={'action': 'set_default'})
                return _redirect_with_message('/admin/nodes/', success='Node назначен по умолчанию')

            if action_value == 'set_health':
                if node_id is None or not (health_status or '').strip():
                    return _redirect_with_message('/admin/nodes/', error='Укажите node_id и health_status')
                row = await repo.get_by_id_for_update(node_id)
                if row is None:
                    return _redirect_with_message('/admin/nodes/', error='Node не найден')
                await repo.set_health(row, health_status=health_status)
                await audit_repo.create(action=AuditAction.node_registry_updated, actor_type=AuditActorType.admin, actor_tg_id=None, entity_type='node_registry', entity_id=str(row.id), details={'action': 'set_health', 'health_status': health_status})
                return _redirect_with_message('/admin/nodes/', success='Статус node обновлен')
    except Exception as exc:
        return _redirect_with_message('/admin/nodes/', error=str(exc))
    finally:
        if marzban is not None:
            with suppress(Exception):
                await marzban.close()

    return _redirect_with_message('/admin/nodes/', error='Неизвестное действие')


@router.get('/admin/routing-profiles/', response_class=HTMLResponse, dependencies=[Depends(require_web_admin)])
async def admin_routing_profiles(
    request: Request,
    edit_id: int | None = Query(default=None),
    preview_profile_id: int | None = Query(default=None),
    preview_tags: str | None = Query(default=''),
):
    templates = request.app.state.templates
    sessionmaker = request.app.state.sessionmaker
    settings = request.app.state.settings

    async with sessionmaker.begin() as session:
        service = RoutingProfilesService(session)
        profiles = await service.list_all()
        default_profile = await service.get_default()

        edit_profile = await service.get_by_id(edit_id) if edit_id is not None else None
        effective_preview_profile_id = preview_profile_id if preview_profile_id is not None else (edit_id if edit_id is not None else None)
        routing_preview = await _build_routing_preview_context(
            session,
            settings,
            service,
            preview_profile_id=effective_preview_profile_id,
            preview_tags_text=preview_tags,
        )

    default_profile_id = getattr(default_profile, 'id', None)
    routing_health_ok, routing_health_message, routing_health_state = _routing_profiles_ui_health(profiles, default_profile_id)

    return templates.TemplateResponse(
        request,
        'admin_routing_profiles.html',
        {
            'current_page': 'routing_profiles',
            'profiles': profiles,
            'profile_rows': [_routing_profile_summary(profile) for profile in profiles],
            'default_profile_id': default_profile_id,
            'edit_profile': edit_profile,
            'edit_form': _routing_profile_form(edit_profile),
            'preview_profile_id': effective_preview_profile_id,
            'preview_tags': preview_tags or '',
            'routing_preview': routing_preview,
            'xray_restart_notice': (routing_preview or {}).get('restart_warning') if isinstance(routing_preview, dict) else _routing_xray_restart_warning_default(),
            'routing_profiles_health': {
                'ok': routing_health_ok,
                'state': routing_health_state,
                'message': routing_health_message,
            },
            'success_message': request.query_params.get('success'),
            'error_message': request.query_params.get('error'),
        },
    )


@router.post('/admin/routing-profiles/', dependencies=[Depends(require_web_admin)])
async def admin_routing_profiles_update(
    request: Request,
    action: str | None = Form(default=None),
    profile_id: int | None = Form(default=None),
    code: str | None = Form(default=None),
    title: str | None = Form(default=None),
    description: str | None = Form(default=None),
    sort_order: int | None = Form(default=None),
    match_tags_text: str | None = Form(default=None),
    config_json_text: str | None = Form(default=None),
    notes: str | None = Form(default=None),
    is_enabled: bool = Form(default=False),
    is_default: bool = Form(default=False),
):
    normalized_action = (action or '').strip().lower()
    if not normalized_action:
        return _redirect_with_message('/admin/routing-profiles/', error='Действие не указано')

    sessionmaker = request.app.state.sessionmaker
    try:
        async with sessionmaker.begin() as session:
            service = RoutingProfilesService(session)
            audit_repo = AuditLogRepository(session)

            if normalized_action == 'preview':
                if profile_id is None:
                    return _redirect_with_message('/admin/routing-profiles/', error='Сначала сохраните routing profile, затем выполните preview')
                preview_tags = quote_plus((match_tags_text or '').strip())
                return RedirectResponse(
                    url=f'/admin/routing-profiles/?edit_id={profile_id}&preview_profile_id={profile_id}&preview_tags={preview_tags}',
                    status_code=303,
                )

            if normalized_action == 'upsert':
                config_json = _parse_json_object_text(config_json_text, field_label='Config JSON')
                match_tags = _parse_csv_tags(match_tags_text)
                if profile_id is not None:
                    row = await service.get_by_id_for_update(profile_id)
                    if row is None:
                        return _redirect_with_message('/admin/routing-profiles/', error='Routing profile не найден')
                    before = _routing_profile_summary(row)
                    row = await service.update(
                        row,
                        title=title or row.title,
                        description=description,
                        is_enabled=is_enabled,
                        is_default=is_default,
                        sort_order=sort_order if sort_order is not None else row.sort_order,
                        match_tags=match_tags if match_tags_text is not None else row.match_tags,
                        config_json=config_json if config_json_text is not None else row.config_json,
                        notes=notes,
                    )
                    after = _routing_profile_summary(row)
                    details = {'action': 'update', 'before': before, 'after': after}
                else:
                    if not (code or '').strip() or not (title or '').strip():
                        return _redirect_with_message('/admin/routing-profiles/', error='Для нового routing profile заполните code и title')
                    row = await service.upsert(
                        code=code or '',
                        title=title or '',
                        description=description,
                        is_enabled=is_enabled,
                        is_default=is_default,
                        sort_order=sort_order or 100,
                        match_tags=match_tags,
                        config_json=config_json,
                        notes=notes,
                    )
                    details = {'action': 'create_or_upsert', 'after': _routing_profile_summary(row)}
                await audit_repo.create(
                    action=AuditAction.routing_profile_updated,
                    actor_type=AuditActorType.admin,
                    actor_tg_id=None,
                    entity_type='routing_profile',
                    entity_id=str(row.id),
                    details=details,
                )
                preview_tags = quote_plus(', '.join(list(getattr(row, 'match_tags', None) or [])))
                return RedirectResponse(
                    url=f'/admin/routing-profiles/?edit_id={row.id}&preview_profile_id={row.id}&preview_tags={preview_tags}&success={quote_plus("Routing profile сохранен")}',
                    status_code=303,
                )

            if normalized_action == 'set_default':
                if profile_id is None:
                    return _redirect_with_message('/admin/routing-profiles/', error='profile_id не указан')
                row = await service.get_by_id_for_update(profile_id)
                if row is None:
                    return _redirect_with_message('/admin/routing-profiles/', error='Routing profile не найден')
                await service.update(row, is_default=True)
                await audit_repo.create(
                    action=AuditAction.routing_profile_updated,
                    actor_type=AuditActorType.admin,
                    actor_tg_id=None,
                    entity_type='routing_profile',
                    entity_id=str(row.id),
                    details={'action': 'set_default', 'code': row.code},
                )
                return _redirect_with_message('/admin/routing-profiles/', success='Routing profile назначен по умолчанию')

            if normalized_action == 'delete':
                if profile_id is None:
                    return _redirect_with_message('/admin/routing-profiles/', error='profile_id не указан')
                row = await service.get_by_id_for_update(profile_id)
                if row is None:
                    return _redirect_with_message('/admin/routing-profiles/', error='Routing profile не найден')
                if bool(getattr(row, 'is_default', False)):
                    return _redirect_with_message('/admin/routing-profiles/', error='Нельзя удалить default routing profile. Сначала назначьте другой default profile.')
                snapshot = _routing_profile_summary(row)
                await session.delete(row)
                await audit_repo.create(
                    action=AuditAction.routing_profile_updated,
                    actor_type=AuditActorType.admin,
                    actor_tg_id=None,
                    entity_type='routing_profile',
                    entity_id=str(profile_id),
                    details={'action': 'delete', 'before': snapshot},
                )
                return _redirect_with_message('/admin/routing-profiles/', success='Routing profile удален')
    except (ValueError, RoutingProfileValidationError) as exc:
        return _redirect_with_message('/admin/routing-profiles/', error=str(exc))
    except Exception as exc:
        return _redirect_with_message('/admin/routing-profiles/', error=str(exc))

    return _redirect_with_message('/admin/routing-profiles/', error='Неизвестное действие')

@router.get('/admin/invoices/', response_class=HTMLResponse, dependencies=[Depends(require_web_admin)])
async def admin_invoices(
    request: Request,
    page: int = Query(default=1, ge=1),
    status: str | None = Query(default='all'),
    q: str | None = Query(default=''),
):
    sessionmaker = request.app.state.sessionmaker
    page_size = ADMIN_PAGE_SIZE
    status_filter = _invoice_status_filter_normalize(status)
    query = (q or '').strip()

    async with sessionmaker() as session:
        repo = InvoiceRepository(session)
        invoices = await repo.list_recent(limit=500, offset=0)

    filtered = [invoice for invoice in invoices if _invoice_matches_status(invoice, status_filter) and _invoice_matches_query(invoice, query)]
    offset = (page - 1) * page_size
    page_items = filtered[offset : offset + page_size + 1]
    has_next_page = len(page_items) > page_size
    page_items = page_items[:page_size]

    rows = []
    for invoice in page_items:
        status_value = getattr(getattr(invoice, 'status', None), 'value', getattr(invoice, 'status', None))
        rows.append({
            'id': getattr(invoice, 'id', None),
            'user_id': getattr(invoice, 'user_id', None),
            'purpose': getattr(getattr(invoice, 'purpose', None), 'value', getattr(invoice, 'purpose', None)) or '—',
            'status_label': _invoice_status_label(status_value),
            'status_tone': _invoice_status_tone(status_value),
            'provider': getattr(invoice, 'provider', '—') or '—',
            'external_invoice_id': getattr(invoice, 'external_invoice_id', '—') or '—',
            'payable_amount': _invoice_money_label(getattr(invoice, 'payable_amount', '—'), getattr(invoice, 'currency', None)),
            'created_at': format_dt(getattr(invoice, 'created_at', None)),
        })

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        'admin_invoices.html',
        {
            'current_page': 'invoices',
            'rows': rows,
            'status_filter': status_filter,
            'query': query,
            'success_message': request.query_params.get('success'),
            'error_message': request.query_params.get('error'),
            'page': page,
            'has_next_page': has_next_page,
            'has_prev': page > 1,
        },
    )


@router.get('/admin/invoices/{invoice_id}', response_class=HTMLResponse, dependencies=[Depends(require_web_admin)])
async def admin_invoice_detail(request: Request, invoice_id: int):
    sessionmaker = request.app.state.sessionmaker
    settings = request.app.state.settings

    async with sessionmaker() as session:
        invoice_repo = InvoiceRepository(session)
        user_repo = UserRepository(session)
        invoice = await invoice_repo.get_by_id(invoice_id)
        if invoice is None:
            raise HTTPException(status_code=404, detail='Invoice not found')
        user = await user_repo.get_by_id(getattr(invoice, 'user_id', 0))
        result = await session.execute(
            select(AuditLog)
            .where(AuditLog.entity_type == 'invoice', AuditLog.entity_id == str(invoice.id))
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(50)
        )
        audit_rows = list(result.scalars().all())

    provider_diag = await _fetch_invoice_provider_diagnostic(settings, invoice)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        'admin_invoice_detail.html',
        {
            'current_page': 'invoices',
            'invoice': invoice,
            'user': user,
            'provider_diag': provider_diag,
            'audit_rows': audit_rows,
            'success_message': request.query_params.get('success'),
            'error_message': request.query_params.get('error'),
        },
    )


@router.post('/admin/invoices/{invoice_id}/refresh', dependencies=[Depends(require_web_admin)])
async def admin_invoice_refresh(request: Request, invoice_id: int, return_to: str | None = Form(default=None)):
    sessionmaker = request.app.state.sessionmaker
    settings = request.app.state.settings
    target = _safe_return_to(return_to, fallback=f'/admin/invoices/{invoice_id}')

    async with sessionmaker() as session:
        invoice_repo = InvoiceRepository(session)
        invoice = await invoice_repo.get_by_id(invoice_id)
        if invoice is None:
            return _redirect_with_message('/admin/invoices/', error='Счет не найден')

        provider_diag = await _fetch_invoice_provider_diagnostic(settings, invoice)
        status = (provider_diag.get('status') or '').strip().lower()
        resolved_external_id = str(provider_diag.get('external_invoice_id') or getattr(invoice, 'external_invoice_id', '') or '').strip()

        if status not in {'paid', 'cancelled'}:
            message = 'Провайдер не подтвердил оплату: ' + (provider_diag.get('raw_status') or status or 'pending')
            return _redirect_with_message(target, success=message)

        service, marzban, payments = _build_payment_service(session, settings, provider_name=getattr(invoice, 'provider', None))
        try:
            result = await service.process_provider_callback(getattr(invoice, 'provider', ''), resolved_external_id, status)
        finally:
            await _close_payment_stack(marzban, payments)

    if result is None and resolved_external_id != str(getattr(invoice, 'external_invoice_id', '') or '').strip():
        async with sessionmaker() as session:
            service, marzban, payments = _build_payment_service(session, settings, provider_name=getattr(invoice, 'provider', None))
            try:
                result = await service.process_provider_callback(getattr(invoice, 'provider', ''), str(getattr(invoice, 'external_invoice_id', '') or '').strip(), status)
            finally:
                await _close_payment_stack(marzban, payments)

    if result is None:
        return _redirect_with_message(target, error='Не удалось сопоставить callback провайдера с внутренним счетом')

    return _redirect_with_message(target, success=f'Проверка завершена: {result.status_text}')


@router.post('/admin/invoices/{invoice_id}/approve', dependencies=[Depends(require_web_admin)])
async def admin_invoice_approve(request: Request, invoice_id: int, return_to: str | None = Form(default=None)):
    sessionmaker = request.app.state.sessionmaker
    settings = request.app.state.settings
    target = _safe_return_to(return_to, fallback=f'/admin/invoices/{invoice_id}')

    async with sessionmaker() as session:
        repo = InvoiceRepository(session)
        invoice = await repo.get_by_id(invoice_id)
        if invoice is None:
            return _redirect_with_message('/admin/invoices/', error='Счет не найден')
        service, marzban, payments = _build_payment_service(session, settings, provider_name=getattr(invoice, 'provider', None))
        try:
            result = await service.approve_invoice_as_admin(invoice_id, admin_tg_id=None)
        except Exception as exc:
            return _redirect_with_message(target, error=str(exc))
        finally:
            await _close_payment_stack(marzban, payments)

    return _redirect_with_message(target, success=f'Счет обработан: {result.status_text}')


@router.post('/admin/invoices/{invoice_id}/cancel', dependencies=[Depends(require_web_admin)])
async def admin_invoice_cancel(request: Request, invoice_id: int, return_to: str | None = Form(default=None)):
    sessionmaker = request.app.state.sessionmaker
    settings = request.app.state.settings
    target = _safe_return_to(return_to, fallback=f'/admin/invoices/{invoice_id}')

    async with sessionmaker() as session:
        repo = InvoiceRepository(session)
        invoice = await repo.get_by_id(invoice_id)
        if invoice is None:
            return _redirect_with_message('/admin/invoices/', error='Счет не найден')
        service, marzban, payments = _build_payment_service(session, settings, provider_name=getattr(invoice, 'provider', None))
        try:
            await service.cancel_invoice_as_admin(invoice_id, admin_tg_id=None)
        except Exception as exc:
            return _redirect_with_message(target, error=str(exc))
        finally:
            await _close_payment_stack(marzban, payments)

    return _redirect_with_message(target, success='Счет отменён')


@router.get('/admin/export/{entity}.csv', dependencies=[Depends(require_web_admin)])
async def admin_export_csv(
    request: Request,
    entity: str,
):
    sessionmaker = request.app.state.sessionmaker

    buffer, writer = _csv_writer_buffer()

    async with sessionmaker() as session:
        if entity == 'users':
            users = await UserRepository(session).list_recent(limit=CSV_EXPORT_LIMIT, offset=0)
            writer.writerow(['id', 'tg_id', 'username', 'first_name', 'last_name', 'balance', 'created_at'])
            for user in users:
                writer.writerow([
                    user.id,
                    user.tg_id,
                    _safe_csv_cell(user.username or ''),
                    _safe_csv_cell(getattr(user, 'first_name', '') or ''),
                    _safe_csv_cell(getattr(user, 'last_name', '') or ''),
                    str(user.balance or Decimal('0.00')),
                    user.created_at.isoformat() if user.created_at else '',
                ])

        elif entity == 'tickets':
            tickets = await SupportTicketRepository(session).list_recent(limit=CSV_EXPORT_LIMIT, offset=0)
            writer.writerow(['id', 'user_id', 'status', 'closed_at', 'close_reason', 'created_at'])
            for ticket in tickets:
                writer.writerow([
                    ticket.id,
                    ticket.user_id,
                    ticket.status.value,
                    ticket.closed_at.isoformat() if ticket.closed_at else '',
                    _safe_csv_cell(ticket.close_reason or ''),
                    ticket.created_at.isoformat() if ticket.created_at else '',
                ])

        elif entity == 'invoices':
            invoices = await InvoiceRepository(session).list_recent(limit=CSV_EXPORT_LIMIT, offset=0)
            writer.writerow(['id', 'user_id', 'purpose', 'status', 'provider', 'amount', 'balance_used', 'payable_amount', 'created_at'])
            for invoice in invoices:
                writer.writerow([
                    invoice.id,
                    invoice.user_id,
                    invoice.purpose.value,
                    invoice.status.value,
                    _safe_csv_cell(invoice.provider),
                    str(invoice.amount),
                    str(invoice.balance_used),
                    str(invoice.payable_amount),
                    invoice.created_at.isoformat() if invoice.created_at else '',
                ])

        elif entity == 'broadcasts':
            jobs = await BroadcastJobRepository(session).list_recent(limit=CSV_EXPORT_LIMIT, offset=0)
            writer.writerow(['id', 'created_by_tg_id', 'status', 'run_at', 'processed_users', 'sent_count', 'failed_count', 'created_at'])
            for job in jobs:
                writer.writerow([
                    job.id,
                    job.created_by_tg_id,
                    _safe_csv_cell(job.status.value),
                    job.run_at.isoformat() if job.run_at else '',
                    job.processed_users,
                    job.sent_count,
                    job.failed_count,
                    job.created_at.isoformat() if job.created_at else '',
                ])
        else:
            raise HTTPException(status_code=404, detail='Unknown export entity')

    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="{entity}.csv"'},
    )


@router.get('/subscription/{public_key}', response_class=RedirectResponse)
async def subscription_profile(request: Request, public_key: str):
    """
    Compat redirect: legacy public subscription URLs must canonicalize to /sub/<token>.
    Falls back to bot redirect only when public origin is not configured.
    """
    settings = request.app.state.settings
    return RedirectResponse(
        url=_canonical_subscription_redirect_or_bot(settings, public_key),
        status_code=302,
    )


@router.get('/profile/{service_id}', response_class=RedirectResponse)
async def legacy_subscription_profile_redirect(request: Request, service_id: str):
    """
    Compat redirect for the oldest public profile links.
    Canonical destination is /sub/<token> when the public origin is configured.
    """
    settings = request.app.state.settings
    return RedirectResponse(
        url=_canonical_subscription_redirect_or_bot(settings, service_id),
        status_code=302,
    )


@router.get('/payment/success', response_class=HTMLResponse)
async def payment_success(request: Request):
    templates = request.app.state.templates
    settings = request.app.state.settings
    return templates.TemplateResponse(
        request,
        'success.html',
        {
            'bot_username': _bot_username_or_none(settings),
        },
    )


@router.get('/payment/failed', response_class=HTMLResponse)
async def payment_failed(request: Request):
    templates = request.app.state.templates
    settings = request.app.state.settings
    return templates.TemplateResponse(
        request,
        'failed.html',
        {
            'bot_username': _bot_username_or_none(settings),
        },
    )