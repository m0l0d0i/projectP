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
import ipaddress
import secrets
import shlex
import shutil
import tempfile
import time
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote_plus, urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from sqlalchemy import select, text, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import (
    AuditAction,
    AuditActorType,
    AuditLog,
    BroadcastJobStatus,
    Invoice,
    InvoicePurpose,
    InvoiceStatus,
    LLMProviderKind,
    NodeHealthStatus,
    NodeSourceStatus,
    NodeSyncState,
    SupportTicketStatus,
    TariffVisibility,
    TransactionType,
    User,
    WebAdminRole,
)
from app.db.repositories import (
    AdminDmMessageRepository,
    AppLinkRepository,
    AppSettingsRepository,
    AuditLogRepository,
    BroadcastJobRepository,
    CannedResponseRepository,
    InvoiceRepository,
    LLMConfigRepository,
    MarzbanPageSettingsRepository,
    NotificationRuleRepository,
    OutboxRepository,
    PricingRuleRepository,
    TrafficTopupOptionRepository,
    PromoRepository,
    SubscriptionRepository,
    SupportMessageRepository,
    SupportTicketRepository,
    TariffRepository,
    TransactionRepository,
    UserRepository,
    WebAdminUserRepository,
)
from app.db.repositories.node_health import (
    NodeHealthRangePoint,
    NodeHealthSampleRepository,
)
from app.db.repositories.node_registry import NodeRegistryRepository
from app.services.broadcasts import BroadcastService, BroadcastValidationError
from app.services.geodata_updater import GeodataUpdater
from app.services.marzban import MarzbanClient
from app.services.marzban_env_manager import MarzbanEnvManager
from app.services.marzban_template_renderer import MarzbanTemplateRenderer
from app.services.privacy import PrivacyService
from app.services.node_policy import NodePolicyService
from app.services.payment_engine import PaymentService
from app.services.payments import MockPaymentProvider, PaymentProvider, PlategaProvider
from app.services.web_admin_auth import (
    MIN_PLAINTEXT_PASSWORD_LENGTH,
    hash_password,
)
from app.services.promos import PromoService
from app.services.routing_profiles import RoutingProfilesService, RoutingProfileValidationError
from app.services.subscription_urls import build_canonical_subscription_url, configured_public_subscription_origin
from app.services.subscriptions import SubscriptionService
from app.services.tariff_visibility import parse_segment_filter_text
from app.services.support_ai import (
    DEEPSEEK_DEFAULT_API_BASE_URL,
    LLMProviderError,
    LLMSecretsKeyError,
    build_provider,
    decrypt_api_key,
    encrypt_api_key,
    generate_support_draft,
    mask_api_key_preview,
)
from app.services.tariffs import PricingService
from app.observability.metrics import SUPPORT_AI_CALLS, notification_counters_snapshot
from app.utils.formatters import DISPLAY_TIMEZONE, bytes_to_gb, format_dt
from app.utils.runtime_settings import (
    effective_bool_from_row,
    effective_int_from_row,
    effective_list_from_row,
    effective_optional_int_from_row,
)
from app.web.auth import (
    WebAdminPrincipal,
    require_any,
    require_finance,
    require_finance_or_support,
    require_role,
    require_superadmin,
    require_support,
    web_admin_security,
)

router = APIRouter()
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


_LOGIN_FAILURE_WINDOW_SECONDS = 300.0
_LOGIN_FAILURE_LIMIT = 10
_LOGIN_FAILURES_MAX_TRACKED_IPS = 10_000
_login_failures: dict[str, list[float]] = {}


def _login_rate_limit_check(client_ip: str) -> tuple[bool, int]:
    """Returns (allowed, retry_after_seconds). Reads-only; does not record."""
    now = time.monotonic()
    cutoff = now - _LOGIN_FAILURE_WINDOW_SECONDS

    bucket = _login_failures.get(client_ip)
    if not bucket:
        return True, 0

    bucket = [t for t in bucket if t > cutoff]
    _login_failures[client_ip] = bucket

    if len(bucket) >= _LOGIN_FAILURE_LIMIT:
        oldest = bucket[0]
        return False, max(1, int((oldest + _LOGIN_FAILURE_WINDOW_SECONDS) - now))
    return True, 0


def _record_login_failure(client_ip: str) -> None:
    now = time.monotonic()
    cutoff = now - _LOGIN_FAILURE_WINDOW_SECONDS

    if len(_login_failures) >= _LOGIN_FAILURES_MAX_TRACKED_IPS and client_ip not in _login_failures:
        # naive eviction: drop oldest tracked IPs
        for evict_ip in list(_login_failures.keys())[: len(_login_failures) // 4]:
            del _login_failures[evict_ip]

    bucket = _login_failures.setdefault(client_ip, [])
    bucket.append(now)
    _login_failures[client_ip] = [t for t in bucket if t > cutoff]


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



def _node_latency_tone(latency_ms: int | None, *, has_probed: bool) -> str:
    """CSS-классы chip-а под latency (FEA-ADMIN-NODE-MONITOR).

    Пороги выбраны эмпирически для panel-latency (один HTTP-вызов к Marzban):
    < 200ms нормально, 200–500ms warn, > 500ms slow, fail-probe — alarm.
    """
    if not has_probed or latency_ms is None:
        return 'border-slate-700 bg-slate-950 text-slate-400'
    if latency_ms < 200:
        return 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300'
    if latency_ms < 500:
        return 'border-amber-500/30 bg-amber-500/10 text-amber-300'
    return 'border-rose-500/30 bg-rose-500/10 text-rose-300'


def _node_row_for_admin(node: Any) -> dict[str, Any]:
    raw_health = getattr(node, 'health_status', None)
    raw_source = getattr(node, 'source_status', None)
    raw_sync = getattr(node, 'sync_state', None)
    health_status = str(getattr(raw_health, 'value', raw_health) or NodeHealthStatus.unknown.value)
    source_status = str(getattr(raw_source, 'value', raw_source) or NodeSourceStatus.unknown.value)
    sync_state = str(getattr(raw_sync, 'value', raw_sync) or NodeSyncState.never_synced.value)

    last_latency_ms = getattr(node, 'last_latency_ms', None)
    last_users_online = getattr(node, 'last_users_online', None)
    last_users_total = getattr(node, 'last_users_total', None)
    last_probe_at = getattr(node, 'last_probe_at', None)
    consecutive_fail_count = int(getattr(node, 'consecutive_fail_count', 0) or 0)
    has_probed = last_probe_at is not None

    if last_latency_ms is None:
        latency_label = '—'
    else:
        latency_label = f'{int(last_latency_ms)} мс'

    if last_users_online is None and last_users_total is None:
        users_label = '—'
    else:
        online_part = str(int(last_users_online)) if last_users_online is not None else '?'
        total_part = str(int(last_users_total)) if last_users_total is not None else '?'
        users_label = f'{online_part} / {total_part}'

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
        # FEA-ADMIN-NODE-MONITOR: live-probe данные (денорм-поля).
        'last_latency_ms': last_latency_ms,
        'last_latency_label': latency_label,
        'last_latency_tone': _node_latency_tone(last_latency_ms, has_probed=has_probed),
        'last_users_online': last_users_online,
        'last_users_total': last_users_total,
        'last_users_label': users_label,
        'last_probe_at_display': format_dt(last_probe_at) if has_probed else 'Не проверялась',
        'consecutive_fail_count': consecutive_fail_count,
        'is_degraded_probe': consecutive_fail_count >= 1,
        'is_alerting_probe': consecutive_fail_count >= 5,
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
        'unlocks_tariff_id': getattr(promo, 'unlocks_tariff_id', None),
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
        'unlocks_tariff_id': getattr(promo, 'unlocks_tariff_id', None),
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
        # FEA-ADMIN-TARIFF-PLUS: visibility/окна/сегменты/private-link
        'visibility': str(getattr(getattr(plan, 'visibility', None), 'value', getattr(plan, 'visibility', 'public')) or 'public'),
        'available_from_iso': (plan.available_from.isoformat() if getattr(plan, 'available_from', None) else ''),
        'available_to_iso': (plan.available_to.isoformat() if getattr(plan, 'available_to', None) else ''),
        'available_from_label': format_dt(getattr(plan, 'available_from', None)) or '—',
        'available_to_label': format_dt(getattr(plan, 'available_to', None)) or '—',
        'segment_filter_text': (
            json.dumps(plan.segment_filter_json, ensure_ascii=False, indent=2)
            if getattr(plan, 'segment_filter_json', None) else ''
        ),
        'private_token': getattr(plan, 'private_token', None) or '',
        'accent_color': getattr(plan, 'accent_color', None) or '',
        'is_recommended': bool(getattr(plan, 'is_recommended', False)),
        'max_active_subscriptions': getattr(plan, 'max_active_subscriptions', None),
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


# FEA-ADMIN-DASHBOARD: read-only аналитика для всех ролей (RBAC=any).
# Только агрегаты — без PII (juicy invoice_id'ы, имена и т.п. остаются в
# /admin/users/, /admin/invoices/, /admin/subscriptions/).
@router.get('/admin/dashboard/', response_class=HTMLResponse, dependencies=[Depends(require_any)])
async def admin_dashboard(request: Request):
    templates = request.app.state.templates
    sessionmaker = request.app.state.sessionmaker

    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)

    async with sessionmaker() as session:
        user_repo = UserRepository(session)
        sub_repo = SubscriptionRepository(session)
        invoice_repo = InvoiceRepository(session)
        ticket_repo = SupportTicketRepository(session)

        active_subs = await sub_repo.count_alive(trial=False)
        active_trials = await sub_repo.count_alive(trial=True)
        new_users_7d = await user_repo.count_created_since(week_ago)
        new_users_30d = await user_repo.count_created_since(month_ago)
        revenue_today = await invoice_repo.sum_paid_since(today_start)
        revenue_7d = await invoice_repo.sum_paid_since(week_ago)
        revenue_30d = await invoice_repo.sum_paid_since(month_ago)
        mrr_30d = await invoice_repo.sum_paid_since(month_ago, purpose=InvoicePurpose.tariff)
        open_tickets = (
            await ticket_repo.count_for_admin(status=SupportTicketStatus.waiting_operator)
            + await ticket_repo.count_for_admin(status=SupportTicketStatus.waiting_user)
        )

    scheduler = getattr(request.app.state, 'scheduler', None)
    scheduler_running = bool(getattr(scheduler, 'running', False)) if scheduler is not None else False

    cards = [
        {'title': 'Активные подписки', 'value': active_subs, 'hint': 'без trial, expire_date в будущем'},
        {'title': 'Активные trial', 'value': active_trials, 'hint': 'trial-подписки, ещё живые'},
        {'title': 'MRR (30 дней)', 'value': f'{mrr_30d:.0f} ₽', 'hint': 'оплаченные tariff-инвойсы за 30 дней'},
        {'title': 'Выручка сегодня', 'value': f'{revenue_today:.0f} ₽', 'hint': 'все purpose, paid+consumed+applying'},
        {'title': 'Выручка за 7 дней', 'value': f'{revenue_7d:.0f} ₽', 'hint': 'все purpose'},
        {'title': 'Выручка за 30 дней', 'value': f'{revenue_30d:.0f} ₽', 'hint': 'все purpose'},
        {'title': 'Новые юзеры (7д)', 'value': new_users_7d, 'hint': f'за 30д: {new_users_30d}'},
        {'title': 'Открытые тикеты', 'value': open_tickets, 'hint': 'waiting_user + waiting_operator'},
        {'title': 'Scheduler', 'value': 'OK' if scheduler_running else '⚠️ не запущен', 'hint': 'жив только на leader-инстансе'},
    ]

    return templates.TemplateResponse(
        request,
        'admin_dashboard.html',
        {
            'current_page': 'dashboard',
            'cards': cards,
            'generated_at': format_dt(now),
        },
    )


@router.get('/admin/system/', response_class=HTMLResponse, dependencies=[Depends(require_any)])
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


@router.get('/admin/search', response_class=HTMLResponse, dependencies=[Depends(require_any)])
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


@router.get('/admin/users/', response_class=HTMLResponse, dependencies=[Depends(require_any)])
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


_USER_TIMELINE_LIMIT = 60
_USER_DM_LIMIT = 25
_USER_TICKETS_LIMIT = 25
_USER_AUDIT_LIMIT = 50


_AUDIT_TIMELINE_LABELS: dict[str, str] = {
    'user_notes_updated': '📝 Заметки админа обновлены',
    'user_tag_added': '🏷 Добавлен тег',
    'user_tag_removed': '🏷 Удалён тег',
    'user_blocked': '🚫 Пользователь заблокирован',
    'user_unblocked': '✓ Пользователь разблокирован',
    'user_trial_reset': '↻ Trial сброшен',
    'user_admin_dm_sent': '📨 Отправлен DM из админки',
    'user_force_subscription_disabled': '⛔ Подписка отключена',
    'balance_adjusted': '💰 Корректировка баланса',
    'referral_activated': '🤝 Реферал активирован',
    'promo_redeemed': '🎁 Промокод применён',
    'invoice_paid': '✅ Счёт оплачен',
    'invoice_cancelled': '❌ Счёт отменён',
    'ticket_closed': '✅ Тикет закрыт',
    'ticket_assigned': '👤 Тикет назначен',
    'ticket_tagged': '🏷 Тег тикета',
    'support_ai_generated': '🤖 AI-черновик ответа',
}


@router.get('/admin/users/{user_id}', response_class=HTMLResponse, dependencies=[Depends(require_any)])
async def admin_user_detail(request: Request, user_id: int):
    templates = request.app.state.templates
    sessionmaker = request.app.state.sessionmaker

    async with sessionmaker() as session:
        user_repo = UserRepository(session)
        subscription_repo = SubscriptionRepository(session)
        invoice_repo = InvoiceRepository(session)
        ticket_repo = SupportTicketRepository(session)
        dm_repo = AdminDmMessageRepository(session)
        audit_repo = AuditLogRepository(session)
        user = await user_repo.get_by_id(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail='User not found')
        subscriptions_raw = await subscription_repo.list_by_user_id(user_id)
        invoices_raw = await invoice_repo.list_by_user_id(user_id, limit=100)
        tickets_raw = await ticket_repo.list_by_user(user_id)
        dms_raw = await dm_repo.list_by_user(user_id, limit=_USER_DM_LIMIT)
        audit_raw = await audit_repo.list_for_user(user_id, limit=_USER_AUDIT_LIMIT)

    subscriptions = []
    for sub in subscriptions_raw:
        monthly_traffic_bytes = getattr(sub, 'monthly_traffic_bytes', None)
        if monthly_traffic_bytes is None:
            traffic_label = '♾️ Безлимит'
        else:
            traffic_label = f"{bytes_to_gb(monthly_traffic_bytes)} ГБ / мес."
        subscriptions.append(
            SimpleNamespace(
                id=getattr(sub, 'id', None),
                service_id=getattr(sub, 'service_id', '—'),
                marzban_username=getattr(sub, 'marzban_username', None),
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

    dms = [
        SimpleNamespace(
            id=dm.id,
            text=dm.text,
            status=dm.status,
            admin_username=dm.admin_username or '—',
            created_at=format_dt(dm.created_at),
            outbox_message_id=dm.outbox_message_id,
        )
        for dm in dms_raw
    ]

    timeline = _build_user_timeline(
        user_id=user_id,
        invoices_raw=invoices_raw,
        tickets_raw=tickets_raw,
        dms_raw=dms_raw,
        audit_raw=audit_raw,
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
                admin_notes=getattr(user, 'admin_notes', None) or '',
                tags=list(getattr(user, 'tags', None) or []),
                is_blocked=bool(getattr(user, 'is_blocked', False)),
                blocked_at=format_dt(getattr(user, 'blocked_at', None)),
                blocked_reason=getattr(user, 'blocked_reason', None),
                bot_blocked=bool(getattr(user, 'bot_blocked', False)),
                bot_blocked_at=format_dt(getattr(user, 'bot_blocked_at', None)),
                bot_blocked_reason=getattr(user, 'bot_blocked_reason', None),
                trial_issued_at=format_dt(getattr(user, 'trial_issued_at', None)),
                first_paid_at=format_dt(getattr(user, 'first_paid_at', None)),
            ),
            'subscriptions': subscriptions,
            'invoices': invoices,
            'dms': dms,
            'timeline': timeline,
            'success_message': request.query_params.get('success'),
            'error_message': request.query_params.get('error'),
        },
    )


def _build_user_timeline(
    *,
    user_id: int,
    invoices_raw: list,
    tickets_raw: list,
    dms_raw: list,
    audit_raw: list,
) -> list[SimpleNamespace]:
    """Собрать communication timeline для страницы пользователя.

    Источники:
    - DM-сообщения из `admin_dm_messages` (`kind='dm'`).
    - Тикеты саппорта (`kind='ticket'`).
    - Счета (`kind='invoice'`).
    - Audit-логи с `entity_type='user'` для этого пользователя
      (`kind='audit'`).

    Каждая запись — SimpleNamespace с одинаковым набором полей:
    `kind`, `created_at_label`, `created_at` (для сортировки),
    `title`, `subtitle`, `link` (опционально), `tone` (cyan/amber/
    rose/emerald/slate). Сортировка — по created_at desc; срезаем
    до `_USER_TIMELINE_LIMIT`.
    """
    items: list[SimpleNamespace] = []

    for dm in dms_raw:
        tone = 'amber' if dm.status == 'failed' else ('emerald' if dm.status == 'sent' else 'cyan')
        items.append(
            SimpleNamespace(
                kind='dm',
                created_at=dm.created_at,
                created_at_label=format_dt(dm.created_at),
                title=f'📨 DM от {dm.admin_username or "—"} ({dm.status})',
                subtitle=(dm.text or '')[:200] + ('…' if dm.text and len(dm.text) > 200 else ''),
                link=None,
                tone=tone,
            )
        )

    for ticket in tickets_raw:
        status_value = getattr(getattr(ticket, 'status', None), 'value', getattr(ticket, 'status', None))
        tone = 'emerald' if status_value == 'closed' else ('amber' if status_value == 'waiting_operator' else 'cyan')
        items.append(
            SimpleNamespace(
                kind='ticket',
                created_at=ticket.created_at,
                created_at_label=format_dt(ticket.created_at),
                title=f'🎫 Тикет #{ticket.id} ({status_value or "—"})',
                subtitle=getattr(ticket, 'hashtag', None) or '',
                link=f'/admin/tickets/{ticket.id}',
                tone=tone,
            )
        )

    for invoice in invoices_raw:
        status_value = getattr(getattr(invoice, 'status', None), 'value', getattr(invoice, 'status', None)) or '—'
        purpose_value = getattr(getattr(invoice, 'purpose', None), 'value', getattr(invoice, 'purpose', None)) or '—'
        tone = (
            'emerald' if status_value in {'paid', 'consumed'} else
            'rose' if status_value == 'cancelled' else
            'cyan'
        )
        items.append(
            SimpleNamespace(
                kind='invoice',
                created_at=invoice.created_at,
                created_at_label=format_dt(invoice.created_at),
                title=f'💳 Счёт #{invoice.id} · {purpose_value} · {status_value}',
                subtitle=f'{getattr(invoice, "payable_amount", "—")} {getattr(invoice, "currency", "")}',
                link=None,
                tone=tone,
            )
        )

    for log in audit_raw:
        action_value = getattr(getattr(log, 'action', None), 'value', getattr(log, 'action', None)) or 'admin_action'
        title = _AUDIT_TIMELINE_LABELS.get(action_value, f'⚙️ {action_value}')
        actor = getattr(log, 'actor_username', None) or '—'
        details = getattr(log, 'details', None) or {}
        # короткое summary деталей (до 160 символов) — без рекурсивного дампа JSON
        if isinstance(details, dict):
            summary_parts = [f'{k}={v}' for k, v in details.items() if k not in {'method', 'role', 'is_legacy', 'client_ip'}]
            subtitle = '; '.join(summary_parts)[:160]
        else:
            subtitle = ''
        items.append(
            SimpleNamespace(
                kind='audit',
                created_at=log.created_at,
                created_at_label=format_dt(log.created_at),
                title=f'{title} · {actor}',
                subtitle=subtitle,
                link=None,
                tone='slate',
            )
        )

    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    items.sort(key=lambda it: (it.created_at or epoch), reverse=True)
    return items[:_USER_TIMELINE_LIMIT]


@router.post('/admin/users/{user_id}/notes', dependencies=[Depends(require_support)])
async def admin_user_update_notes(
    request: Request,
    user_id: int,
    admin_notes: str = Form(default=''),
    principal: WebAdminPrincipal = Depends(require_support),
):
    sessionmaker = request.app.state.sessionmaker
    redirect = f'/admin/users/{user_id}'
    async with sessionmaker.begin() as session:
        repo = UserRepository(session)
        user = await repo.get_by_id_for_update(user_id)
        if user is None:
            return _redirect_with_message('/admin/users/', error='Пользователь не найден')
        before_len = len(user.admin_notes or '')
        await repo.set_admin_notes(user, admin_notes)
        after_len = len(user.admin_notes or '')
        await AuditLogRepository(session).create(
            action=AuditAction.user_notes_updated,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            actor_username=principal.username,
            entity_type='user',
            entity_id=str(user_id),
            details={'before_len': before_len, 'after_len': after_len},
        )
    return _redirect_with_message(redirect, success='Заметки обновлены')


@router.post('/admin/users/{user_id}/tags/add', dependencies=[Depends(require_support)])
async def admin_user_add_tag(
    request: Request,
    user_id: int,
    tag: str = Form(...),
    principal: WebAdminPrincipal = Depends(require_support),
):
    sessionmaker = request.app.state.sessionmaker
    redirect = f'/admin/users/{user_id}'
    async with sessionmaker.begin() as session:
        repo = UserRepository(session)
        user = await repo.get_by_id_for_update(user_id)
        if user is None:
            return _redirect_with_message('/admin/users/', error='Пользователь не найден')
        try:
            added = await repo.add_tag(user, tag)
        except ValueError as exc:
            return _redirect_with_message(redirect, error=str(exc))
        if not added:
            return _redirect_with_message(redirect, error=f'Тег «{tag.strip().lower()}» уже есть')
        await AuditLogRepository(session).create(
            action=AuditAction.user_tag_added,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            actor_username=principal.username,
            entity_type='user',
            entity_id=str(user_id),
            details={'tag': tag.strip().lower(), 'tags_after': list(user.tags or [])},
        )
    return _redirect_with_message(redirect, success=f'Тег «{tag.strip().lower()}» добавлен')


@router.post('/admin/users/{user_id}/tags/remove', dependencies=[Depends(require_support)])
async def admin_user_remove_tag(
    request: Request,
    user_id: int,
    tag: str = Form(...),
    principal: WebAdminPrincipal = Depends(require_support),
):
    sessionmaker = request.app.state.sessionmaker
    redirect = f'/admin/users/{user_id}'
    async with sessionmaker.begin() as session:
        repo = UserRepository(session)
        user = await repo.get_by_id_for_update(user_id)
        if user is None:
            return _redirect_with_message('/admin/users/', error='Пользователь не найден')
        removed = await repo.remove_tag(user, tag)
        if not removed:
            return _redirect_with_message(redirect, error=f'Тег «{tag}» не найден')
        await AuditLogRepository(session).create(
            action=AuditAction.user_tag_removed,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            actor_username=principal.username,
            entity_type='user',
            entity_id=str(user_id),
            details={'tag': tag.strip().lower(), 'tags_after': list(user.tags or [])},
        )
    return _redirect_with_message(redirect, success=f'Тег «{tag.strip().lower()}» удалён')


@router.post('/admin/users/{user_id}/block', dependencies=[Depends(require_support)])
async def admin_user_block(
    request: Request,
    user_id: int,
    reason: str = Form(default=''),
    principal: WebAdminPrincipal = Depends(require_support),
):
    sessionmaker = request.app.state.sessionmaker
    redirect = f'/admin/users/{user_id}'
    normalized_reason = (reason or '').strip()[:255] or None
    async with sessionmaker.begin() as session:
        repo = UserRepository(session)
        user = await repo.get_by_id_for_update(user_id)
        if user is None:
            return _redirect_with_message('/admin/users/', error='Пользователь не найден')
        if user.is_blocked:
            return _redirect_with_message(redirect, error='Пользователь уже заблокирован')
        await repo.set_blocked(user, True, normalized_reason)
        await AuditLogRepository(session).create(
            action=AuditAction.user_blocked,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            actor_username=principal.username,
            entity_type='user',
            entity_id=str(user_id),
            details={'reason': normalized_reason},
        )
    return _redirect_with_message(redirect, success='Пользователь заблокирован')


@router.post('/admin/users/{user_id}/unblock', dependencies=[Depends(require_support)])
async def admin_user_unblock(
    request: Request,
    user_id: int,
    principal: WebAdminPrincipal = Depends(require_support),
):
    sessionmaker = request.app.state.sessionmaker
    redirect = f'/admin/users/{user_id}'
    async with sessionmaker.begin() as session:
        repo = UserRepository(session)
        user = await repo.get_by_id_for_update(user_id)
        if user is None:
            return _redirect_with_message('/admin/users/', error='Пользователь не найден')
        if not user.is_blocked:
            return _redirect_with_message(redirect, error='Пользователь не заблокирован')
        previous_reason = user.blocked_reason
        await repo.set_blocked(user, False, None)
        await AuditLogRepository(session).create(
            action=AuditAction.user_unblocked,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            actor_username=principal.username,
            entity_type='user',
            entity_id=str(user_id),
            details={'previous_reason': previous_reason},
        )
    return _redirect_with_message(redirect, success='Пользователь разблокирован')


@router.get('/admin/users/{user_id}/export.json', dependencies=[Depends(require_support)])
async def admin_user_export_data(
    request: Request,
    user_id: int,
    principal: WebAdminPrincipal = Depends(require_support),
):
    """CMP-1: GDPR-style JSON-экспорт PII пользователя.
    Audit пишется в отдельной транзакции после построения payload'а.
    """
    sessionmaker = request.app.state.sessionmaker
    settings: Settings = request.app.state.settings

    async with sessionmaker() as session:
        user = await UserRepository(session).get_by_id(user_id)
        if user is None:
            return _redirect_with_message('/admin/users/', error='Пользователь не найден')
        privacy = PrivacyService(session, settings)
        data = await privacy.export_user_data(user)

    async with sessionmaker.begin() as session:
        await AuditLogRepository(session).create(
            action=AuditAction.user_data_exported,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            actor_username=principal.username,
            entity_type='user',
            entity_id=str(user_id),
            details={'source': 'admin_panel'},
        )

    payload = json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')
    return Response(
        content=payload,
        media_type='application/json',
        headers={
            'Content-Disposition': f'attachment; filename="vpn_bot_user_{user_id}_export.json"',
            'Cache-Control': 'no-store',
        },
    )


@router.post('/admin/users/{user_id}/erase', dependencies=[Depends(require_superadmin)])
async def admin_user_erase(
    request: Request,
    user_id: int,
    confirm: str = Form(default=''),
    principal: WebAdminPrincipal = Depends(require_superadmin),
):
    """CMP-1: GDPR-style anonymize-erase. Только superadmin — операция
    необратима. Требует confirm='ERASE' в форме (защита от случайного
    клика).
    """
    sessionmaker = request.app.state.sessionmaker
    settings: Settings = request.app.state.settings
    redirect = f'/admin/users/{user_id}'

    if confirm.strip() != 'ERASE':
        return _redirect_with_message(redirect, error='Подтверждение не пройдено: введите ERASE')

    marzban_client: MarzbanClient | None = None
    if settings.marzban_enabled:
        marzban_client = MarzbanClient(settings)

    try:
        async with sessionmaker.begin() as session:
            user = await UserRepository(session).get_by_id_for_update(user_id)
            if user is None:
                return _redirect_with_message('/admin/users/', error='Пользователь не найден')
            if user.anonymized_at is not None:
                return _redirect_with_message(redirect, error='Аккаунт уже анонимизирован')
            privacy = PrivacyService(session, settings, marzban=marzban_client)
            await privacy.erase_user(
                user,
                actor_tg_id=None,
                actor_username=principal.username,
                actor_type=AuditActorType.admin,
            )
    finally:
        if marzban_client is not None:
            with suppress(Exception):
                await marzban_client.close()

    return _redirect_with_message('/admin/users/', success=f'Аккаунт {user_id} анонимизирован')


@router.post('/admin/users/{user_id}/trial-reset', dependencies=[Depends(require_support)])
async def admin_user_reset_trial(
    request: Request,
    user_id: int,
    principal: WebAdminPrincipal = Depends(require_support),
):
    sessionmaker = request.app.state.sessionmaker
    redirect = f'/admin/users/{user_id}'
    async with sessionmaker.begin() as session:
        repo = UserRepository(session)
        user = await repo.get_by_id_for_update(user_id)
        if user is None:
            return _redirect_with_message('/admin/users/', error='Пользователь не найден')
        previous_at = user.trial_issued_at
        reset_done = await repo.reset_trial(user)
        if not reset_done:
            return _redirect_with_message(redirect, error='У пользователя ещё не было выдан trial — сбрасывать нечего')
        await AuditLogRepository(session).create(
            action=AuditAction.user_trial_reset,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            actor_username=principal.username,
            entity_type='user',
            entity_id=str(user_id),
            details={'previous_trial_issued_at': previous_at.isoformat() if previous_at else None},
        )
    return _redirect_with_message(redirect, success='Trial сброшен — пользователь сможет получить новый')


@router.post('/admin/users/{user_id}/dm', dependencies=[Depends(require_support)])
async def admin_user_send_dm(
    request: Request,
    user_id: int,
    text: str = Form(...),
    principal: WebAdminPrincipal = Depends(require_support),
):
    """Отправить DM из админки. Атомарно создаёт запись в
    `admin_dm_messages` и enqueue в `outbox_messages` (доставка через
    общий outbox-worker с retry/backoff). Status DM-записи — `queued`;
    обновляется до `failed`, если outbox.enqueue вернёт None
    (correlation-key-конфликт ИЛИ другой сбой).
    """
    sessionmaker = request.app.state.sessionmaker
    redirect = f'/admin/users/{user_id}'
    try:
        normalized_text = AdminDmMessageRepository._normalize_text(text)
    except ValueError as exc:
        return _redirect_with_message(redirect, error=str(exc))

    correlation_key = f'admin_dm:{user_id}:{int(time.time() * 1000)}:{secrets.token_hex(4)}'

    async with sessionmaker.begin() as session:
        user_repo = UserRepository(session)
        outbox_repo = OutboxRepository(session)
        dm_repo = AdminDmMessageRepository(session)
        audit_repo = AuditLogRepository(session)

        user = await user_repo.get_by_id(user_id)
        if user is None:
            return _redirect_with_message('/admin/users/', error='Пользователь не найден')

        outbox_row = await outbox_repo.enqueue_tg_message(
            chat_id=user.tg_id,
            text=normalized_text,
            user_id=user.id,
            correlation_key=correlation_key,
        )
        status = 'queued' if outbox_row is not None else 'failed'

        dm_row = await dm_repo.create(
            user_id=user.id,
            text=normalized_text,
            status=status,
            admin_id=principal.db_id,
            admin_username=principal.username,
            outbox_message_id=outbox_row.id if outbox_row is not None else None,
        )
        await audit_repo.create(
            action=AuditAction.user_admin_dm_sent,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            actor_username=principal.username,
            entity_type='user',
            entity_id=str(user_id),
            details={
                'dm_id': dm_row.id,
                'text_len': len(normalized_text),
                'status': status,
                'outbox_message_id': outbox_row.id if outbox_row is not None else None,
            },
        )

    if status == 'failed':
        return _redirect_with_message(redirect, error='Не удалось поставить DM в очередь — проверьте логи')
    return _redirect_with_message(redirect, success='DM поставлен в очередь outbox')


@router.post(
    '/admin/users/{user_id}/subscriptions/{subscription_id}/force-disable',
    dependencies=[Depends(require_support)],
)
async def admin_user_force_disable_subscription(
    request: Request,
    user_id: int,
    subscription_id: int,
    reason: str = Form(default=''),
    principal: WebAdminPrincipal = Depends(require_support),
):
    """Force-cancel подписки: `is_active=False` локально + статус
    `disabled` в Marzban. Marzban-вызов идёт через circuit breaker
    клиента; при OPEN-circuit или сетевой ошибке локальный флаг
    остаётся True и UI получает понятную ошибку (НЕ полу-применяем).
    """
    sessionmaker = request.app.state.sessionmaker
    settings: Settings = request.app.state.settings
    redirect = f'/admin/users/{user_id}'
    normalized_reason = (reason or '').strip()[:255] or 'force_disabled_by_admin'

    async with sessionmaker() as session:
        sub = await SubscriptionRepository(session).get_by_id(subscription_id)
        if sub is None or sub.user_id != user_id:
            return _redirect_with_message(redirect, error='Подписка не найдена для этого пользователя')
        if not sub.is_active:
            return _redirect_with_message(redirect, error='Подписка уже неактивна')
        marzban_username = sub.marzban_username

    # Marzban-вызов вне транзакции — не держим row-lock, пока ходим в внешний API.
    if settings.marzban_enabled and marzban_username:
        marzban = MarzbanClient(settings)
        try:
            await marzban.safe_modify_user(marzban_username, status='disabled')
        except Exception as exc:  # noqa: BLE001
            logger.exception('Force-disable Marzban failed user_id=%s sub_id=%s', user_id, subscription_id)
            with suppress(Exception):
                await marzban.close()
            return _redirect_with_message(
                redirect,
                error=f'Marzban-вызов не удался: {exc}. Локальный статус не изменён.',
            )
        with suppress(Exception):
            await marzban.close()

    async with sessionmaker.begin() as session:
        sub_repo = SubscriptionRepository(session)
        sub = await sub_repo.get_by_id_for_update(subscription_id)
        if sub is None or sub.user_id != user_id:
            return _redirect_with_message(redirect, error='Подписка не найдена')
        sub.is_active = False
        await session.flush()
        await AuditLogRepository(session).create(
            action=AuditAction.user_force_subscription_disabled,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            actor_username=principal.username,
            entity_type='subscription',
            entity_id=str(subscription_id),
            details={
                'user_id': user_id,
                'service_id': sub.service_id,
                'marzban_username': marzban_username,
                'reason': normalized_reason,
                'marzban_called': bool(settings.marzban_enabled and marzban_username),
            },
        )
    return _redirect_with_message(redirect, success=f'Подписка {sub.service_id} отключена')


@router.post('/admin/users/{user_id}/balance', dependencies=[Depends(require_finance)])
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


@router.get('/admin/people/', response_class=HTMLResponse, dependencies=[Depends(require_any)])
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


@router.post('/admin/people/', dependencies=[Depends(require_superadmin)])
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


@router.post('/admin/people/test-support-chat', dependencies=[Depends(require_superadmin)])
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


@router.get('/admin/pricing/', response_class=HTMLResponse, dependencies=[Depends(require_any)])
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
            'visibility_options': [
                ('public', 'Public · виден всем'),
                ('code_only', 'Code-only · только при unlock через промокод/админа'),
                ('segment_only', 'Segment-only · по DSL `segment_filter_json`'),
                ('private_link', 'Private-link · только через deep-link/админский unlock'),
            ],
            'bot_username': request.app.state.settings.bot_username,
            'success_message': request.query_params.get('success'),
            'error_message': request.query_params.get('error'),
        },
    )


@router.post('/admin/pricing/', dependencies=[Depends(require_finance)])
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


# --- FEA-ADMIN-TARIFF-PLUS: visibility / окна / сегменты ---------------------

def _parse_iso_form_datetime(value: str | None, *, field_label: str) -> datetime | None:
    raw = (value or '').strip()
    if not raw:
        return None
    try:
        # html5 datetime-local → 'YYYY-MM-DDTHH:MM' без таймзоны → читаем как UTC
        parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError(f'{field_label}: ожидается ISO-8601 datetime') from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@router.post('/admin/pricing/tariff/{tariff_id}/visibility', dependencies=[Depends(require_finance)])
async def admin_tariff_visibility_update(
    request: Request,
    tariff_id: int,
    visibility: str = Form(...),
    available_from: str = Form(default=''),
    available_to: str = Form(default=''),
    segment_filter_text: str = Form(default=''),
    accent_color: str = Form(default=''),
    is_recommended: str = Form(default=''),
    max_active_subscriptions: str = Form(default=''),
    private_token_action: str = Form(default=''),  # '', 'generate', 'clear'
    principal: WebAdminPrincipal = Depends(require_finance),
):
    """Обновить visibility-поля тарифа (FEA-ADMIN-TARIFF-PLUS #2).

    Отдельный POST на каждый тариф, чтобы не разрастать гигантский
    `admin_pricing_update` ещё больше. Все поля валидируются и
    нормализуются здесь; на стороне БД дополнительно работают CHECK
    (`available_from <= available_to`, `max_active_subscriptions ≥ 1`).
    """
    sessionmaker = request.app.state.sessionmaker
    redirect = '/admin/pricing/'
    normalized_visibility = (visibility or '').strip().lower()
    try:
        visibility_enum = TariffVisibility(normalized_visibility)
    except ValueError:
        return _redirect_with_message(redirect, error=f'Неизвестная visibility «{visibility}»')

    try:
        available_from_dt = _parse_iso_form_datetime(available_from, field_label='available_from')
        available_to_dt = _parse_iso_form_datetime(available_to, field_label='available_to')
    except ValueError as exc:
        return _redirect_with_message(redirect, error=str(exc))
    if available_from_dt and available_to_dt and available_from_dt > available_to_dt:
        return _redirect_with_message(redirect, error='available_from должен быть ≤ available_to')

    try:
        segment_payload = parse_segment_filter_text(segment_filter_text)
    except ValueError as exc:
        return _redirect_with_message(redirect, error=str(exc))

    accent = (accent_color or '').strip() or None
    if accent and (len(accent) > 16 or not accent.startswith('#')):
        return _redirect_with_message(redirect, error='accent_color: ожидается hex-цвет вида #RRGGBB')

    cap_raw = (max_active_subscriptions or '').strip()
    cap_value: int | None = None
    if cap_raw:
        try:
            cap_value = int(cap_raw)
        except ValueError:
            return _redirect_with_message(redirect, error='max_active_subscriptions: ожидается целое число')
        if cap_value < 1:
            return _redirect_with_message(redirect, error='max_active_subscriptions: должен быть ≥ 1')

    async with sessionmaker.begin() as session:
        repo = TariffRepository(session)
        plan = await repo.get_by_id_for_update(tariff_id)
        if plan is None:
            return _redirect_with_message(redirect, error='Тариф не найден')

        before = {
            'visibility': str(getattr(plan.visibility, 'value', plan.visibility)),
            'available_from': plan.available_from.isoformat() if plan.available_from else None,
            'available_to': plan.available_to.isoformat() if plan.available_to else None,
            'segment_filter_json': plan.segment_filter_json,
            'private_token_present': bool(plan.private_token),
            'accent_color': plan.accent_color,
            'is_recommended': plan.is_recommended,
            'max_active_subscriptions': plan.max_active_subscriptions,
        }

        plan.visibility = visibility_enum
        plan.available_from = available_from_dt
        plan.available_to = available_to_dt
        plan.segment_filter_json = segment_payload
        plan.accent_color = accent
        plan.is_recommended = bool(is_recommended)
        plan.max_active_subscriptions = cap_value

        action = (private_token_action or '').strip().lower()
        if action == 'generate':
            plan.private_token = secrets.token_urlsafe(24)
        elif action == 'clear':
            plan.private_token = None
        # else: оставляем как было

        await session.flush()
        await AuditLogRepository(session).create(
            action=AuditAction.tariff_visibility_updated,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            actor_username=principal.username,
            entity_type='tariff_plan',
            entity_id=str(tariff_id),
            details={
                'before': before,
                'after': {
                    'visibility': plan.visibility.value,
                    'available_from': plan.available_from.isoformat() if plan.available_from else None,
                    'available_to': plan.available_to.isoformat() if plan.available_to else None,
                    'segment_filter_json': plan.segment_filter_json,
                    'private_token_present': bool(plan.private_token),
                    'accent_color': plan.accent_color,
                    'is_recommended': plan.is_recommended,
                    'max_active_subscriptions': plan.max_active_subscriptions,
                },
                'private_token_action': action or None,
            },
        )
    return _redirect_with_message(redirect, success=f'Видимость тарифа #{tariff_id} обновлена')


@router.get('/admin/trial/', response_class=HTMLResponse, dependencies=[Depends(require_any)])
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


@router.post('/admin/trial/', dependencies=[Depends(require_superadmin)])
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


@router.post('/admin/trial/force-reissue-all', dependencies=[Depends(require_superadmin)])
async def admin_trial_force_reissue_all(request: Request):
    """Сбрасывает `trial_issued_at = NULL` для всех active-юзеров
    (FEA-ADMIN-CRUD-EXPAND). Не трогает существующие подписки.

    После этого пользователь снова сможет получить trial через бот.
    Active = `bot_blocked=False AND is_blocked=False`. Пользователи,
    у которых `trial_issued_at` уже NULL, не считаются (count показывает
    реально сброшенные).

    Реализовано через bulk-UPDATE (один SQL-запрос вместо N+1
    `reset_trial(user)`). Один audit-лог `admin_action` на всю операцию
    с `details.action='trial_reset_bulk_all_active'`, count и cutoff_filter.
    """
    sessionmaker = request.app.state.sessionmaker

    async with sessionmaker.begin() as session:
        result = await session.execute(
            sa_update(User)
            .where(
                User.bot_blocked.is_(False),
                User.is_blocked.is_(False),
                User.trial_issued_at.is_not(None),
            )
            .values(trial_issued_at=None)
            .execution_options(synchronize_session=False)
        )
        affected = int(result.rowcount or 0)

        await AuditLogRepository(session).create(
            action=AuditAction.admin_action,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            entity_type='user',
            entity_id='bulk',
            details={
                'action': 'trial_reset_bulk_all_active',
                'affected_users': affected,
                'cutoff_filter': 'bot_blocked=False AND is_blocked=False AND trial_issued_at IS NOT NULL',
            },
        )

    if affected == 0:
        return _redirect_with_message(
            '/admin/trial/',
            success='У всех active-пользователей trial_issued_at и так пуст — нечего сбрасывать',
        )
    return _redirect_with_message(
        '/admin/trial/',
        success=f'Сброшено trial_issued_at для {affected} active-пользователей. Они снова смогут получить trial через бот.',
    )


@router.get('/admin/antispam/', response_class=HTMLResponse, dependencies=[Depends(require_any)])
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


@router.post('/admin/antispam/', dependencies=[Depends(require_superadmin)])
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

@router.get('/admin/rules/', response_class=HTMLResponse, dependencies=[Depends(require_any)])
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


@router.post('/admin/rules/', dependencies=[Depends(require_superadmin)])
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


_NOTIFICATION_PREVIEW_CONTEXT: dict[str, Any] = {
    'tariff_name': 'Премиум 1 месяц',
    'tariff_price': '299.00 ₽',
    'expire_date': '12.05.2026 23:59',
    'expire_at': '12.05.2026 23:59',
    'days_left': 3,
    'hours_left': 2,
    'traffic_used_gb': 90,
    'traffic_limit_gb': 100,
    'traffic_remaining_gb': 10,
    'percent_used': 90,
    'username': 'alice',
    'first_name': 'Алиса',
    'subscription_id': 42,
    'user_id': 1234567,
    'tg_id': 1234567,
    'topup_50_url': 'https://t.me/your_bot?start=topup_50',
    'topup_100_url': 'https://t.me/your_bot?start=topup_100',
    'renew_url': 'https://t.me/your_bot?start=renew',
}


class _PreviewUndefined:
    """Безопасная Jinja-undefined: рендерит `⟨name⟩` вместо ошибки."""

    __slots__ = ('_name',)

    def __init__(self, name: str = 'undefined') -> None:
        self._name = name

    def __getattr__(self, attr: str) -> '_PreviewUndefined':
        return _PreviewUndefined(f'{self._name}.{attr}')

    def __getitem__(self, key: object) -> '_PreviewUndefined':
        return _PreviewUndefined(f'{self._name}[{key}]')

    def __str__(self) -> str:
        return f'⟨{self._name}⟩'

    def __bool__(self) -> bool:
        return False

    def __iter__(self):
        return iter(())


def _build_preview_context() -> dict[str, Any]:
    """Контекст-заглушка для preview-рендера; реальные переменные перекрывают
    placeholder'ы из `_NOTIFICATION_PREVIEW_CONTEXT`, отсутствующие — попадают
    в `_PreviewUndefined`-логику Jinja."""
    return dict(_NOTIFICATION_PREVIEW_CONTEXT)


def _render_notification_preview(template_text: str) -> tuple[str | None, str | None]:
    """Возвращает (rendered_text, error_message). Использует тот же
    SandboxedEnvironment что и dispatcher, но с `_PreviewUndefined`."""
    from jinja2.exceptions import TemplateError as _Jinja2TemplateError
    from jinja2.sandbox import SandboxedEnvironment as _SandboxedEnvironment

    env = _SandboxedEnvironment(autoescape=False, keep_trailing_newline=True)

    ctx = _build_preview_context()

    class _Resolver(dict):
        def __missing__(self, key):
            return _PreviewUndefined(name=key)

    try:
        template = env.from_string(template_text)
        rendered = template.render(_Resolver(ctx))
        return rendered, None
    except _Jinja2TemplateError as exc:
        return None, str(exc)
    except Exception as exc:  # pragma: no cover — defensive
        return None, f'{type(exc).__name__}: {exc}'


def _validate_notification_keyboard_json(parsed: object) -> str | None:
    """Структурная валидация: list of lists of dicts с text + url|callback_data.
    Возвращает None при успехе, иначе сообщение об ошибке."""
    if not isinstance(parsed, list) or not parsed:
        return 'Клавиатура должна быть непустым массивом строк'
    for row_idx, row in enumerate(parsed, start=1):
        if not isinstance(row, list) or not row:
            return f'Строка {row_idx} должна быть непустым массивом кнопок'
        for btn_idx, btn in enumerate(row, start=1):
            if not isinstance(btn, dict):
                return f'Кнопка {row_idx}.{btn_idx}: ожидался объект'
            text_raw = btn.get('text')
            if not isinstance(text_raw, str) or not text_raw.strip():
                return f'Кнопка {row_idx}.{btn_idx}: поле "text" обязательно'
            url_raw = btn.get('url')
            cb_raw = btn.get('callback_data')
            has_url = isinstance(url_raw, str) and url_raw.strip()
            has_cb = isinstance(cb_raw, str) and cb_raw.strip()
            if not (has_url or has_cb):
                return (
                    f'Кнопка {row_idx}.{btn_idx}: нужен хотя бы один из'
                    ' "url" или "callback_data"'
                )
    return None


def _format_cooldown(seconds: int) -> str:
    """Человеко-читаемая длительность cooldown'а: 0 → '—', 86400 → '24ч'."""
    if seconds <= 0:
        return '—'
    if seconds % 86400 == 0:
        return f'{seconds // 86400}д'
    if seconds % 3600 == 0:
        return f'{seconds // 3600}ч'
    if seconds % 60 == 0:
        return f'{seconds // 60}м'
    return f'{seconds}с'


def _notification_rule_view(
    rule,
    counters: dict[str, dict[str, float]] | None = None,
    *,
    with_preview: bool = False,
):
    counters = counters or {}
    rule_counters = counters.get(rule.code, {})
    sent_total = int(
        rule_counters.get('sent_ok', 0.0) + rule_counters.get('sent_fallback', 0.0)
    )
    blocked_total = int(
        sum(value for key, value in rule_counters.items() if key.startswith('blocked_'))
    )

    keyboard_preview = None
    if rule.template_keyboard_json is not None:
        try:
            keyboard_preview = json.dumps(
                rule.template_keyboard_json, ensure_ascii=False, indent=2,
            )
        except (TypeError, ValueError):
            keyboard_preview = repr(rule.template_keyboard_json)

    segment_preview = None
    if rule.segment_filter_json is not None:
        try:
            segment_preview = json.dumps(
                rule.segment_filter_json, ensure_ascii=False, indent=2,
            )
        except (TypeError, ValueError):
            segment_preview = repr(rule.segment_filter_json)

    preview_text: str | None = None
    preview_error: str | None = None
    if with_preview:
        preview_text, preview_error = _render_notification_preview(rule.template_text)

    return SimpleNamespace(
        id=rule.id,
        code=rule.code,
        is_enabled=rule.is_enabled,
        template_text=rule.template_text,
        template_keyboard_json=rule.template_keyboard_json,
        keyboard_preview=keyboard_preview,
        segment_preview=segment_preview,
        cooldown_seconds=rule.cooldown_seconds,
        cooldown_label=_format_cooldown(rule.cooldown_seconds),
        priority=rule.priority,
        description=rule.description,
        updated_at=format_dt(getattr(rule, 'updated_at', None)),
        sent_total=sent_total,
        blocked_total=blocked_total,
        sent_ok=int(rule_counters.get('sent_ok', 0.0)),
        sent_fallback=int(rule_counters.get('sent_fallback', 0.0)),
        blocked_disabled=int(rule_counters.get('blocked_disabled', 0.0)),
        blocked_cooldown=int(rule_counters.get('blocked_cooldown', 0.0)),
        blocked_template_error=int(rule_counters.get('blocked_template_error', 0.0)),
        blocked_snoozed=int(rule_counters.get('blocked_snoozed', 0.0)),
        preview_text=preview_text,
        preview_error=preview_error,
    )


@router.get('/admin/notifications/', response_class=HTMLResponse, dependencies=[Depends(require_any)])
async def admin_notifications(request: Request):
    templates = request.app.state.templates
    sessionmaker = request.app.state.sessionmaker

    counters = notification_counters_snapshot()

    async with sessionmaker() as session:
        rules = await NotificationRuleRepository(session).list_all()

    rule_views = [_notification_rule_view(rule, counters) for rule in rules]

    enabled_count = sum(1 for r in rule_views if r.is_enabled)
    sent_total = sum(r.sent_total for r in rule_views)
    blocked_total = sum(r.blocked_total for r in rule_views)

    return templates.TemplateResponse(
        request,
        'admin_notifications.html',
        {
            'current_page': 'notifications',
            'rules': rule_views,
            'enabled_count': enabled_count,
            'rules_total': len(rule_views),
            'sent_total': sent_total,
            'blocked_total': blocked_total,
            'success_message': request.query_params.get('success'),
            'error_message': request.query_params.get('error'),
        },
    )


@router.get(
    '/admin/notifications/{code}',
    response_class=HTMLResponse,
    dependencies=[Depends(require_any)],
)
async def admin_notification_detail(request: Request, code: str):
    templates = request.app.state.templates
    sessionmaker = request.app.state.sessionmaker

    counters = notification_counters_snapshot()

    async with sessionmaker() as session:
        rule = await NotificationRuleRepository(session).get_by_code(code)

    if rule is None:
        return _redirect_with_message(
            '/admin/notifications/',
            error=f'Правило с кодом «{code}» не найдено',
        )

    rule_view = _notification_rule_view(rule, counters, with_preview=True)

    return templates.TemplateResponse(
        request,
        'admin_notification_detail.html',
        {
            'current_page': 'notifications',
            'rule': rule_view,
            'success_message': request.query_params.get('success'),
            'error_message': request.query_params.get('error'),
        },
    )


@router.post(
    '/admin/notifications/{code}/toggle',
    dependencies=[Depends(require_support)],
)
async def admin_notification_toggle(request: Request, code: str):
    sessionmaker = request.app.state.sessionmaker

    async with sessionmaker.begin() as session:
        repo = NotificationRuleRepository(session)
        audit_repo = AuditLogRepository(session)
        rule = await repo.get_by_code(code)
        if rule is None:
            return _redirect_with_message(
                '/admin/notifications/',
                error=f'Правило с кодом «{code}» не найдено',
            )

        new_state = not rule.is_enabled
        await repo.update_rule(rule, is_enabled=new_state)
        await audit_repo.create(
            action=AuditAction.notification_rule_toggled,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            entity_type='notification_rule',
            entity_id=str(rule.id),
            details={'code': rule.code, 'is_enabled': new_state},
        )

    state_label = 'включено' if new_state else 'выключено'
    return _redirect_with_message(
        f'/admin/notifications/{code}',
        success=f'Правило «{code}» {state_label}',
    )


def _resolve_admin_tg_id_for_test(app_settings_view, settings: Settings) -> int | None:
    """Первый tg_id из app_settings.admin_ids; fallback — settings.admin_ids."""
    candidates: list[int] = []
    raw = getattr(app_settings_view, 'admin_ids', None) or []
    for value in raw:
        try:
            candidates.append(int(value))
        except (TypeError, ValueError):
            continue
    if candidates:
        return candidates[0]
    for value in settings.admin_ids or []:
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


@router.post(
    '/admin/notifications/{code}/test-send',
    dependencies=[Depends(require_support)],
)
async def admin_notification_test_send(request: Request, code: str):
    sessionmaker = request.app.state.sessionmaker
    settings = request.app.state.settings
    redirect_path = f'/admin/notifications/{code}'

    async with sessionmaker() as session:
        app_settings = _app_settings_view(
            await AppSettingsRepository(session).get(), settings,
        )
        rule = await NotificationRuleRepository(session).get_by_code(code)

    if rule is None:
        return _redirect_with_message(
            '/admin/notifications/',
            error=f'Правило с кодом «{code}» не найдено',
        )

    target_tg_id = _resolve_admin_tg_id_for_test(app_settings, settings)
    if target_tg_id is None:
        return _redirect_with_message(
            redirect_path,
            error='Не задан admin_ids — некому отправить тестовое сообщение',
        )

    redis_client = (
        getattr(request.app.state, 'redis', None)
        or getattr(request.app.state, 'redis_client', None)
    )
    redis_prefix = getattr(settings, 'redis_prefix', 'vpn_bot')

    from app.services.notification_dispatcher import NotificationDispatcher as _Dispatcher
    dispatcher = _Dispatcher(redis_client=redis_client, redis_prefix=redis_prefix)

    correlation_key = f'admin-test:{code}:{int(time.time() * 1000)}'

    async with sessionmaker.begin() as session:
        try:
            sent = await dispatcher.dispatch(
                session=session,
                code=code,
                chat_id=target_tg_id,
                user_id=target_tg_id,
                default_text=rule.template_text,
                default_reply_markup=None,
                default_parse_mode=None,
                context=_build_preview_context(),
                correlation_key=correlation_key,
                force=True,
            )
        except Exception:
            logger.exception('Test-send failed for notification rule %s', code)
            return _redirect_with_message(
                redirect_path,
                error='Не удалось поставить тест в outbox — смотрите логи сервиса',
            )

        if not sent:
            return _redirect_with_message(
                redirect_path,
                error='Dispatcher отказал в отправке (см. метрики/логи)',
            )

        await AuditLogRepository(session).create(
            action=AuditAction.notification_rule_test_sent,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            entity_type='notification_rule',
            entity_id=str(rule.id),
            details={
                'code': rule.code,
                'target_tg_id': target_tg_id,
                'correlation_key': correlation_key,
            },
        )

    return _redirect_with_message(
        redirect_path,
        success=(
            f'Тест поставлен в outbox для tg_id={target_tg_id}. '
            'Сообщение придёт после ближайшего цикла outbox-воркера.'
        ),
    )


@router.post('/admin/notifications/{code}', dependencies=[Depends(require_support)])
async def admin_notification_update(
    request: Request,
    code: str,
    template_text: str = Form(...),
    template_keyboard_json_raw: str = Form(default=''),
    cooldown_seconds: int = Form(...),
    priority: int = Form(...),
    description: str = Form(default=''),
    segment_filter_json_raw: str = Form(default=''),
):
    sessionmaker = request.app.state.sessionmaker
    redirect_path = f'/admin/notifications/{code}'

    text_value = (template_text or '').strip()
    if not text_value:
        return _redirect_with_message(
            redirect_path, error='Текст шаблона не может быть пустым',
        )

    if cooldown_seconds < 0:
        return _redirect_with_message(
            redirect_path, error='Cooldown должен быть ≥ 0',
        )

    keyboard_value: list[Any] | None = None
    clear_keyboard = False
    raw_keyboard = (template_keyboard_json_raw or '').strip()
    if not raw_keyboard:
        clear_keyboard = True
    else:
        try:
            keyboard_value = json.loads(raw_keyboard)
        except json.JSONDecodeError as exc:
            return _redirect_with_message(
                redirect_path, error=f'Невалидный JSON клавиатуры: {exc.msg}',
            )
        kb_error = _validate_notification_keyboard_json(keyboard_value)
        if kb_error is not None:
            return _redirect_with_message(redirect_path, error=kb_error)

    segment_value: dict[str, Any] | None = None
    clear_segment = False
    raw_segment = (segment_filter_json_raw or '').strip()
    if not raw_segment:
        clear_segment = True
    else:
        try:
            parsed_segment = json.loads(raw_segment)
        except json.JSONDecodeError as exc:
            return _redirect_with_message(
                redirect_path, error=f'Невалидный JSON segment_filter: {exc.msg}',
            )
        if not isinstance(parsed_segment, dict):
            return _redirect_with_message(
                redirect_path, error='segment_filter должен быть JSON-объектом',
            )
        segment_value = parsed_segment

    description_value = (description or '').strip()
    if len(description_value) > 255:
        return _redirect_with_message(
            redirect_path, error='Описание не должно превышать 255 символов',
        )

    async with sessionmaker.begin() as session:
        repo = NotificationRuleRepository(session)
        audit_repo = AuditLogRepository(session)
        rule = await repo.get_by_code(code)
        if rule is None:
            return _redirect_with_message(
                '/admin/notifications/',
                error=f'Правило с кодом «{code}» не найдено',
            )

        try:
            await repo.update_rule(
                rule,
                template_text=text_value,
                template_keyboard_json=keyboard_value,
                clear_keyboard=clear_keyboard,
                cooldown_seconds=cooldown_seconds,
                priority=priority,
                description=description_value,
                segment_filter_json=segment_value,
                clear_segment_filter=clear_segment,
            )
        except ValueError as exc:
            return _redirect_with_message(redirect_path, error=str(exc))

        await audit_repo.create(
            action=AuditAction.notification_rule_updated,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            entity_type='notification_rule',
            entity_id=str(rule.id),
            details={
                'code': rule.code,
                'cooldown_seconds': rule.cooldown_seconds,
                'priority': rule.priority,
                'has_keyboard': rule.template_keyboard_json is not None,
                'has_segment_filter': rule.segment_filter_json is not None,
            },
        )

    return _redirect_with_message(
        redirect_path, success='Правило сохранено',
    )


def _topup_option_view(row, *, is_best: bool = False):
    extra = max(1, int(row.extra_traffic_gb))
    ppg = (Decimal(str(row.amount)) / Decimal(extra)).quantize(Decimal('0.01'))
    return SimpleNamespace(
        id=row.id,
        code=row.code,
        title=row.title,
        extra_traffic_gb=row.extra_traffic_gb,
        amount=row.amount,
        is_enabled=row.is_enabled,
        sort_order=row.sort_order,
        badge_label=row.badge_label,
        price_per_gb=ppg,
        is_best_price=is_best,
        updated_at=format_dt(getattr(row, 'updated_at', None)),
    )


@router.get('/admin/upsells/traffic/', response_class=HTMLResponse, dependencies=[Depends(require_any)])
async def admin_upsells_traffic(request: Request):
    templates = request.app.state.templates
    sessionmaker = request.app.state.sessionmaker
    async with sessionmaker() as session:
        rows = await TrafficTopupOptionRepository(session).list_all()
    enabled_rows = [r for r in rows if r.is_enabled]
    best_id: int | None = None
    if len(enabled_rows) >= 2:
        ratios = {r.id: (Decimal(str(r.amount)) / Decimal(max(1, int(r.extra_traffic_gb)))) for r in enabled_rows}
        min_v = min(ratios.values())
        winners = [oid for oid, v in ratios.items() if v == min_v]
        if len(winners) == 1:
            best_id = winners[0]
    options = [_topup_option_view(r, is_best=(r.id == best_id and r.is_enabled)) for r in rows]
    return templates.TemplateResponse(
        request,
        'admin_upsells_traffic.html',
        {
            'current_page': 'upsells_traffic',
            'options': options,
            'options_total': len(options),
            'enabled_count': len(enabled_rows),
            'success_message': request.query_params.get('success'),
            'error_message': request.query_params.get('error'),
        },
    )


def _parse_topup_form(
    *,
    code: str | None,
    title: str,
    extra_traffic_gb: int,
    amount: str,
    is_enabled: bool,
    sort_order: int,
    badge_label: str,
) -> tuple[dict, str | None]:
    if code is not None:
        code_value = (code or '').strip()
        if not code_value or len(code_value) > 32:
            return {}, 'Код обязателен и не длиннее 32 символов'
    else:
        code_value = None
    title_value = (title or '').strip()
    if not title_value or len(title_value) > 64:
        return {}, 'Название обязательно и не длиннее 64 символов'
    if extra_traffic_gb <= 0:
        return {}, 'Объём ГБ должен быть > 0'
    try:
        amount_value = Decimal((amount or '').strip().replace(',', '.'))
    except Exception:
        return {}, 'Сумма должна быть числом'
    if amount_value < 0:
        return {}, 'Сумма должна быть ≥ 0'
    badge_value = (badge_label or '').strip()
    if len(badge_value) > 64:
        return {}, 'Бейдж не длиннее 64 символов'
    payload = {
        'title': title_value,
        'extra_traffic_gb': int(extra_traffic_gb),
        'amount': amount_value,
        'is_enabled': bool(is_enabled),
        'sort_order': int(sort_order),
        'badge_label': badge_value,
    }
    if code_value is not None:
        payload['code'] = code_value
    return payload, None


@router.post('/admin/upsells/traffic/', dependencies=[Depends(require_finance)])
async def admin_upsells_traffic_create(
    request: Request,
    code: str = Form(...),
    title: str = Form(...),
    extra_traffic_gb: int = Form(...),
    amount: str = Form(...),
    sort_order: int = Form(default=100),
    badge_label: str = Form(default=''),
    is_enabled: str = Form(default=''),
):
    sessionmaker = request.app.state.sessionmaker
    redirect = '/admin/upsells/traffic/'
    payload, err = _parse_topup_form(
        code=code, title=title, extra_traffic_gb=extra_traffic_gb,
        amount=amount, is_enabled=bool(is_enabled), sort_order=sort_order,
        badge_label=badge_label,
    )
    if err:
        return _redirect_with_message(redirect, error=err)
    async with sessionmaker.begin() as session:
        repo = TrafficTopupOptionRepository(session)
        if await repo.get_by_code(payload['code']) is not None:
            return _redirect_with_message(redirect, error=f'Код «{payload["code"]}» уже используется')
        try:
            row = await repo.create(**payload)
        except ValueError as exc:
            return _redirect_with_message(redirect, error=str(exc))
        await AuditLogRepository(session).create(
            action=AuditAction.traffic_topup_option_created,
            actor_type=AuditActorType.admin, actor_tg_id=None,
            entity_type='traffic_topup_option', entity_id=str(row.id),
            details={'code': row.code, 'amount': str(row.amount), 'extra_traffic_gb': row.extra_traffic_gb},
        )
    return _redirect_with_message(redirect, success=f'Опция «{payload["code"]}» создана')


@router.post('/admin/upsells/traffic/{option_id}', dependencies=[Depends(require_finance)])
async def admin_upsells_traffic_update(
    request: Request,
    option_id: int,
    title: str = Form(...),
    extra_traffic_gb: int = Form(...),
    amount: str = Form(...),
    sort_order: int = Form(default=100),
    badge_label: str = Form(default=''),
    is_enabled: str = Form(default=''),
):
    sessionmaker = request.app.state.sessionmaker
    redirect = '/admin/upsells/traffic/'
    payload, err = _parse_topup_form(
        code=None, title=title, extra_traffic_gb=extra_traffic_gb,
        amount=amount, is_enabled=bool(is_enabled), sort_order=sort_order,
        badge_label=badge_label,
    )
    if err:
        return _redirect_with_message(redirect, error=err)
    async with sessionmaker.begin() as session:
        repo = TrafficTopupOptionRepository(session)
        row = await repo.get_by_id(option_id)
        if row is None:
            return _redirect_with_message(redirect, error='Опция не найдена')
        try:
            await repo.update(row, **payload)
        except ValueError as exc:
            return _redirect_with_message(redirect, error=str(exc))
        await AuditLogRepository(session).create(
            action=AuditAction.traffic_topup_option_updated,
            actor_type=AuditActorType.admin, actor_tg_id=None,
            entity_type='traffic_topup_option', entity_id=str(row.id),
            details={'code': row.code, 'amount': str(row.amount), 'extra_traffic_gb': row.extra_traffic_gb, 'is_enabled': row.is_enabled},
        )
    return _redirect_with_message(redirect, success=f'Опция «{row.code}» обновлена')


@router.post('/admin/upsells/traffic/{option_id}/toggle', dependencies=[Depends(require_finance)])
async def admin_upsells_traffic_toggle(request: Request, option_id: int):
    sessionmaker = request.app.state.sessionmaker
    redirect = '/admin/upsells/traffic/'
    async with sessionmaker.begin() as session:
        repo = TrafficTopupOptionRepository(session)
        row = await repo.get_by_id(option_id)
        if row is None:
            return _redirect_with_message(redirect, error='Опция не найдена')
        new_state = not row.is_enabled
        await repo.update(row, is_enabled=new_state)
        await AuditLogRepository(session).create(
            action=AuditAction.traffic_topup_option_toggled,
            actor_type=AuditActorType.admin, actor_tg_id=None,
            entity_type='traffic_topup_option', entity_id=str(row.id),
            details={'code': row.code, 'is_enabled': new_state},
        )
    label = 'включена' if new_state else 'выключена'
    return _redirect_with_message(redirect, success=f'Опция «{row.code}» {label}')


@router.post('/admin/upsells/traffic/{option_id}/delete', dependencies=[Depends(require_finance)])
async def admin_upsells_traffic_delete(request: Request, option_id: int):
    sessionmaker = request.app.state.sessionmaker
    redirect = '/admin/upsells/traffic/'
    async with sessionmaker.begin() as session:
        repo = TrafficTopupOptionRepository(session)
        row = await repo.get_by_id(option_id)
        if row is None:
            return _redirect_with_message(redirect, error='Опция не найдена')
        code = row.code
        await repo.delete(row)
        await AuditLogRepository(session).create(
            action=AuditAction.traffic_topup_option_deleted,
            actor_type=AuditActorType.admin, actor_tg_id=None,
            entity_type='traffic_topup_option', entity_id=str(option_id),
            details={'code': code},
        )
    return _redirect_with_message(redirect, success=f'Опция «{code}» удалена')


def _device_topup_preview_examples(
    *,
    monthly_extra_device_price: Decimal,
    fixed_price: Decimal,
    sample_days: tuple[int, ...] = (30, 15, 5, 1),
    days_in_cycle: int = 30,
) -> list[dict[str, Any]]:
    """Live-preview для admin: цена для нескольких days_left при текущей monthly-ставке.

    Используем ту же формулу, что и `PricingService.calculate_reset_price`
    (round CEILING до рубля), чтобы превью совпадало с реальной ценой
    в боте.
    """
    examples: list[dict[str, Any]] = []
    for days_left in sample_days:
        prorated = PricingService.calculate_reset_price(
            monthly_extra_device_price,
            days_left_in_month=days_left,
            days_in_month=days_in_cycle,
        )
        examples.append({
            'days_left': days_left,
            'days_in_cycle': days_in_cycle,
            'prorated_price': prorated,
            'fixed_price': fixed_price,
        })
    return examples


@router.get('/admin/upsells/devices/', response_class=HTMLResponse, dependencies=[Depends(require_any)])
async def admin_upsells_devices(request: Request):
    templates = request.app.state.templates
    sessionmaker = request.app.state.sessionmaker
    async with sessionmaker() as session:
        app_settings = await AppSettingsRepository(session).ensure()
        rules = await PricingService.get_rules(session)
    monthly_price = Decimal(str(rules.device_step_price))
    fixed_price = Decimal(str(app_settings.mid_cycle_device_fixed_price))
    examples = _device_topup_preview_examples(
        monthly_extra_device_price=monthly_price,
        fixed_price=fixed_price,
    )
    return templates.TemplateResponse(
        request,
        'admin_upsells_devices.html',
        {
            'current_page': 'upsells_devices',
            'enabled': app_settings.mid_cycle_device_topup_enabled,
            'price_mode': app_settings.mid_cycle_device_price_mode,
            'fixed_price': fixed_price,
            'monthly_extra_device_price': monthly_price,
            'max_custom_devices': PricingService.MAX_CUSTOM_DEVICES,
            'preview_examples': examples,
            'success_message': request.query_params.get('success'),
            'error_message': request.query_params.get('error'),
        },
    )


@router.post('/admin/upsells/devices/', dependencies=[Depends(require_finance)])
async def admin_upsells_devices_update(
    request: Request,
    price_mode: str = Form(...),
    fixed_price: str = Form(...),
    enabled: str = Form(default=''),
):
    sessionmaker = request.app.state.sessionmaker
    redirect = '/admin/upsells/devices/'
    try:
        normalized_fixed = Decimal((fixed_price or '').strip().replace(',', '.'))
    except Exception:
        return _redirect_with_message(redirect, error='Цена должна быть числом')
    async with sessionmaker.begin() as session:
        repo = AppSettingsRepository(session)
        row = await repo.ensure()
        try:
            await repo.update_mid_cycle_device_settings(
                row,
                enabled=bool(enabled),
                price_mode=price_mode,
                fixed_price=normalized_fixed,
            )
        except ValueError as exc:
            return _redirect_with_message(redirect, error=str(exc))
        await AuditLogRepository(session).create(
            action=AuditAction.mid_cycle_device_settings_updated,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            entity_type='app_settings',
            entity_id='1',
            details={
                'enabled': row.mid_cycle_device_topup_enabled,
                'price_mode': row.mid_cycle_device_price_mode,
                'fixed_price': str(row.mid_cycle_device_fixed_price),
            },
        )
    return _redirect_with_message(redirect, success='Настройки апсейла устройств сохранены')


# --- /admin/web-admins/ — RBAC management (FEA-C39 #3) ----------------------

# Матрица прав: какие роли что могут делать. Используется и для UI
# подсветки, и потенциально для будущих read-only-проверок. Источник
# истины для значений — это require_role/Depends в самих роутах; эта
# таблица — справочное представление.
_ROLE_CAPABILITY_MATRIX: tuple[tuple[str, dict[str, bool]], ...] = (
    ('Дашборд / Поиск', {'superadmin': True, 'finance': True, 'support': True, 'readonly': True}),
    ('Пользователи (read)', {'superadmin': True, 'finance': True, 'support': True, 'readonly': True}),
    ('Баланс пользователя (write)', {'superadmin': True, 'finance': True, 'support': False, 'readonly': False}),
    ('Тарифы / Промокоды / Инвойсы (write)', {'superadmin': True, 'finance': True, 'support': False, 'readonly': False}),
    ('Апсейлы трафика/устройств (write)', {'superadmin': True, 'finance': True, 'support': False, 'readonly': False}),
    ('Тикеты (close) / Рассылки / Push-правила', {'superadmin': True, 'finance': False, 'support': True, 'readonly': False}),
    ('Trial / Antispam / Rules / Links', {'superadmin': True, 'finance': False, 'support': False, 'readonly': False}),
    ('Marzban / Nodes / Routing-profiles (write)', {'superadmin': True, 'finance': False, 'support': False, 'readonly': False}),
    ('Веб-админы (RBAC management)', {'superadmin': True, 'finance': False, 'support': False, 'readonly': False}),
)


def _web_admin_view(row, *, current_username: str) -> dict[str, Any]:
    return {
        'id': row.id,
        'username': row.username,
        'role': row.role.value,
        'is_active': row.is_active,
        'last_login_at': row.last_login_at,
        'created_at': row.created_at,
        'updated_at': row.updated_at,
        'notes': row.notes or '',
        'is_self': row.username.lower() == (current_username or '').lower(),
    }


def _parse_web_admin_role(value: str | None) -> WebAdminRole:
    normalized = (value or '').strip().lower()
    try:
        return WebAdminRole(normalized)
    except ValueError as exc:
        raise ValueError(f'Неизвестная роль: {value!r}') from exc


@router.get('/admin/web-admins/', response_class=HTMLResponse, dependencies=[Depends(require_superadmin)])
async def admin_web_admins(
    request: Request,
    principal: WebAdminPrincipal = Depends(require_superadmin),
):
    templates = request.app.state.templates
    sessionmaker = request.app.state.sessionmaker
    async with sessionmaker() as session:
        rows = await WebAdminUserRepository(session).list_all()
        active_supers = await WebAdminUserRepository(session).count_active_by_role(WebAdminRole.superadmin)
    admins = [_web_admin_view(r, current_username=principal.username) for r in rows]
    return templates.TemplateResponse(
        request,
        'admin_web_admins.html',
        {
            'current_page': 'web_admins',
            'admins': admins,
            'admins_total': len(admins),
            'active_supers': active_supers,
            'roles': [r.value for r in WebAdminRole],
            'min_password_length': MIN_PLAINTEXT_PASSWORD_LENGTH,
            'capability_matrix': _ROLE_CAPABILITY_MATRIX,
            'success_message': request.query_params.get('success'),
            'error_message': request.query_params.get('error'),
        },
    )


@router.post('/admin/web-admins/', dependencies=[Depends(require_superadmin)])
async def admin_web_admins_create(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form(...),
    notes: str = Form(default=''),
    is_active: str = Form(default=''),
    principal: WebAdminPrincipal = Depends(require_superadmin),
):
    sessionmaker = request.app.state.sessionmaker
    redirect = '/admin/web-admins/'

    normalized_username = (username or '').strip()
    if not normalized_username:
        return _redirect_with_message(redirect, error='Username обязателен')
    if len(password or '') < MIN_PLAINTEXT_PASSWORD_LENGTH:
        return _redirect_with_message(
            redirect,
            error=f'Пароль должен быть длиной не менее {MIN_PLAINTEXT_PASSWORD_LENGTH} символов',
        )
    try:
        role_value = _parse_web_admin_role(role)
    except ValueError as exc:
        return _redirect_with_message(redirect, error=str(exc))

    async with sessionmaker.begin() as session:
        repo = WebAdminUserRepository(session)
        if await repo.get_by_username(normalized_username) is not None:
            return _redirect_with_message(redirect, error=f'Пользователь «{normalized_username}» уже существует')
        try:
            row = await repo.create(
                username=normalized_username,
                password_hash=hash_password(password),
                role=role_value,
                is_active=bool(is_active),
                notes=notes,
            )
        except ValueError as exc:
            return _redirect_with_message(redirect, error=str(exc))
        await AuditLogRepository(session).create(
            action=AuditAction.web_admin_action,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            actor_username=principal.username,
            entity_type='web_admin_user',
            entity_id=str(row.id),
            details={
                'op': 'create',
                'target_username': row.username,
                'role': row.role.value,
                'is_active': row.is_active,
            },
        )
    return _redirect_with_message(redirect, success=f'Веб-админ «{normalized_username}» создан')


@router.post('/admin/web-admins/{admin_id}', dependencies=[Depends(require_superadmin)])
async def admin_web_admins_update(
    request: Request,
    admin_id: int,
    role: str = Form(...),
    notes: str = Form(default=''),
    is_active: str = Form(default=''),
    principal: WebAdminPrincipal = Depends(require_superadmin),
):
    sessionmaker = request.app.state.sessionmaker
    redirect = '/admin/web-admins/'
    try:
        new_role = _parse_web_admin_role(role)
    except ValueError as exc:
        return _redirect_with_message(redirect, error=str(exc))

    async with sessionmaker.begin() as session:
        repo = WebAdminUserRepository(session)
        row = await repo.get_by_id(admin_id)
        if row is None:
            return _redirect_with_message(redirect, error='Веб-админ не найден')

        new_active = bool(is_active)
        is_self = row.username.lower() == principal.username.lower()
        if is_self and not new_active:
            return _redirect_with_message(redirect, error='Нельзя деактивировать самого себя')
        if is_self and new_role != WebAdminRole.superadmin:
            return _redirect_with_message(redirect, error='Нельзя понизить роль самому себе')

        # Защита: должен оставаться хотя бы один активный superadmin.
        if (
            row.role == WebAdminRole.superadmin
            and (new_role != WebAdminRole.superadmin or not new_active)
        ):
            active_supers = await repo.count_active_by_role(WebAdminRole.superadmin)
            currently_active_super = row.is_active
            remaining = active_supers - (1 if currently_active_super else 0)
            if remaining < 1:
                return _redirect_with_message(
                    redirect,
                    error='Нельзя оставить систему без активного superadmin',
                )

        old_role = row.role
        old_active = row.is_active
        await repo.update_role(row, new_role)
        await repo.set_active(row, active=new_active)
        await repo.update_notes(row, notes)
        await AuditLogRepository(session).create(
            action=AuditAction.web_admin_action,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            actor_username=principal.username,
            entity_type='web_admin_user',
            entity_id=str(row.id),
            details={
                'op': 'update',
                'target_username': row.username,
                'old_role': old_role.value,
                'new_role': row.role.value,
                'old_active': old_active,
                'new_active': row.is_active,
            },
        )
    return _redirect_with_message(redirect, success=f'Веб-админ «{row.username}» обновлён')


@router.post('/admin/web-admins/{admin_id}/password', dependencies=[Depends(require_superadmin)])
async def admin_web_admins_change_password(
    request: Request,
    admin_id: int,
    password: str = Form(...),
    principal: WebAdminPrincipal = Depends(require_superadmin),
):
    sessionmaker = request.app.state.sessionmaker
    redirect = '/admin/web-admins/'
    if len(password or '') < MIN_PLAINTEXT_PASSWORD_LENGTH:
        return _redirect_with_message(
            redirect,
            error=f'Пароль должен быть длиной не менее {MIN_PLAINTEXT_PASSWORD_LENGTH} символов',
        )

    async with sessionmaker.begin() as session:
        repo = WebAdminUserRepository(session)
        row = await repo.get_by_id(admin_id)
        if row is None:
            return _redirect_with_message(redirect, error='Веб-админ не найден')
        await repo.update_password(row, hash_password(password))
        await AuditLogRepository(session).create(
            action=AuditAction.web_admin_action,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            actor_username=principal.username,
            entity_type='web_admin_user',
            entity_id=str(row.id),
            details={
                'op': 'password_changed',
                'target_username': row.username,
            },
        )
    return _redirect_with_message(redirect, success=f'Пароль для «{row.username}» обновлён')


@router.post('/admin/web-admins/{admin_id}/delete', dependencies=[Depends(require_superadmin)])
async def admin_web_admins_delete(
    request: Request,
    admin_id: int,
    principal: WebAdminPrincipal = Depends(require_superadmin),
):
    sessionmaker = request.app.state.sessionmaker
    redirect = '/admin/web-admins/'
    async with sessionmaker.begin() as session:
        repo = WebAdminUserRepository(session)
        row = await repo.get_by_id(admin_id)
        if row is None:
            return _redirect_with_message(redirect, error='Веб-админ не найден')
        if row.username.lower() == principal.username.lower():
            return _redirect_with_message(redirect, error='Нельзя удалить самого себя')

        if row.role == WebAdminRole.superadmin and row.is_active:
            active_supers = await repo.count_active_by_role(WebAdminRole.superadmin)
            if active_supers <= 1:
                return _redirect_with_message(
                    redirect,
                    error='Нельзя удалить последнего активного superadmin',
                )

        username = row.username
        role_value = row.role.value
        await repo.delete(row)
        await AuditLogRepository(session).create(
            action=AuditAction.web_admin_action,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            actor_username=principal.username,
            entity_type='web_admin_user',
            entity_id=str(admin_id),
            details={
                'op': 'delete',
                'target_username': username,
                'role': role_value,
            },
        )
    return _redirect_with_message(redirect, success=f'Веб-админ «{username}» удалён')


@router.get('/admin/whoami', response_class=HTMLResponse, dependencies=[Depends(require_any)])
async def admin_whoami(
    request: Request,
    principal: WebAdminPrincipal = Depends(require_any),
):
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        'admin_whoami.html',
        {
            'current_page': 'whoami',
            'principal_username': principal.username,
            'principal_role': principal.role.value,
            'is_legacy': principal.is_legacy,
            'capability_matrix': _ROLE_CAPABILITY_MATRIX,
        },
    )


def _parse_assignee_filter(
    raw: str | None,
    *,
    current_admin_db_id: int | None,
) -> tuple[int | None, str]:
    """Возвращает (assignee_admin_id_or_None_or_unassigned_sentinel, normalized_value).

    Поддерживает значения: '' (без фильтра), 'me' (current_admin), 'unassigned'
    (тикеты без assignee), '<int>' (конкретный admin_id). Если выбрано 'me'
    но залогинен legacy-юзер без db_id — фильтр игнорируется (нет связки).
    """
    raw_normalized = (raw or '').strip().lower()
    if not raw_normalized:
        return None, ''
    if raw_normalized == 'me':
        return (current_admin_db_id, 'me') if current_admin_db_id is not None else (None, '')
    if raw_normalized == 'unassigned':
        return SupportTicketRepository.UNASSIGNED_FILTER, 'unassigned'
    try:
        return int(raw_normalized), raw_normalized
    except ValueError:
        return None, ''


@router.get('/admin/tickets/', response_class=HTMLResponse, dependencies=[Depends(require_any)])
async def admin_tickets(
    request: Request,
    q: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    unanswered_first: bool = Query(default=False),
    assignee: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    principal: WebAdminPrincipal = Depends(require_any),
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

    assignee_filter, assignee_normalized = _parse_assignee_filter(
        assignee, current_admin_db_id=principal.db_id,
    )
    tag_filter = (tag or '').strip().lower() or None

    async with sessionmaker() as session:
        repo = SupportTicketRepository(session)
        admin_repo = WebAdminUserRepository(session)
        total = await repo.count_for_admin(
            query=q,
            status=status_enum,
            assignee_admin_id=assignee_filter,
            tag=tag_filter,
        )
        rows = await repo.list_for_admin_with_meta(
            query=q,
            status=status_enum,
            limit=ADMIN_PAGE_SIZE,
            offset=offset,
            unanswered_first=unanswered_first,
            assignee_admin_id=assignee_filter,
            tag=tag_filter,
        )
        admins_for_filter = await admin_repo.list_all()

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
            'assignee_filter': assignee_normalized,
            'tag_filter': tag_filter or '',
            'admins_for_filter': admins_for_filter,
            'current_admin_db_id': principal.db_id,
        },
    )


async def _load_ticket_detail_context(request: Request, ticket_id: int) -> dict[str, Any]:
    """Загрузить контекст для шаблона `admin_ticket_detail.html`.

    Используется и обычным GET, и POST'ом генерации AI-черновика —
    второй после генерации рендерит ту же страницу с дополнительными
    `ai_draft`/`ai_used_canned_codes`/`ai_meta`/`ai_error` в context.
    """
    sessionmaker = request.app.state.sessionmaker

    async with sessionmaker() as session:
        ticket_repo = SupportTicketRepository(session)
        msg_repo = SupportMessageRepository(session)
        user_repo = UserRepository(session)
        admin_repo = WebAdminUserRepository(session)
        canned_repo = CannedResponseRepository(session)
        llm_repo = LLMConfigRepository(session)

        ticket = await ticket_repo.get_by_id(ticket_id)
        if ticket is None:
            raise HTTPException(status_code=404, detail='Ticket not found')

        messages = await msg_repo.list_by_ticket(ticket_id)
        user = await user_repo.get_by_id(ticket.user_id)
        last_message_at = await ticket_repo.get_last_message_timestamp(ticket_id)
        has_admin_reply = await ticket_repo.has_admin_reply(ticket_id)
        admins_list = await admin_repo.list_all()
        canned_responses = await canned_repo.list_active()
        active_llm_config = await llm_repo.get_active()
        assignee = None
        if ticket.assignee_admin_id is not None:
            assignee = await admin_repo.get_by_id(ticket.assignee_admin_id)

    return {
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
        'admins_list': admins_list,
        'canned_responses': canned_responses,
        'ticket_assignee': assignee,
        'ticket_tags': list(ticket.tags or []),
        'active_llm_config': active_llm_config,
        'ai_draft': None,
        'ai_used_canned_codes': [],
        'ai_meta': None,
        'ai_error': None,
        'success_message': request.query_params.get('success'),
        'error_message': request.query_params.get('error'),
    }


@router.get('/admin/tickets/{ticket_id}', response_class=HTMLResponse, dependencies=[Depends(require_any)])
async def admin_ticket_detail(request: Request, ticket_id: int):
    templates = request.app.state.templates
    context = await _load_ticket_detail_context(request, ticket_id)
    return templates.TemplateResponse(request, 'admin_ticket_detail.html', context)


@router.post('/admin/tickets/{ticket_id}/assignee', dependencies=[Depends(require_support)])
async def admin_ticket_set_assignee(
    request: Request,
    ticket_id: int,
    assignee_admin_id: str = Form(default=''),
    principal: WebAdminPrincipal = Depends(require_support),
):
    sessionmaker = request.app.state.sessionmaker
    redirect = f'/admin/tickets/{ticket_id}'
    raw = (assignee_admin_id or '').strip()
    new_assignee_id: int | None
    if not raw or raw == '0':
        new_assignee_id = None
    else:
        try:
            new_assignee_id = int(raw)
        except ValueError:
            return _redirect_with_message(redirect, error='Некорректный ID assignee')

    async with sessionmaker.begin() as session:
        ticket_repo = SupportTicketRepository(session)
        admin_repo = WebAdminUserRepository(session)
        ticket = await ticket_repo.get_by_id_for_update(ticket_id)
        if ticket is None:
            raise HTTPException(status_code=404, detail='Ticket not found')

        target_username: str | None = None
        if new_assignee_id is not None:
            target = await admin_repo.get_by_id(new_assignee_id)
            if target is None:
                return _redirect_with_message(redirect, error='Веб-админ не найден')
            if not target.is_active:
                return _redirect_with_message(redirect, error='Нельзя назначить деактивированного веб-админа')
            target_username = target.username

        old_assignee_id = ticket.assignee_admin_id
        await ticket_repo.set_assignee(ticket, new_assignee_id)
        await AuditLogRepository(session).create(
            action=AuditAction.ticket_assigned,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            actor_username=principal.username,
            entity_type='support_ticket',
            entity_id=str(ticket_id),
            details={
                'old_assignee_admin_id': old_assignee_id,
                'new_assignee_admin_id': new_assignee_id,
                'new_assignee_username': target_username,
            },
        )

    if new_assignee_id is None:
        return _redirect_with_message(redirect, success='Assignee снят')
    return _redirect_with_message(
        redirect, success=f'Assignee = {target_username or new_assignee_id}',
    )


@router.post('/admin/tickets/{ticket_id}/tags/add', dependencies=[Depends(require_support)])
async def admin_ticket_add_tag(
    request: Request,
    ticket_id: int,
    tag: str = Form(...),
    principal: WebAdminPrincipal = Depends(require_support),
):
    sessionmaker = request.app.state.sessionmaker
    redirect = f'/admin/tickets/{ticket_id}'

    async with sessionmaker.begin() as session:
        ticket_repo = SupportTicketRepository(session)
        ticket = await ticket_repo.get_by_id_for_update(ticket_id)
        if ticket is None:
            raise HTTPException(status_code=404, detail='Ticket not found')
        try:
            await ticket_repo.add_tag(ticket, tag)
        except ValueError as exc:
            return _redirect_with_message(redirect, error=str(exc))
        await AuditLogRepository(session).create(
            action=AuditAction.ticket_tagged,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            actor_username=principal.username,
            entity_type='support_ticket',
            entity_id=str(ticket_id),
            details={
                'op': 'add',
                'tag': tag.strip().lower(),
                'tags_after': list(ticket.tags or []),
            },
        )
    return _redirect_with_message(redirect, success=f'Тег «{tag.strip().lower()}» добавлен')


@router.post('/admin/tickets/{ticket_id}/tags/remove', dependencies=[Depends(require_support)])
async def admin_ticket_remove_tag(
    request: Request,
    ticket_id: int,
    tag: str = Form(...),
    principal: WebAdminPrincipal = Depends(require_support),
):
    sessionmaker = request.app.state.sessionmaker
    redirect = f'/admin/tickets/{ticket_id}'

    async with sessionmaker.begin() as session:
        ticket_repo = SupportTicketRepository(session)
        ticket = await ticket_repo.get_by_id_for_update(ticket_id)
        if ticket is None:
            raise HTTPException(status_code=404, detail='Ticket not found')
        await ticket_repo.remove_tag(ticket, tag)
        await AuditLogRepository(session).create(
            action=AuditAction.ticket_tagged,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            actor_username=principal.username,
            entity_type='support_ticket',
            entity_id=str(ticket_id),
            details={
                'op': 'remove',
                'tag': tag.strip().lower(),
                'tags_after': list(ticket.tags or []),
            },
        )
    return _redirect_with_message(redirect, success=f'Тег «{tag.strip().lower()}» удалён')


@router.post('/admin/tickets/bulk-close', dependencies=[Depends(require_support)])
async def admin_tickets_bulk_close(
    request: Request,
    ticket_ids: list[int] = Form(default=[]),
    close_reason: str | None = Form(default=None),
):
    """Массовое закрытие тикетов (FEA-ADMIN-CRUD-EXPAND).

    Принимает список `ticket_ids` (checkboxes из /admin/tickets/) и общий
    `close_reason` (опционально). Открытые тикеты закрываются по очереди
    в одной транзакции; уже закрытые — пропускаются (закрытие idempotent).
    Уведомления пользователям отправляются после коммита (best-effort через
    outbox). Аудит — отдельная `ticket_closed`-запись на каждый тикет с
    `bulk=True` в details.
    """
    sessionmaker = request.app.state.sessionmaker
    normalized_close_reason = _normalized_optional_form_text(close_reason)

    # dedup + skip non-positive
    unique_ids: list[int] = []
    seen: set[int] = set()
    for raw_id in ticket_ids or []:
        try:
            tid = int(raw_id)
        except (TypeError, ValueError):
            continue
        if tid < 1 or tid in seen:
            continue
        seen.add(tid)
        unique_ids.append(tid)

    if not unique_ids:
        return _redirect_with_message('/admin/tickets/', error='Не выбрано ни одного тикета')

    closed_user_ids: list[tuple[int, int]] = []  # (ticket_id, user_id)
    already_closed: list[int] = []
    not_found: list[int] = []

    async with sessionmaker.begin() as session:
        repo = SupportTicketRepository(session)
        audit_repo = AuditLogRepository(session)
        for ticket_id in unique_ids:
            ticket = await repo.get_by_id_for_update(ticket_id)
            if ticket is None:
                not_found.append(ticket_id)
                continue

            previous_status = getattr(ticket.status, 'value', ticket.status)
            closed_now = await repo.close(
                ticket,
                reason=normalized_close_reason,
                closed_by_admin_tg_id=None,
                actor_type='admin',
                actor_tg_id=None,
            )
            if not closed_now:
                already_closed.append(ticket_id)
                continue

            closed_user_ids.append((ticket_id, ticket.user_id))
            await audit_repo.create(
                action=AuditAction.ticket_closed,
                actor_type=AuditActorType.admin,
                actor_tg_id=None,
                entity_type='support_ticket',
                entity_id=str(ticket.id),
                details={
                    'reason': normalized_close_reason,
                    'closed_via': 'web_admin_bulk',
                    'previous_status': previous_status,
                    'new_status': getattr(ticket.status, 'value', ticket.status),
                    'bulk': True,
                    'bulk_batch_size': len(unique_ids),
                },
            )

    for ticket_id, user_id in closed_user_ids:
        with suppress(Exception):
            await _notify_ticket_closed_from_web_admin(request, ticket_id=ticket_id, user_id=user_id)

    closed_count = len(closed_user_ids)
    parts: list[str] = []
    if closed_count:
        parts.append(f'закрыто {closed_count}')
    if already_closed:
        parts.append(f'уже было закрыто {len(already_closed)}')
    if not_found:
        parts.append(f'не найдено {len(not_found)}')
    summary = 'Массовое закрытие: ' + (', '.join(parts) if parts else 'ничего не сделано')

    if closed_count == 0 and not already_closed and not_found:
        return _redirect_with_message('/admin/tickets/', error=summary)
    return _redirect_with_message('/admin/tickets/', success=summary)


@router.post('/admin/tickets/{ticket_id}/close', dependencies=[Depends(require_support)])
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


@router.post('/admin/tickets/{ticket_id}/ai-generate', response_class=HTMLResponse, dependencies=[Depends(require_support)])
async def admin_ticket_ai_generate(
    request: Request,
    ticket_id: int,
    principal: WebAdminPrincipal = Depends(require_support),
):
    """Сгенерировать черновик ответа на тикет через активный LLMConfig.

    Возвращает ту же страницу `admin_ticket_detail.html`, но с
    `ai_draft` (текст) и `ai_meta` (provider/model/tokens/latency) в
    контексте — саппорт правит черновик в textarea и копирует в
    Telegram. Сетевые/HTTP-ошибки попадают в `ai_error` без падения
    страницы.

    Метрика: `vpn_bot_support_ai_calls_total{provider, status}` —
    incrementится в любом из исходов (ok/error/no_active_config).
    Audit: `support_ai_generated` (status=ok|error, used_canned_codes,
    tokens, latency, error message при failure).
    """
    sessionmaker = request.app.state.sessionmaker
    templates = request.app.state.templates
    context = await _load_ticket_detail_context(request, ticket_id)

    active_config: 'LLMConfig | None' = context.get('active_llm_config')
    if active_config is None:
        SUPPORT_AI_CALLS.labels(provider='none', status='no_active_config').inc()
        context['ai_error'] = (
            'Нет активного LLM-конфига. Откройте /admin/support-ai/ и '
            'создайте/активируйте конфиг.'
        )
        return templates.TemplateResponse(request, 'admin_ticket_detail.html', context)

    provider_value = active_config.provider.value
    config_id = active_config.id
    ticket = context['ticket']
    messages = context['messages']
    canned_responses = context['canned_responses']

    try:
        result = await generate_support_draft(
            active_config,
            ticket,
            messages,
            canned_responses,
            timeout_seconds=30.0,
        )
    except LLMSecretsKeyError as exc:
        SUPPORT_AI_CALLS.labels(provider=provider_value, status='secrets_error').inc()
        await _audit_support_ai(
            sessionmaker,
            principal=principal,
            config_id=config_id,
            ticket_id=ticket_id,
            status='error',
            details={'error_kind': 'secrets', 'error': str(exc)[:500]},
        )
        context['ai_error'] = f'Не удалось расшифровать api_key: {exc}'
        return templates.TemplateResponse(request, 'admin_ticket_detail.html', context)
    except LLMProviderError as exc:
        SUPPORT_AI_CALLS.labels(provider=provider_value, status='provider_error').inc()
        message = exc.provider_message or str(exc)
        if exc.status_code:
            message = f'HTTP {exc.status_code}: {message}'
        await _audit_support_ai(
            sessionmaker,
            principal=principal,
            config_id=config_id,
            ticket_id=ticket_id,
            status='error',
            details={
                'error_kind': 'provider',
                'status_code': exc.status_code,
                'error': message[:500],
            },
        )
        context['ai_error'] = f'Ошибка провайдера: {message[:300]}'
        return templates.TemplateResponse(request, 'admin_ticket_detail.html', context)
    except Exception as exc:  # noqa: BLE001
        SUPPORT_AI_CALLS.labels(provider=provider_value, status='unexpected_error').inc()
        logger.exception('Support-AI generate failed for ticket=%s config=%s', ticket_id, config_id)
        await _audit_support_ai(
            sessionmaker,
            principal=principal,
            config_id=config_id,
            ticket_id=ticket_id,
            status='error',
            details={'error_kind': 'unexpected', 'error': repr(exc)[:500]},
        )
        context['ai_error'] = f'Непредвиденная ошибка: {exc}'
        return templates.TemplateResponse(request, 'admin_ticket_detail.html', context)

    SUPPORT_AI_CALLS.labels(provider=provider_value, status='ok').inc()

    # Запись usage + few-shot usage_count + audit — в одной транзакции,
    # чтобы при ошибке audit'а не остался "висячий" usage.
    async with sessionmaker.begin() as session:
        llm_repo = LLMConfigRepository(session)
        canned_repo = CannedResponseRepository(session)
        config_row = await llm_repo.get_by_id(config_id)
        if config_row is not None:
            await llm_repo.record_usage(
                config_row,
                tokens_in=result.response.tokens_in,
                tokens_out=result.response.tokens_out,
            )
        for code in result.used_canned_codes:
            cr = await canned_repo.get_by_code(code)
            if cr is not None:
                await canned_repo.increment_usage(cr)
        await AuditLogRepository(session).create(
            action=AuditAction.support_ai_generated,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            actor_username=principal.username,
            entity_type='support_ticket',
            entity_id=str(ticket_id),
            details={
                'status': 'ok',
                'config_id': config_id,
                'provider': provider_value,
                'model': result.response.model,
                'tokens_in': result.response.tokens_in,
                'tokens_out': result.response.tokens_out,
                'latency_ms': result.response.latency_ms,
                'used_canned_codes': result.used_canned_codes,
            },
        )

    context['ai_draft'] = result.draft
    context['ai_used_canned_codes'] = result.used_canned_codes
    context['ai_meta'] = {
        'provider_label': _LLM_PROVIDER_LABELS.get(provider_value, provider_value),
        'model': result.response.model,
        'tokens_in': result.response.tokens_in,
        'tokens_out': result.response.tokens_out,
        'latency_ms': result.response.latency_ms,
    }
    return templates.TemplateResponse(request, 'admin_ticket_detail.html', context)


async def _audit_support_ai(
    sessionmaker,
    *,
    principal: WebAdminPrincipal,
    config_id: int,
    ticket_id: int,
    status: str,
    details: dict[str, Any],
) -> None:
    """Записать audit `support_ai_generated` со status=error в отдельной
    транзакции. Используется failure-веткой генерации (там основной
    sessionmaker.begin() блок ещё не открывался)."""
    payload = dict(details)
    payload['status'] = status
    payload['config_id'] = config_id
    try:
        async with sessionmaker.begin() as session:
            await AuditLogRepository(session).create(
                action=AuditAction.support_ai_generated,
                actor_type=AuditActorType.admin,
                actor_tg_id=None,
                actor_username=principal.username,
                entity_type='support_ticket',
                entity_id=str(ticket_id),
                details=payload,
            )
    except Exception:
        logger.exception('Failed to audit support_ai_generated for ticket=%s', ticket_id)


def _parse_canned_tags(raw: str | None) -> list[str]:
    """Принимает строку с тегами через запятую/пробел, нормализует lowercase + dedup."""
    if not raw:
        return []
    parts = re.split(r'[\s,;]+', raw.strip())
    seen: set[str] = set()
    result: list[str] = []
    for part in parts:
        normalized = part.strip().lower()
        if not normalized or normalized in seen:
            continue
        if len(normalized) > 32:
            raise ValueError(f'Тег «{normalized}» длиннее 32 символов')
        seen.add(normalized)
        result.append(normalized)
    return result


def _canned_response_view(row) -> dict[str, Any]:
    return {
        'id': row.id,
        'code': row.code,
        'title': row.title,
        'content': row.content,
        'tags': list(row.tags or []),
        'tags_text': ', '.join(row.tags or []),
        'is_active': row.is_active,
        'sort_order': row.sort_order,
        'usage_count': row.usage_count,
        'created_at': row.created_at,
        'updated_at': row.updated_at,
    }


@router.get('/admin/canned-responses/', response_class=HTMLResponse, dependencies=[Depends(require_support)])
async def admin_canned_responses(request: Request):
    templates = request.app.state.templates
    sessionmaker = request.app.state.sessionmaker
    async with sessionmaker() as session:
        rows = await CannedResponseRepository(session).list_all()
    items = [_canned_response_view(r) for r in rows]
    return templates.TemplateResponse(
        request,
        'admin_canned_responses.html',
        {
            'current_page': 'canned_responses',
            'items': items,
            'total': len(items),
            'active_total': sum(1 for it in items if it['is_active']),
            'success_message': request.query_params.get('success'),
            'error_message': request.query_params.get('error'),
        },
    )


@router.post('/admin/canned-responses/', dependencies=[Depends(require_support)])
async def admin_canned_responses_create(
    request: Request,
    code: str = Form(...),
    title: str = Form(...),
    content: str = Form(...),
    tags: str = Form(default=''),
    sort_order: int = Form(default=100),
    is_active: str = Form(default=''),
    principal: WebAdminPrincipal = Depends(require_support),
):
    sessionmaker = request.app.state.sessionmaker
    redirect = '/admin/canned-responses/'
    try:
        normalized_tags = _parse_canned_tags(tags)
    except ValueError as exc:
        return _redirect_with_message(redirect, error=str(exc))

    async with sessionmaker.begin() as session:
        repo = CannedResponseRepository(session)
        if await repo.get_by_code(code) is not None:
            return _redirect_with_message(redirect, error=f'Код «{code}» уже используется')
        try:
            row = await repo.create(
                code=code,
                title=title,
                content=content,
                tags=normalized_tags,
                is_active=bool(is_active),
                sort_order=sort_order,
                created_by_admin_id=principal.db_id,
            )
        except ValueError as exc:
            return _redirect_with_message(redirect, error=str(exc))
        await AuditLogRepository(session).create(
            action=AuditAction.canned_response_created,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            actor_username=principal.username,
            entity_type='canned_response',
            entity_id=str(row.id),
            details={
                'code': row.code,
                'title': row.title,
                'tags': list(row.tags or []),
                'is_active': row.is_active,
            },
        )
    return _redirect_with_message(redirect, success=f'Шаблон «{code}» создан')


@router.post('/admin/canned-responses/{response_id}', dependencies=[Depends(require_support)])
async def admin_canned_responses_update(
    request: Request,
    response_id: int,
    title: str = Form(...),
    content: str = Form(...),
    tags: str = Form(default=''),
    sort_order: int = Form(default=100),
    is_active: str = Form(default=''),
    principal: WebAdminPrincipal = Depends(require_support),
):
    sessionmaker = request.app.state.sessionmaker
    redirect = '/admin/canned-responses/'
    try:
        normalized_tags = _parse_canned_tags(tags)
    except ValueError as exc:
        return _redirect_with_message(redirect, error=str(exc))

    async with sessionmaker.begin() as session:
        repo = CannedResponseRepository(session)
        row = await repo.get_by_id(response_id)
        if row is None:
            return _redirect_with_message(redirect, error='Шаблон не найден')
        try:
            await repo.update(
                row,
                title=title,
                content=content,
                tags=normalized_tags,
                is_active=bool(is_active),
                sort_order=sort_order,
            )
        except ValueError as exc:
            return _redirect_with_message(redirect, error=str(exc))
        await AuditLogRepository(session).create(
            action=AuditAction.canned_response_updated,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            actor_username=principal.username,
            entity_type='canned_response',
            entity_id=str(row.id),
            details={
                'code': row.code,
                'tags': list(row.tags or []),
                'is_active': row.is_active,
                'sort_order': row.sort_order,
            },
        )
    return _redirect_with_message(redirect, success=f'Шаблон «{row.code}» обновлён')


@router.post('/admin/canned-responses/{response_id}/delete', dependencies=[Depends(require_support)])
async def admin_canned_responses_delete(
    request: Request,
    response_id: int,
    principal: WebAdminPrincipal = Depends(require_support),
):
    sessionmaker = request.app.state.sessionmaker
    redirect = '/admin/canned-responses/'
    async with sessionmaker.begin() as session:
        repo = CannedResponseRepository(session)
        row = await repo.get_by_id(response_id)
        if row is None:
            return _redirect_with_message(redirect, error='Шаблон не найден')
        code = row.code
        await repo.delete(row)
        await AuditLogRepository(session).create(
            action=AuditAction.canned_response_deleted,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            actor_username=principal.username,
            entity_type='canned_response',
            entity_id=str(response_id),
            details={'code': code},
        )
    return _redirect_with_message(redirect, success=f'Шаблон «{code}» удалён')


@router.get('/admin/referrals/', response_class=HTMLResponse, dependencies=[Depends(require_any)])
async def admin_referrals(request: Request):
    templates = request.app.state.templates
    sessionmaker = request.app.state.sessionmaker
    async with sessionmaker() as session:
        row = await AppSettingsRepository(session).ensure()
    return templates.TemplateResponse(
        request,
        'admin_referrals.html',
        {
            'current_page': 'referrals',
            'inviter_bonus': row.referral_inviter_bonus,
            'invited_bonus': row.referral_invited_bonus,
            'success_message': request.query_params.get('success'),
            'error_message': request.query_params.get('error'),
        },
    )


@router.post('/admin/referrals/', dependencies=[Depends(require_finance)])
async def admin_referrals_update(
    request: Request,
    inviter_bonus: str = Form(...),
    invited_bonus: str = Form(...),
    principal: WebAdminPrincipal = Depends(require_finance),
):
    sessionmaker = request.app.state.sessionmaker
    redirect = '/admin/referrals/'
    async with sessionmaker.begin() as session:
        repo = AppSettingsRepository(session)
        row = await repo.ensure()
        try:
            await repo.update_referral_settings(
                row,
                inviter_bonus=inviter_bonus.strip().replace(',', '.'),
                invited_bonus=invited_bonus.strip().replace(',', '.'),
            )
        except ValueError as exc:
            return _redirect_with_message(redirect, error=str(exc))
        await AuditLogRepository(session).create(
            action=AuditAction.referral_settings_updated,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            actor_username=principal.username,
            entity_type='app_settings',
            entity_id='1',
            details={
                'inviter_bonus': str(row.referral_inviter_bonus),
                'invited_bonus': str(row.referral_invited_bonus),
            },
        )
    return _redirect_with_message(redirect, success='Бонусы реферальной программы обновлены')


# --- Support-AI (LLM-провайдеры) (FEA-C32 #2) -------------------------------

_LLM_PROVIDER_LABELS: dict[str, str] = {
    LLMProviderKind.deepseek.value: 'DeepSeek',
    LLMProviderKind.openai_compat.value: 'OpenAI-совместимый',
}


def _llm_config_view(row) -> dict[str, Any]:
    """Безопасное представление LLMConfig для шаблона.

    api_key никогда не передаётся в шаблон в plain виде — только preview.
    Декрипт делается лениво и только при показе превью; ошибки декрипта
    превращаются в пометку «(ключ недоступен)», чтобы UI не падал.
    """
    try:
        plain_key = decrypt_api_key(row.api_key_encrypted)
    except LLMSecretsKeyError:
        plain_key = None
    api_key_preview = (
        mask_api_key_preview(plain_key) if plain_key else '⚠️ ключ недоступен'
    )
    return {
        'id': row.id,
        'title': row.title,
        'provider': row.provider.value if hasattr(row.provider, 'value') else str(row.provider),
        'provider_label': _LLM_PROVIDER_LABELS.get(
            row.provider.value if hasattr(row.provider, 'value') else str(row.provider),
            str(row.provider),
        ),
        'api_base_url': row.api_base_url,
        'model_name': row.model_name,
        'system_prompt': row.system_prompt,
        'temperature': row.temperature,
        'max_tokens': row.max_tokens,
        'api_key_preview': api_key_preview,
        'is_active': row.is_active,
        'usage_total_calls': row.usage_total_calls,
        'usage_total_input_tokens': row.usage_total_input_tokens,
        'usage_total_output_tokens': row.usage_total_output_tokens,
        'last_test_status': row.last_test_status,
        'last_test_error': row.last_test_error,
        'last_test_at': row.last_test_at,
        'created_at': row.created_at,
        'updated_at': row.updated_at,
    }


_LLM_DEFAULT_SYSTEM_PROMPT = (
    'Ты — оператор саппорта VPN-сервиса SwoiVPN. Отвечай по-русски, '
    'кратко (3–8 предложений), вежливо и по делу. Используй маркеры '
    'списка только когда это упрощает чтение инструкции. Никогда не '
    'выдумывай факты о подписке пользователя — если данных недостаточно, '
    'попроси уточнения. Не упоминай, что ты ИИ.'
)


def _normalize_llm_provider(value: str | None) -> LLMProviderKind:
    normalized = (value or '').strip().lower()
    try:
        return LLMProviderKind(normalized)
    except ValueError as exc:
        raise ValueError(f'Неизвестный провайдер: {value!r}') from exc


@router.get('/admin/support-ai/', response_class=HTMLResponse, dependencies=[Depends(require_superadmin)])
async def admin_support_ai(request: Request):
    templates = request.app.state.templates
    sessionmaker = request.app.state.sessionmaker
    async with sessionmaker() as session:
        rows = await LLMConfigRepository(session).list_all()
    items = [_llm_config_view(r) for r in rows]
    return templates.TemplateResponse(
        request,
        'admin_support_ai.html',
        {
            'current_page': 'support_ai',
            'items': items,
            'total': len(items),
            'active_total': sum(1 for it in items if it['is_active']),
            'providers': [
                (kind.value, _LLM_PROVIDER_LABELS[kind.value]) for kind in LLMProviderKind
            ],
            'default_deepseek_url': DEEPSEEK_DEFAULT_API_BASE_URL,
            'default_system_prompt': _LLM_DEFAULT_SYSTEM_PROMPT,
            'success_message': request.query_params.get('success'),
            'error_message': request.query_params.get('error'),
        },
    )


@router.post('/admin/support-ai/', dependencies=[Depends(require_superadmin)])
async def admin_support_ai_create(
    request: Request,
    title: str = Form(...),
    provider: str = Form(...),
    api_base_url: str = Form(...),
    model_name: str = Form(...),
    system_prompt: str = Form(...),
    api_key: str = Form(...),
    temperature: str = Form(default='0.30'),
    max_tokens: int = Form(default=1024),
    is_active: str = Form(default=''),
    principal: WebAdminPrincipal = Depends(require_superadmin),
):
    sessionmaker = request.app.state.sessionmaker
    redirect = '/admin/support-ai/'
    try:
        provider_kind = _normalize_llm_provider(provider)
    except ValueError as exc:
        return _redirect_with_message(redirect, error=str(exc))
    try:
        encrypted = encrypt_api_key(api_key.strip())
    except (ValueError, LLMSecretsKeyError) as exc:
        return _redirect_with_message(redirect, error=f'api_key: {exc}')

    async with sessionmaker.begin() as session:
        repo = LLMConfigRepository(session)
        try:
            row = await repo.create(
                title=title,
                provider=provider_kind,
                api_base_url=api_base_url,
                model_name=model_name,
                system_prompt=system_prompt,
                api_key_encrypted=encrypted,
                temperature=temperature.replace(',', '.'),
                max_tokens=max_tokens,
                is_active=bool(is_active),
                created_by_admin_id=principal.db_id,
            )
        except ValueError as exc:
            return _redirect_with_message(redirect, error=str(exc))
        await AuditLogRepository(session).create(
            action=AuditAction.llm_config_created,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            actor_username=principal.username,
            entity_type='llm_config',
            entity_id=str(row.id),
            details={
                'title': row.title,
                'provider': row.provider.value,
                'model_name': row.model_name,
                'is_active': row.is_active,
            },
        )
    return _redirect_with_message(redirect, success=f'LLM-конфиг «{row.title}» создан')


@router.post('/admin/support-ai/{config_id}', dependencies=[Depends(require_superadmin)])
async def admin_support_ai_update(
    request: Request,
    config_id: int,
    title: str = Form(...),
    provider: str = Form(...),
    api_base_url: str = Form(...),
    model_name: str = Form(...),
    system_prompt: str = Form(...),
    temperature: str = Form(default='0.30'),
    max_tokens: int = Form(default=1024),
    api_key: str = Form(default=''),
    principal: WebAdminPrincipal = Depends(require_superadmin),
):
    sessionmaker = request.app.state.sessionmaker
    redirect = '/admin/support-ai/'
    try:
        provider_kind = _normalize_llm_provider(provider)
    except ValueError as exc:
        return _redirect_with_message(redirect, error=str(exc))

    new_encrypted: str | None = None
    if api_key.strip():
        try:
            new_encrypted = encrypt_api_key(api_key.strip())
        except (ValueError, LLMSecretsKeyError) as exc:
            return _redirect_with_message(redirect, error=f'api_key: {exc}')

    async with sessionmaker.begin() as session:
        repo = LLMConfigRepository(session)
        row = await repo.get_by_id(config_id)
        if row is None:
            return _redirect_with_message(redirect, error='LLM-конфиг не найден')
        try:
            await repo.update(
                row,
                title=title,
                provider=provider_kind,
                api_base_url=api_base_url,
                model_name=model_name,
                system_prompt=system_prompt,
                temperature=temperature.replace(',', '.'),
                max_tokens=max_tokens,
                api_key_encrypted=new_encrypted,
            )
        except ValueError as exc:
            return _redirect_with_message(redirect, error=str(exc))
        await AuditLogRepository(session).create(
            action=AuditAction.llm_config_updated,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            actor_username=principal.username,
            entity_type='llm_config',
            entity_id=str(row.id),
            details={
                'title': row.title,
                'provider': row.provider.value,
                'model_name': row.model_name,
                'api_key_rotated': new_encrypted is not None,
            },
        )
    return _redirect_with_message(redirect, success=f'LLM-конфиг «{row.title}» обновлён')


@router.post('/admin/support-ai/{config_id}/activate', dependencies=[Depends(require_superadmin)])
async def admin_support_ai_activate(
    request: Request,
    config_id: int,
    principal: WebAdminPrincipal = Depends(require_superadmin),
):
    sessionmaker = request.app.state.sessionmaker
    redirect = '/admin/support-ai/'
    async with sessionmaker.begin() as session:
        repo = LLMConfigRepository(session)
        row = await repo.get_by_id(config_id)
        if row is None:
            return _redirect_with_message(redirect, error='LLM-конфиг не найден')
        if row.is_active:
            await repo.deactivate_all()
            await AuditLogRepository(session).create(
                action=AuditAction.llm_config_updated,
                actor_type=AuditActorType.admin,
                actor_tg_id=None,
                actor_username=principal.username,
                entity_type='llm_config',
                entity_id=str(row.id),
                details={'is_active': False, 'reason': 'manual_deactivate'},
            )
            return _redirect_with_message(redirect, success=f'LLM-конфиг «{row.title}» деактивирован')
        await repo.set_active(row)
        await AuditLogRepository(session).create(
            action=AuditAction.llm_config_updated,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            actor_username=principal.username,
            entity_type='llm_config',
            entity_id=str(row.id),
            details={'is_active': True, 'title': row.title, 'reason': 'manual_activate'},
        )
    return _redirect_with_message(redirect, success=f'Активный LLM — «{row.title}»')


@router.post('/admin/support-ai/{config_id}/test', dependencies=[Depends(require_superadmin)])
async def admin_support_ai_test(
    request: Request,
    config_id: int,
    principal: WebAdminPrincipal = Depends(require_superadmin),
):
    """Тест соединения с LLM. Отправляет короткий ping-prompt, обновляет
    `last_test_*` поля и пишет audit. Не списывает usage_total_* (это
    диагностика, не реальная генерация ответа).
    """
    sessionmaker = request.app.state.sessionmaker
    redirect = '/admin/support-ai/'

    async with sessionmaker() as session:
        row = await LLMConfigRepository(session).get_by_id(config_id)
        if row is None:
            return _redirect_with_message(redirect, error='LLM-конфиг не найден')
        try:
            provider = build_provider(row)
        except LLMSecretsKeyError as exc:
            await _record_test_failure(sessionmaker, config_id, str(exc), principal)
            return _redirect_with_message(redirect, error=f'Ключ: {exc}')
        title = row.title
        provider_value = row.provider.value
        model_name = row.model_name

    try:
        response = await provider.complete(
            messages=[
                {'role': 'system', 'content': 'You are a connectivity check. Respond with exactly: pong'},
                {'role': 'user', 'content': 'ping'},
            ],
            temperature=0.0,
            max_tokens=8,
            timeout_seconds=15.0,
        )
    except LLMProviderError as exc:
        error_msg = exc.provider_message or str(exc)
        if exc.status_code:
            error_msg = f'HTTP {exc.status_code}: {error_msg}'
        await _record_test_failure(sessionmaker, config_id, error_msg, principal)
        return _redirect_with_message(redirect, error=f'Тест не пройден: {error_msg[:200]}')
    except Exception as exc:  # noqa: BLE001
        logger.exception('LLM test unexpected error config_id=%s', config_id)
        await _record_test_failure(sessionmaker, config_id, repr(exc), principal)
        return _redirect_with_message(redirect, error=f'Тест не пройден: {exc}')

    async with sessionmaker.begin() as session:
        repo = LLMConfigRepository(session)
        row = await repo.get_by_id(config_id)
        if row is not None:
            await repo.record_test_result(row, status='ok', error=None)
        await AuditLogRepository(session).create(
            action=AuditAction.llm_config_test_run,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            actor_username=principal.username,
            entity_type='llm_config',
            entity_id=str(config_id),
            details={
                'status': 'ok',
                'provider': provider_value,
                'model': response.model,
                'tokens_in': response.tokens_in,
                'tokens_out': response.tokens_out,
                'latency_ms': response.latency_ms,
            },
        )
    return _redirect_with_message(
        redirect,
        success=(
            f'Тест «{title}» пройден: {response.model} · '
            f'tokens {response.tokens_in}+{response.tokens_out} · '
            f'{response.latency_ms} мс. Ответ: {response.text[:80]!r}'
        ),
    )


async def _record_test_failure(
    sessionmaker,
    config_id: int,
    error: str,
    principal: WebAdminPrincipal,
) -> None:
    """Записать неуспешный тест в last_test_* + audit. Свопает свою
    транзакцию, чтобы вызывающий код мог использовать sessionmaker дальше.
    """
    async with sessionmaker.begin() as session:
        repo = LLMConfigRepository(session)
        row = await repo.get_by_id(config_id)
        if row is not None:
            await repo.record_test_result(row, status='error', error=error)
        await AuditLogRepository(session).create(
            action=AuditAction.llm_config_test_run,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            actor_username=principal.username,
            entity_type='llm_config',
            entity_id=str(config_id),
            details={'status': 'error', 'error': error[:500]},
        )


@router.post('/admin/support-ai/{config_id}/delete', dependencies=[Depends(require_superadmin)])
async def admin_support_ai_delete(
    request: Request,
    config_id: int,
    principal: WebAdminPrincipal = Depends(require_superadmin),
):
    sessionmaker = request.app.state.sessionmaker
    redirect = '/admin/support-ai/'
    async with sessionmaker.begin() as session:
        repo = LLMConfigRepository(session)
        row = await repo.get_by_id(config_id)
        if row is None:
            return _redirect_with_message(redirect, error='LLM-конфиг не найден')
        title = row.title
        await repo.delete(row)
        await AuditLogRepository(session).create(
            action=AuditAction.llm_config_deleted,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            actor_username=principal.username,
            entity_type='llm_config',
            entity_id=str(config_id),
            details={'title': title},
        )
    return _redirect_with_message(redirect, success=f'LLM-конфиг «{title}» удалён')


# --- Subscriptions admin (FEA-ADMIN-SUB-CRM) -------------------------------

_SUBSCRIPTIONS_PAGE_SIZE = ADMIN_PAGE_SIZE
_SUBSCRIPTION_STATUS_LABELS: dict[str, str] = {
    SubscriptionRepository.STATUS_ALL: 'Все',
    SubscriptionRepository.STATUS_ACTIVE: 'Активные',
    SubscriptionRepository.STATUS_EXPIRED: 'Истёкшие',
    SubscriptionRepository.STATUS_EXHAUSTED: 'Трафик исчерпан',
    SubscriptionRepository.STATUS_DISABLED: 'Отключены',
    SubscriptionRepository.STATUS_TRIAL: 'Trial',
}


def _subscription_row_view(sub, user) -> dict[str, Any]:
    monthly = getattr(sub, 'monthly_traffic_bytes', None)
    if monthly is None or monthly == 0:
        traffic_label = '♾️ Безлимит'
    else:
        traffic_label = f'{bytes_to_gb(monthly)} ГБ / мес.'
    used = int(getattr(sub, 'used_traffic_bytes', 0) or 0)
    limit = getattr(sub, 'data_limit_bytes', None)
    if limit and limit > 0:
        used_label = f'{bytes_to_gb(used)} / {bytes_to_gb(limit)} ГБ'
    else:
        used_label = f'{bytes_to_gb(used)} ГБ' if used else '—'
    is_active = bool(getattr(sub, 'is_active', False))
    expire_at = getattr(sub, 'expire_date', None)
    now = datetime.now(timezone.utc)
    if not is_active:
        status_label, status_tone = 'Отключена', 'rose'
    elif expire_at and expire_at <= now:
        status_label, status_tone = 'Истекла', 'amber'
    elif limit and limit > 0 and used >= limit:
        status_label, status_tone = 'Трафик исчерпан', 'amber'
    else:
        status_label, status_tone = 'Активна', 'emerald'
    return {
        'id': getattr(sub, 'id', None),
        'service_id': getattr(sub, 'service_id', '—'),
        'marzban_username': getattr(sub, 'marzban_username', '—'),
        'is_trial': bool(getattr(sub, 'is_trial', False)),
        'is_active': is_active,
        'status_label': status_label,
        'status_tone': status_tone,
        'traffic_label': traffic_label,
        'used_label': used_label,
        'expire_label': format_dt(expire_at) or '—',
        'tariff_code': getattr(sub, 'current_tariff_code', None) or '—',
        'created_at': format_dt(getattr(sub, 'created_at', None)),
        'user_id': getattr(user, 'id', None),
        'user_tg_id': getattr(user, 'tg_id', None),
        'user_username': getattr(user, 'username', None),
    }


@router.get('/admin/subscriptions/', response_class=HTMLResponse, dependencies=[Depends(require_any)])
async def admin_subscriptions(
    request: Request,
    page: int = Query(default=1, ge=1),
    status: str = Query(default='all'),
    q: str | None = Query(default=None),
    tariff: str | None = Query(default=None),
):
    """Список подписок с поиском и фильтрами (FEA-ADMIN-SUB-CRM #1).

    Поиск: по `service_id`, `marzban_username`, `users.username`,
    `current_tariff_code`, или числовому `id` подписки/`user.id`/`user.tg_id`.
    Фильтры: статус (all/active/expired/exhausted/disabled/trial),
    конкретный tariff_code.
    """
    sessionmaker = request.app.state.sessionmaker
    templates = request.app.state.templates

    status_filter = SubscriptionRepository.normalize_admin_status_filter(status)
    query = (q or '').strip() or None
    tariff_code = (tariff or '').strip() or None
    offset = (page - 1) * _SUBSCRIPTIONS_PAGE_SIZE

    async with sessionmaker() as session:
        sub_repo = SubscriptionRepository(session)
        rows_raw = await sub_repo.admin_search(
            query=query,
            status_filter=status_filter,
            tariff_code=tariff_code,
            limit=_SUBSCRIPTIONS_PAGE_SIZE,
            offset=offset,
        )
        total = await sub_repo.admin_search_count(
            query=query,
            status_filter=status_filter,
            tariff_code=tariff_code,
        )
        tariffs = await TariffRepository(session).list_active()

    rows = [_subscription_row_view(sub, usr) for sub, usr in rows_raw]
    has_next_page = (page * _SUBSCRIPTIONS_PAGE_SIZE) < total

    return templates.TemplateResponse(
        request,
        'admin_subscriptions.html',
        {
            'current_page': 'subscriptions',
            'rows': rows,
            'total': total,
            'page': page,
            'has_prev': page > 1,
            'has_next_page': has_next_page,
            'status_filter': status_filter,
            'status_filters': [
                (value, label) for value, label in _SUBSCRIPTION_STATUS_LABELS.items()
            ],
            'query': query or '',
            'tariff_filter': tariff_code or '',
            'tariff_options': [
                {'code': t.code, 'title': t.title} for t in tariffs if getattr(t, 'code', None)
            ],
            'success_message': request.query_params.get('success'),
            'error_message': request.query_params.get('error'),
        },
    )


@router.get('/admin/subscriptions/{subscription_id}', response_class=HTMLResponse, dependencies=[Depends(require_any)])
async def admin_subscription_detail(request: Request, subscription_id: int):
    """Карточка одной подписки (FEA-ADMIN-SUB-CRM #2).

    Локальное состояние из БД + (best-effort) snapshot из Marzban с
    expire/data_limit/used/status. Marzban-вызов изолирован: при сбое
    показываем локальный state и предупреждение (страница остаётся
    рабочей, action-кнопки активны)."""
    sessionmaker = request.app.state.sessionmaker
    settings: Settings = request.app.state.settings
    templates = request.app.state.templates

    async with sessionmaker() as session:
        sub = await SubscriptionRepository(session).get_by_id(subscription_id)
        if sub is None:
            raise HTTPException(status_code=404, detail='Subscription not found')
        user = await UserRepository(session).get_by_id(sub.user_id)
        tariffs = await TariffRepository(session).list_active()
        local_view = _subscription_row_view(sub, user)
        marzban_username = sub.marzban_username
        sub_url = sub.subscription_url

    marzban_snapshot: dict[str, Any] | None = None
    marzban_error: str | None = None
    if settings.marzban_enabled and marzban_username:
        client = MarzbanClient(settings)
        try:
            remote = await client.get_user(marzban_username)
            marzban_snapshot = {
                'status': getattr(remote, 'status', None) or '—',
                'expire_label': format_dt(getattr(remote, 'expire_datetime', None)) or '—',
                'data_limit_gb': bytes_to_gb(getattr(remote, 'data_limit', None) or 0) if (getattr(remote, 'data_limit', None) or 0) else '♾️',
                'used_gb': bytes_to_gb(getattr(remote, 'used_traffic', None) or 0),
                'subscription_url': getattr(remote, 'subscription_url', None) or sub_url,
                'note': getattr(remote, 'raw', {}).get('note') if hasattr(remote, 'raw') else None,
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning('Marzban snapshot failed for sub_id=%s: %s', subscription_id, exc)
            marzban_error = str(exc)
        finally:
            with suppress(Exception):
                await client.close()

    return templates.TemplateResponse(
        request,
        'admin_subscription_detail.html',
        {
            'current_page': 'subscriptions',
            'sub': local_view,
            'sub_url': sub_url,
            'marzban_snapshot': marzban_snapshot,
            'marzban_error': marzban_error,
            'marzban_enabled': settings.marzban_enabled,
            'tariff_options': [
                {'code': t.code, 'title': t.title}
                for t in tariffs if getattr(t, 'code', None)
            ],
            'success_message': request.query_params.get('success'),
            'error_message': request.query_params.get('error'),
        },
    )


async def _load_sub_or_redirect(sessionmaker, subscription_id: int):
    """Загрузить подписку и user/marzban_username для action-роутов.

    Возвращает (sub_dict, redirect_response_on_error). При успехе
    redirect_response_on_error = None.
    """
    async with sessionmaker() as session:
        sub = await SubscriptionRepository(session).get_by_id(subscription_id)
        if sub is None:
            return None, _redirect_with_message(
                '/admin/subscriptions/', error='Подписка не найдена'
            )
        return (
            {
                'id': sub.id,
                'user_id': sub.user_id,
                'service_id': sub.service_id,
                'marzban_username': sub.marzban_username,
                'expire_date': sub.expire_date,
                'data_limit_bytes': sub.data_limit_bytes,
                'monthly_traffic_bytes': sub.monthly_traffic_bytes,
                'online_limit': sub.online_limit,
                'is_active': sub.is_active,
                'current_tariff_code': sub.current_tariff_code,
            },
            None,
        )


@router.post('/admin/subscriptions/{subscription_id}/extend', dependencies=[Depends(require_finance_or_support)])
async def admin_subscription_extend(
    request: Request,
    subscription_id: int,
    months: int = Form(...),
    principal: WebAdminPrincipal = Depends(require_finance_or_support),
):
    """Продлить подписку на N месяцев (1..36) через
    `MarzbanClient.renew_subscription` (продлевает от текущего expire,
    если активна, или от now если истекла; reset_traffic для нового
    цикла)."""
    sessionmaker = request.app.state.sessionmaker
    settings: Settings = request.app.state.settings
    redirect = f'/admin/subscriptions/{subscription_id}'

    if months < 1 or months > 36:
        return _redirect_with_message(redirect, error='Срок продления должен быть 1..36 месяцев')

    sub_view, err = await _load_sub_or_redirect(sessionmaker, subscription_id)
    if err is not None:
        return err
    marzban_username = sub_view['marzban_username']

    if not settings.marzban_enabled or not marzban_username:
        return _redirect_with_message(redirect, error='Marzban отключён — продление невозможно')

    client = MarzbanClient(settings)
    try:
        tariff_limit_gb: int | None = None
        if sub_view['monthly_traffic_bytes']:
            tariff_limit_gb = max(1, int(sub_view['monthly_traffic_bytes'] // (1024 ** 3)))
        try:
            remote = await client.renew_subscription(
                marzban_username,
                months=months,
                tariff_limit_gb=tariff_limit_gb,
                online_limit=sub_view.get('online_limit'),
                note=f'admin_extend_{principal.username}',
                status='active',
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception('Marzban renew failed sub_id=%s', subscription_id)
            return _redirect_with_message(redirect, error=f'Marzban: {exc}')
    finally:
        with suppress(Exception):
            await client.close()

    new_expire = getattr(remote, 'expire_datetime', None)

    async with sessionmaker.begin() as session:
        sub_repo = SubscriptionRepository(session)
        sub = await sub_repo.get_by_id_for_update(subscription_id)
        if sub is None:
            return _redirect_with_message(redirect, error='Подписка пропала между запросами')
        previous_expire = sub.expire_date
        if new_expire is not None:
            sub.expire_date = new_expire
        sub.is_active = True
        await session.flush()
        await AuditLogRepository(session).create(
            action=AuditAction.subscription_extended,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            actor_username=principal.username,
            entity_type='subscription',
            entity_id=str(subscription_id),
            details={
                'user_id': sub_view['user_id'],
                'months': months,
                'previous_expire_at': previous_expire.isoformat() if previous_expire else None,
                'new_expire_at': new_expire.isoformat() if new_expire else None,
                'marzban_username': marzban_username,
            },
        )
    return _redirect_with_message(redirect, success=f'Подписка продлена на {months} мес.')


@router.post('/admin/subscriptions/{subscription_id}/reset-traffic', dependencies=[Depends(require_finance_or_support)])
async def admin_subscription_reset_traffic(
    request: Request,
    subscription_id: int,
    principal: WebAdminPrincipal = Depends(require_finance_or_support),
):
    """Сбросить использованный трафик подписки до нуля
    (`MarzbanClient.reset_user_usage`). Локальные `used_traffic_bytes`
    и `cycle_extra_traffic_bytes` обнуляются после успешного Marzban-вызова.
    """
    sessionmaker = request.app.state.sessionmaker
    settings: Settings = request.app.state.settings
    redirect = f'/admin/subscriptions/{subscription_id}'

    sub_view, err = await _load_sub_or_redirect(sessionmaker, subscription_id)
    if err is not None:
        return err
    marzban_username = sub_view['marzban_username']

    if not settings.marzban_enabled or not marzban_username:
        return _redirect_with_message(redirect, error='Marzban отключён — сброс невозможен')

    client = MarzbanClient(settings)
    try:
        try:
            await client.reset_user_usage(marzban_username)
        except Exception as exc:  # noqa: BLE001
            logger.exception('Marzban reset_user_usage failed sub_id=%s', subscription_id)
            return _redirect_with_message(redirect, error=f'Marzban: {exc}')
    finally:
        with suppress(Exception):
            await client.close()

    async with sessionmaker.begin() as session:
        sub = await SubscriptionRepository(session).get_by_id_for_update(subscription_id)
        if sub is None:
            return _redirect_with_message(redirect, error='Подписка пропала между запросами')
        previous_used = int(sub.used_traffic_bytes or 0)
        sub.used_traffic_bytes = 0
        sub.cycle_extra_traffic_bytes = 0
        sub.notified_low_traffic = False
        sub.notified_exhausted = False
        await session.flush()
        await AuditLogRepository(session).create(
            action=AuditAction.subscription_traffic_reset,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            actor_username=principal.username,
            entity_type='subscription',
            entity_id=str(subscription_id),
            details={
                'user_id': sub_view['user_id'],
                'previous_used_bytes': previous_used,
                'marzban_username': marzban_username,
            },
        )
    return _redirect_with_message(redirect, success='Трафик подписки обнулён')


async def _toggle_subscription_active(
    request: Request,
    *,
    subscription_id: int,
    target_active: bool,
    audit_action: AuditAction,
    success_message: str,
    principal: WebAdminPrincipal,
    reason: str | None = None,
) -> RedirectResponse:
    sessionmaker = request.app.state.sessionmaker
    settings: Settings = request.app.state.settings
    redirect = f'/admin/subscriptions/{subscription_id}'

    sub_view, err = await _load_sub_or_redirect(sessionmaker, subscription_id)
    if err is not None:
        return err
    marzban_username = sub_view['marzban_username']
    if sub_view['is_active'] == target_active:
        return _redirect_with_message(
            redirect,
            error='Подписка уже в этом состоянии',
        )

    if settings.marzban_enabled and marzban_username:
        client = MarzbanClient(settings)
        try:
            try:
                await client.safe_modify_user(
                    marzban_username,
                    status='active' if target_active else 'disabled',
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    'Marzban toggle failed sub_id=%s target_active=%s',
                    subscription_id,
                    target_active,
                )
                return _redirect_with_message(redirect, error=f'Marzban: {exc}')
        finally:
            with suppress(Exception):
                await client.close()

    async with sessionmaker.begin() as session:
        sub = await SubscriptionRepository(session).get_by_id_for_update(subscription_id)
        if sub is None:
            return _redirect_with_message(redirect, error='Подписка пропала между запросами')
        sub.is_active = target_active
        await session.flush()
        await AuditLogRepository(session).create(
            action=audit_action,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            actor_username=principal.username,
            entity_type='subscription',
            entity_id=str(subscription_id),
            details={
                'user_id': sub_view['user_id'],
                'service_id': sub_view['service_id'],
                'marzban_username': marzban_username,
                'reason': reason,
                'marzban_called': bool(settings.marzban_enabled and marzban_username),
            },
        )
    return _redirect_with_message(redirect, success=success_message)


@router.post('/admin/subscriptions/{subscription_id}/disable', dependencies=[Depends(require_finance_or_support)])
async def admin_subscription_disable(
    request: Request,
    subscription_id: int,
    reason: str = Form(default=''),
    principal: WebAdminPrincipal = Depends(require_finance_or_support),
):
    return await _toggle_subscription_active(
        request,
        subscription_id=subscription_id,
        target_active=False,
        audit_action=AuditAction.subscription_disabled,
        success_message='Подписка отключена',
        principal=principal,
        reason=(reason or '').strip()[:255] or None,
    )


@router.post('/admin/subscriptions/{subscription_id}/enable', dependencies=[Depends(require_finance_or_support)])
async def admin_subscription_enable(
    request: Request,
    subscription_id: int,
    principal: WebAdminPrincipal = Depends(require_finance_or_support),
):
    return await _toggle_subscription_active(
        request,
        subscription_id=subscription_id,
        target_active=True,
        audit_action=AuditAction.subscription_enabled,
        success_message='Подписка снова активна',
        principal=principal,
    )


def _resolve_tariff_traffic_gb(tariff) -> int | None:
    """Вернуть «месячный лимит трафика» из тарифа в ГБ.

    Приоритет: `monthly_traffic_gb` (legacy) → `fixed_traffic_gb` →
    `base_traffic_gb`. None означает безлимит (Marzban data_limit=0).
    """
    for attr in ('monthly_traffic_gb', 'fixed_traffic_gb', 'base_traffic_gb'):
        value = getattr(tariff, attr, None)
        if value is not None:
            return int(value)
    return None


@router.post('/admin/subscriptions/{subscription_id}/change-tariff', dependencies=[Depends(require_finance_or_support)])
async def admin_subscription_change_tariff(
    request: Request,
    subscription_id: int,
    tariff_code: str = Form(...),
    principal: WebAdminPrincipal = Depends(require_finance_or_support),
):
    """Сменить тариф подписки (FEA-ADMIN-SUB-CRM #3).

    Подбирает TariffPlan по `code`, считает целевой data_limit (ГБ),
    делает `safe_modify_user(data_limit=...)` в Marzban, после успеха
    локально сохраняет `current_tariff_id/code`, `monthly_traffic_bytes`
    и `data_limit_bytes`. Не меняет expire (для extends — отдельная
    кнопка); НЕ сбрасывает used_traffic (саппорт может сбросить
    отдельной кнопкой если нужно).
    """
    sessionmaker = request.app.state.sessionmaker
    settings: Settings = request.app.state.settings
    redirect = f'/admin/subscriptions/{subscription_id}'
    normalized_code = (tariff_code or '').strip()
    if not normalized_code:
        return _redirect_with_message(redirect, error='Не указан код тарифа')

    sub_view, err = await _load_sub_or_redirect(sessionmaker, subscription_id)
    if err is not None:
        return err
    marzban_username = sub_view['marzban_username']

    async with sessionmaker() as session:
        tariff = await TariffRepository(session).get_by_code(normalized_code)
    if tariff is None or not getattr(tariff, 'is_active', True):
        return _redirect_with_message(redirect, error=f'Тариф «{normalized_code}» не найден или неактивен')
    if tariff.code == sub_view['current_tariff_code']:
        return _redirect_with_message(redirect, error='Подписка уже на этом тарифе')

    target_traffic_gb = _resolve_tariff_traffic_gb(tariff)
    target_data_limit_bytes = (target_traffic_gb or 0) * (1024 ** 3)
    online_limit = getattr(tariff, 'online_limit_single', None)

    if not settings.marzban_enabled or not marzban_username:
        return _redirect_with_message(redirect, error='Marzban отключён — смена тарифа невозможна')

    client = MarzbanClient(settings)
    try:
        try:
            payload: dict[str, Any] = {'data_limit': target_data_limit_bytes}
            if online_limit is not None and settings.marzban_online_limit_field:
                payload[settings.marzban_online_limit_field] = int(online_limit)
            await client.safe_modify_user(marzban_username, **payload)
        except Exception as exc:  # noqa: BLE001
            logger.exception('Marzban change-tariff failed sub_id=%s', subscription_id)
            return _redirect_with_message(redirect, error=f'Marzban: {exc}')
    finally:
        with suppress(Exception):
            await client.close()

    async with sessionmaker.begin() as session:
        sub = await SubscriptionRepository(session).get_by_id_for_update(subscription_id)
        if sub is None:
            return _redirect_with_message(redirect, error='Подписка пропала между запросами')
        previous_tariff = sub.current_tariff_code
        previous_data_limit = sub.data_limit_bytes
        sub.current_tariff_id = tariff.id
        sub.current_tariff_code = tariff.code
        sub.monthly_traffic_bytes = target_data_limit_bytes if target_traffic_gb else None
        sub.data_limit_bytes = target_data_limit_bytes if target_traffic_gb else None
        if online_limit is not None:
            sub.online_limit = int(online_limit)
        await session.flush()
        await AuditLogRepository(session).create(
            action=AuditAction.subscription_tariff_changed,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            actor_username=principal.username,
            entity_type='subscription',
            entity_id=str(subscription_id),
            details={
                'user_id': sub_view['user_id'],
                'previous_tariff_code': previous_tariff,
                'new_tariff_code': tariff.code,
                'previous_data_limit_bytes': previous_data_limit,
                'new_data_limit_bytes': target_data_limit_bytes,
                'marzban_username': marzban_username,
            },
        )
    return _redirect_with_message(redirect, success=f'Тариф изменён на «{tariff.code}»')


@router.post('/admin/subscriptions/{subscription_id}/reissue-url', dependencies=[Depends(require_support)])
async def admin_subscription_reissue_url(
    request: Request,
    subscription_id: int,
    principal: WebAdminPrincipal = Depends(require_support),
):
    """Перевыпустить subscription URL (FEA-ADMIN-SUB-CRM #3).

    Marzban: `POST /api/user/{username}/revoke_sub` — старая ссылка
    становится невалидной, выдаётся новая. После успеха локально
    обновляется `subscription.subscription_url`.
    """
    sessionmaker = request.app.state.sessionmaker
    settings: Settings = request.app.state.settings
    redirect = f'/admin/subscriptions/{subscription_id}'

    sub_view, err = await _load_sub_or_redirect(sessionmaker, subscription_id)
    if err is not None:
        return err
    marzban_username = sub_view['marzban_username']

    if not settings.marzban_enabled or not marzban_username:
        return _redirect_with_message(redirect, error='Marzban отключён — re-issue невозможен')

    client = MarzbanClient(settings)
    try:
        try:
            remote = await client.revoke_subscription_url(marzban_username)
        except Exception as exc:  # noqa: BLE001
            logger.exception('Marzban revoke_subscription_url failed sub_id=%s', subscription_id)
            return _redirect_with_message(redirect, error=f'Marzban: {exc}')
    finally:
        with suppress(Exception):
            await client.close()

    new_url = getattr(remote, 'subscription_url', None)

    async with sessionmaker.begin() as session:
        sub = await SubscriptionRepository(session).get_by_id_for_update(subscription_id)
        if sub is None:
            return _redirect_with_message(redirect, error='Подписка пропала между запросами')
        previous_url = sub.subscription_url
        if new_url:
            sub.subscription_url = new_url
        await session.flush()
        await AuditLogRepository(session).create(
            action=AuditAction.subscription_url_reissued,
            actor_type=AuditActorType.admin,
            actor_tg_id=None,
            actor_username=principal.username,
            entity_type='subscription',
            entity_id=str(subscription_id),
            details={
                'user_id': sub_view['user_id'],
                'marzban_username': marzban_username,
                'previous_url_present': bool(previous_url),
                'new_url_present': bool(new_url),
            },
        )
    return _redirect_with_message(redirect, success='Subscription URL перевыпущен')


@router.get('/admin/promocodes/', response_class=HTMLResponse, dependencies=[Depends(require_any)])
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
        # FEA-ADMIN-CRUD-EXPAND: список тарифов для select'а `unlocks_tariff_id`
        # в форме промокода. Берём list_all() — админ должен видеть в т.ч.
        # архивные тарифы, чтобы не «терять» привязку при архивации тарифа.
        tariff_options = await TariffRepository(session).list_all()

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
                unlocks_tariff_id=getattr(promo, 'unlocks_tariff_id', None),
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
            'tariff_options': [
                SimpleNamespace(
                    id=getattr(plan, 'id', None),
                    code=getattr(plan, 'code', '') or '',
                    title=getattr(plan, 'title', '') or getattr(plan, 'code', '') or f'#{getattr(plan, "id", "?")}',
                    is_active=bool(getattr(plan, 'is_active', False)),
                    is_archived=bool(getattr(plan, 'is_archived', False)),
                )
                for plan in tariff_options
            ],
        },
    )


@router.post('/admin/promocodes/', dependencies=[Depends(require_finance)])
async def admin_promocodes_update(
    request: Request,
    action: str | None = Form(default=None),
    code: str | None = Form(default=None),
    bonus_amount: str | None = Form(default=None),
    max_uses: str | None = Form(default=None),
    expires_at: str | None = Form(default=None),
    promo_id: int | None = Form(default=None),
    is_active: bool = Form(default=False),
    unlocks_tariff_id: str | None = Form(default=None),
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
                    unlocks_tariff_id=unlocks_tariff_id,
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
                    unlocks_tariff_id=unlocks_tariff_id,
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


@router.get('/admin/broadcasts/', response_class=HTMLResponse, dependencies=[Depends(require_any)])
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
        user_repo = UserRepository(session)
        jobs = await _broadcast_list_recent_filtered(repo, limit=ADMIN_PAGE_SIZE, offset=offset, status_filter=status_filter)
        total = await _broadcast_count_filtered(repo, status_filter=status_filter)
        summary_counts = await _broadcast_summary_counts(repo)
        edit_job = await repo.get_by_id(edit_id) if edit_id else None
        # FEA-ADMIN-CRUD-EXPAND: предпросмотр реальной аудитории.
        # `count_broadcast_recipients` исключает bot_blocked + is_blocked; это
        # то же условие, что `broadcast_polling._claim_next_job` использует
        # для нарезки чанков. Показываем единожды на странице — сегментация
        # пока не реализована, поэтому число одинаково для всех job'ов.
        broadcast_recipients_count = await user_repo.count_broadcast_recipients()

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
            'broadcast_recipients_count': broadcast_recipients_count,
            'has_prev': page > 1,
            'has_next': offset + len(jobs) < total,
            'success_message': request.query_params.get('success'),
            'error_message': request.query_params.get('error'),
        },
    )


@router.post('/admin/broadcasts/', dependencies=[Depends(require_support)])
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


@router.post('/admin/broadcasts/test', dependencies=[Depends(require_support)])
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

@router.get('/admin/links/', response_class=HTMLResponse, dependencies=[Depends(require_any)])
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


@router.post('/admin/links/{link_id}', dependencies=[Depends(require_superadmin)])
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



@router.get('/admin/marzban-page/', response_class=HTMLResponse, dependencies=[Depends(require_any)])
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


@router.post('/admin/marzban-page/', dependencies=[Depends(require_superadmin)])
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


@router.post('/admin/marzban-page/env/', dependencies=[Depends(require_superadmin)])
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


@router.post('/admin/marzban-page/apply/', dependencies=[Depends(require_superadmin)])
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


_MARZBAN_PREVIEW_CSP = (
    "default-src 'self'; "
    "img-src 'self' data: https:; "
    "style-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://cdnjs.cloudflare.com; "
    "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://cdnjs.cloudflare.com; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'"
)


@router.get('/admin/marzban-page/preview', response_class=HTMLResponse, dependencies=[Depends(require_any)])
async def admin_marzban_page_preview(request: Request):
    sessionmaker = request.app.state.sessionmaker
    settings = request.app.state.settings

    try:
        async with sessionmaker.begin() as session:
            renderer = MarzbanTemplateRenderer(session, settings)
            render_result = await renderer.render_preview()
        # Preview рендерит публичный Marzban-template с inline-style/script
        # и Tailwind CDN. Под admin CSP это бы заблокировало контент;
        # ставим relaxed CSP только на этот один роут (само admin-UI остаётся
        # под strict CSP middleware'а).
        response = HTMLResponse(render_result.rendered_html, status_code=200)
        response.headers['Content-Security-Policy'] = _MARZBAN_PREVIEW_CSP
        return response
    except Exception as exc:
        escaped = html.escape(str(exc))
        body = f"""<!doctype html>
<html lang='ru'>
  <head>
    <meta charset='utf-8'>
    <title>Marzban preview failed</title>
    <link rel='stylesheet' href='/static/admin.css'>
  </head>
  <body class='preview-error-page'>
    <div class='card'>
      <h1>❌ Preview render failed</h1>
      <p>Jinja preview or Marzban page context build failed. The admin page should still stay usable; fix the error below and reload preview.</p>
      <pre>{escaped}</pre>
    </div>
  </body>
</html>"""
        return HTMLResponse(body, status_code=500)


@router.get('/admin/marzban-ops/', response_class=HTMLResponse, dependencies=[Depends(require_any)])
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


@router.post('/admin/marzban-ops/geodata/update', dependencies=[Depends(require_superadmin)])
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


@router.get('/admin/nodes/', response_class=HTMLResponse, dependencies=[Depends(require_any)])
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


@router.post('/admin/nodes/', dependencies=[Depends(require_superadmin)])
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


_NODE_PROBE_RANGES: dict[str, dict[str, Any]] = {
    '1h': {'hours': 1, 'bucket_seconds': None, 'label': '1 час'},
    '24h': {'hours': 24, 'bucket_seconds': 60, 'label': '24 часа'},
    '7d': {'hours': 24 * 7, 'bucket_seconds': 3600, 'label': '7 дней'},
}


def _serialize_probe_point(point: NodeHealthRangePoint) -> dict[str, Any]:
    return {
        'ts': point.ts.isoformat(),
        'latency_ms': point.latency_ms_avg,
        'users_online': point.users_online_avg,
        'users_total': point.users_total_avg,
        'ok': point.ok_count,
        'fail': point.fail_count,
    }


async def _load_probe_samples(
    session,
    *,
    node_id: int,
    range_key: str,
) -> tuple[dict[str, Any], str]:
    """Грузит точки графика для ноды + резолвит meta для UI.

    Возвращает (payload, resolved_range_key). Если `range_key` неизвестен —
    дефолтится в '24h'. payload содержит `points`, `bucket_seconds`, `range`,
    `since`/`until` (ISO) — клиентский JS отрисует это в Canvas.
    """
    resolved = range_key if range_key in _NODE_PROBE_RANGES else '24h'
    cfg = _NODE_PROBE_RANGES[resolved]
    until = datetime.now(timezone.utc)
    since = until - timedelta(hours=cfg['hours'])

    repo = NodeHealthSampleRepository(session)
    points = await repo.range_for_node(
        node_id,
        since=since,
        until=until,
        bucket_seconds=cfg['bucket_seconds'],
    )
    return (
        {
            'range': resolved,
            'range_label': cfg['label'],
            'bucket_seconds': cfg['bucket_seconds'],
            'since': since.isoformat(),
            'until': until.isoformat(),
            'points': [_serialize_probe_point(p) for p in points],
        },
        resolved,
    )


@router.get('/admin/nodes/{node_id}', response_class=HTMLResponse, dependencies=[Depends(require_any)])
async def admin_node_detail(
    request: Request,
    node_id: int,
    range: str = Query(default='24h'),
):
    templates = request.app.state.templates
    sessionmaker = request.app.state.sessionmaker

    async with sessionmaker.begin() as session:
        node_repo = NodeRegistryRepository(session)
        sample_repo = NodeHealthSampleRepository(session)
        node = await node_repo.get_by_id(node_id)
        if node is None:
            return _redirect_with_message('/admin/nodes/', error=f'Узел id={node_id} не найден')

        chart_payload, resolved_range = await _load_probe_samples(
            session, node_id=node_id, range_key=range,
        )
        latest_sample = await sample_repo.latest_for_node(node_id)

    row_summary = _node_row_for_admin(node)
    latest_payload: dict[str, Any] | None = None
    if latest_sample is not None:
        latest_payload = {
            'ts': format_dt(latest_sample.ts),
            'status': latest_sample.status.value,
            'latency_ms': latest_sample.latency_ms,
            'users_online': latest_sample.users_online,
            'users_total': latest_sample.users_total,
            'error_text': (latest_sample.error_text or '').strip() or None,
        }

    return templates.TemplateResponse(
        request,
        'admin_node_detail.html',
        {
            'current_page': 'nodes',
            'node': node,
            'row': row_summary,
            'latest_sample': latest_payload,
            'range_key': resolved_range,
            'range_options': [
                {'key': key, 'label': cfg['label']} for key, cfg in _NODE_PROBE_RANGES.items()
            ],
            'chart_data_json': json.dumps(chart_payload, ensure_ascii=False),
            'success_message': request.query_params.get('success'),
            'error_message': request.query_params.get('error'),
        },
    )


@router.get('/admin/nodes/{node_id}/samples.json', dependencies=[Depends(require_any)])
async def admin_node_samples_json(
    request: Request,
    node_id: int,
    range: str = Query(default='24h'),
):
    sessionmaker = request.app.state.sessionmaker
    async with sessionmaker.begin() as session:
        node = await NodeRegistryRepository(session).get_by_id(node_id)
        if node is None:
            return JSONResponse({'error': 'node_not_found'}, status_code=404)
        payload, _ = await _load_probe_samples(session, node_id=node_id, range_key=range)
    return JSONResponse(payload)


@router.get('/admin/routing-profiles/', response_class=HTMLResponse, dependencies=[Depends(require_any)])
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


@router.post('/admin/routing-profiles/', dependencies=[Depends(require_superadmin)])
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
    profile_ids: list[int] = Form(default=[]),
    bulk_target_state: str | None = Form(default=None),
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

            if normalized_action == 'copy':
                # FEA-ADMIN-CRUD-EXPAND: копия routing-profile.
                # Безопасно: новый profile создаётся `is_enabled=False`,
                # `is_default=False`. Чтобы включить — отдельным редактированием.
                if profile_id is None:
                    return _redirect_with_message('/admin/routing-profiles/', error='profile_id не указан')
                src = await service.get_by_id(profile_id)
                if src is None:
                    return _redirect_with_message('/admin/routing-profiles/', error='Исходный routing profile не найден')

                base_code = (src.code or '').strip().lower() or f'profile-{src.id}'
                new_code = f'{base_code}-copy'
                if await service.get_by_code(new_code) is not None:
                    for suffix in range(2, 100):
                        candidate = f'{base_code}-copy-{suffix}'
                        if await service.get_by_code(candidate) is None:
                            new_code = candidate
                            break
                    else:
                        return _redirect_with_message('/admin/routing-profiles/', error='Не удалось подобрать уникальный code для копии')

                src_title = (src.title or src.code or '').strip()
                copy_row = await service.create(
                    code=new_code,
                    title=f'{src_title} (копия)' if src_title else f'Копия {new_code}',
                    description=src.description,
                    is_enabled=False,
                    is_default=False,
                    sort_order=int(src.sort_order or 100),
                    match_tags=list(src.match_tags or []),
                    config_json=dict(src.config_json or {}),
                    notes=src.notes,
                )
                await audit_repo.create(
                    action=AuditAction.routing_profile_updated,
                    actor_type=AuditActorType.admin,
                    actor_tg_id=None,
                    entity_type='routing_profile',
                    entity_id=str(copy_row.id),
                    details={
                        'action': 'copy',
                        'source_profile_id': src.id,
                        'source_code': src.code,
                        'new_code': copy_row.code,
                    },
                )
                return _redirect_with_message(
                    f'/admin/routing-profiles/?edit_id={copy_row.id}',
                    success=f'Создана копия: {copy_row.code} (выключена, отредактируйте и включите явно)',
                )

            if normalized_action == 'bulk_toggle':
                # bulk enable/disable. Default-profile исключаем из disable
                # (он должен оставаться включённым, иначе routing развалится).
                normalized_target = (bulk_target_state or '').strip().lower()
                if normalized_target not in {'enable', 'disable'}:
                    return _redirect_with_message('/admin/routing-profiles/', error='bulk_target_state должен быть enable|disable')

                unique_ids: list[int] = []
                seen: set[int] = set()
                for raw_id in profile_ids or []:
                    try:
                        pid = int(raw_id)
                    except (TypeError, ValueError):
                        continue
                    if pid < 1 or pid in seen:
                        continue
                    seen.add(pid)
                    unique_ids.append(pid)

                if not unique_ids:
                    return _redirect_with_message('/admin/routing-profiles/', error='Не выбрано ни одного routing-profile')

                target_state = normalized_target == 'enable'
                changed: list[int] = []
                skipped_default: list[int] = []
                not_found: list[int] = []
                for pid in unique_ids:
                    row = await service.get_by_id_for_update(pid)
                    if row is None:
                        not_found.append(pid)
                        continue
                    if not target_state and bool(getattr(row, 'is_default', False)):
                        skipped_default.append(pid)
                        continue
                    if bool(row.is_enabled) == target_state:
                        continue
                    await service.update(row, is_enabled=target_state)
                    changed.append(pid)
                    await audit_repo.create(
                        action=AuditAction.routing_profile_updated,
                        actor_type=AuditActorType.admin,
                        actor_tg_id=None,
                        entity_type='routing_profile',
                        entity_id=str(pid),
                        details={
                            'action': 'bulk_toggle',
                            'target_state': normalized_target,
                            'bulk': True,
                            'bulk_batch_size': len(unique_ids),
                        },
                    )

                parts: list[str] = []
                parts.append(f'{"включено" if target_state else "выключено"} {len(changed)}')
                if skipped_default:
                    parts.append(f'пропущен default ({len(skipped_default)})')
                if not_found:
                    parts.append(f'не найдено {len(not_found)}')
                return _redirect_with_message('/admin/routing-profiles/', success='Bulk-toggle: ' + ', '.join(parts))
    except (ValueError, RoutingProfileValidationError) as exc:
        return _redirect_with_message('/admin/routing-profiles/', error=str(exc))
    except Exception as exc:
        return _redirect_with_message('/admin/routing-profiles/', error=str(exc))

    return _redirect_with_message('/admin/routing-profiles/', error='Неизвестное действие')

@router.get('/admin/invoices/', response_class=HTMLResponse, dependencies=[Depends(require_any)])
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

    # FEA-ADMIN-CRUD-EXPAND: per-provider статистика из загруженной выборки.
    # Берём ту же 500-инвойсную выборку, что и для списка, чтобы не делать
    # лишних запросов и видеть стат-снимок по «свежим» инвойсам.
    provider_stats_map: dict[str, dict[str, Any]] = {}
    pending_stale_count = 0
    pending_cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    for inv in invoices:
        provider_key = (getattr(inv, 'provider', None) or 'unknown')
        status_value = getattr(getattr(inv, 'status', None), 'value', getattr(inv, 'status', None))
        bucket = provider_stats_map.setdefault(
            provider_key,
            {'provider': provider_key, 'total': 0, 'paid': 0, 'pending': 0, 'cancelled': 0,
             'paid_amount': Decimal('0.00')},
        )
        bucket['total'] += 1
        if status_value == 'paid' or status_value == 'consumed':
            bucket['paid'] += 1
            try:
                bucket['paid_amount'] += Decimal(str(getattr(inv, 'payable_amount', '0') or '0'))
            except (InvalidOperation, ValueError):
                pass
        elif status_value == 'pending':
            bucket['pending'] += 1
            created_at = getattr(inv, 'created_at', None)
            if created_at is not None and created_at < pending_cutoff:
                pending_stale_count += 1
        elif status_value == 'cancelled':
            bucket['cancelled'] += 1

    provider_stats = sorted(
        (
            {**bucket, 'paid_amount_str': f'{bucket["paid_amount"]:.2f}'}
            for bucket in provider_stats_map.values()
        ),
        key=lambda b: (-b['total'], b['provider']),
    )

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
            'provider_stats': provider_stats,
            'pending_stale_count': pending_stale_count,
            'pending_stale_cutoff_hours': 24,
        },
    )


@router.post('/admin/invoices/bulk-cancel-stale', dependencies=[Depends(require_finance)])
async def admin_invoices_bulk_cancel_stale(request: Request):
    """Массовая отмена pending-инвойсов старше 24ч (FEA-ADMIN-CRUD-EXPAND).

    Только status=pending + created_at < now-24h. consumed/applying/paid
    не трогаем. Direct DB-mutation (без вызова payment-провайдера) —
    pending значит, что external-провайдер либо не получил callback, либо
    клиент закрыл окно оплаты; в любом случае такой счёт безопасно
    отменить. Аудит — на каждый отменённый, action=admin_action,
    details.action='cancel_invoice_bulk_stale'.
    """
    sessionmaker = request.app.state.sessionmaker
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    cancelled_count = 0
    cancelled_by_provider: dict[str, int] = {}

    async with sessionmaker.begin() as session:
        audit_repo = AuditLogRepository(session)
        res = await session.execute(
            select(Invoice)
            .where(Invoice.status == InvoiceStatus.pending, Invoice.created_at < cutoff)
            .with_for_update()
        )
        invoices = list(res.scalars().all())

        for invoice in invoices:
            invoice.status = InvoiceStatus.cancelled
            invoice.idempotency_key = None
            provider_key = (invoice.provider or 'unknown')
            cancelled_by_provider[provider_key] = cancelled_by_provider.get(provider_key, 0) + 1
            await audit_repo.create(
                action=AuditAction.admin_action,
                actor_type=AuditActorType.admin,
                actor_tg_id=None,
                entity_type='invoice',
                entity_id=str(invoice.id),
                details={
                    'action': 'cancel_invoice_bulk_stale',
                    'provider': provider_key,
                    'purpose': getattr(getattr(invoice, 'purpose', None), 'value', None),
                    'created_at': invoice.created_at.isoformat() if invoice.created_at else None,
                    'cutoff_hours': 24,
                },
            )
            cancelled_count += 1

    if cancelled_count == 0:
        return _redirect_with_message(
            '/admin/invoices/',
            success='Не найдено pending-инвойсов старше 24ч',
        )
    by_provider = ', '.join(f'{p}: {n}' for p, n in sorted(cancelled_by_provider.items()))
    return _redirect_with_message(
        '/admin/invoices/',
        success=f'Отменено инвойсов: {cancelled_count} ({by_provider})',
    )


@router.get('/admin/invoices/{invoice_id}', response_class=HTMLResponse, dependencies=[Depends(require_any)])
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


@router.post('/admin/invoices/{invoice_id}/refresh', dependencies=[Depends(require_finance)])
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


@router.post('/admin/invoices/{invoice_id}/approve', dependencies=[Depends(require_finance)])
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


@router.post('/admin/invoices/{invoice_id}/cancel', dependencies=[Depends(require_finance)])
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


@router.get('/admin/export/{entity}.csv', dependencies=[Depends(require_any)])
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


# FEA-B18: маппинг (нижний регистр → каноничный os_name из AppLinkRepository).
# Порядок проверок важен (iPad detected as iOS, AndroidTV до Android).
_USER_AGENT_OS_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (('android tv', 'smarttv', 'googletv', 'chromecast'), 'AndroidTV'),
    (('iphone', 'ipad', 'ipod'), 'iOS'),
    (('android',), 'Android'),
    (('mac os x', 'macintosh', 'macos'), 'macOS'),
    (('windows nt', 'win64', 'win32'), 'Windows'),
    (('linux',), 'Linux'),
)


def _detect_os_from_user_agent(user_agent: str | None) -> str | None:
    """Возвращает каноничный `os_name` из AppLink (iOS/Android/...) или None.

    Эвристика по подстрокам User-Agent. None означает, что бот не смог
    определить ОС — клиент получает grid всех платформ без выделенной кнопки.
    """
    if not user_agent:
        return None
    haystack = user_agent.lower()
    for needles, os_name in _USER_AGENT_OS_RULES:
        if any(needle in haystack for needle in needles):
            return os_name
    return None


@router.get('/setup/{service_id}', response_class=HTMLResponse)
async def setup_landing(request: Request, service_id: str):
    """FEA-B18: публичная landing-страница «Установить на этом устройстве».

    URL `{PUBLIC_BOT_BASE_URL}/setup/{service_id}` открывается из кнопки в
    боте. Сервер читает User-Agent, определяет ОС, ищет соответствующий
    AppLink и рендерит страницу с выделенной кнопкой «Установить на этом
    устройстве» + grid других платформ + ссылку на subscription URL.

    БЕЗ auth — service_id выступает магическим токеном (8 chars из secrets,
    стат-уникальный). Если не нашли — нейтральная 404-страница без утечки
    инфы о том, существовал ли token.
    """
    templates = request.app.state.templates
    sessionmaker = request.app.state.sessionmaker
    settings = request.app.state.settings

    user_agent = request.headers.get('user-agent') or ''
    detected_os = _detect_os_from_user_agent(user_agent)

    async with sessionmaker() as session:
        sub_repo = SubscriptionRepository(session)
        subscription = await sub_repo.get_by_service_id((service_id or '').strip())
        if subscription is None:
            return templates.TemplateResponse(
                request,
                'public_404.html',
                {'bot_username': _bot_username_or_none(settings)},
                status_code=404,
            )

        app_links = await AppLinkRepository(session).list_all()

    canonical_subscription_url = build_canonical_subscription_url(
        getattr(subscription, 'service_id', None),
        public_origin=configured_public_subscription_origin(settings),
    )

    link_by_os: dict[str, dict[str, str | None]] = {}
    for link in app_links:
        os_key = (link.os_name or '').strip()
        if not os_key:
            continue
        link_by_os[os_key] = {
            'download_url': _safe_public_url_for_display(link.download_url, field_label='Ссылка на приложение'),
            'guide_url': _safe_public_url_for_display(link.guide_url, field_label='Ссылка на инструкцию'),
        }

    detected_link = link_by_os.get(detected_os) if detected_os else None

    fallback_os_cards = [
        {
            'os_name': os_name,
            'download_url': (link_by_os.get(os_name) or {}).get('download_url'),
            'guide_url': (link_by_os.get(os_name) or {}).get('guide_url'),
            'is_detected': bool(detected_os) and os_name == detected_os,
        }
        for os_name in AppLinkRepository.DEFAULT_OS_NAMES
    ]

    return templates.TemplateResponse(
        request,
        'setup_landing.html',
        {
            'detected_os': detected_os,
            'detected_download_url': (detected_link or {}).get('download_url'),
            'detected_guide_url': (detected_link or {}).get('guide_url'),
            'canonical_subscription_url': canonical_subscription_url,
            'fallback_os_cards': fallback_os_cards,
            'bot_username': _bot_username_or_none(settings),
        },
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
