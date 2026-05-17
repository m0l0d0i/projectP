from __future__ import annotations

import asyncio
import copy
import json
from collections import Counter
from contextlib import suppress
import logging
import re
import shlex
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from sqlalchemy.ext.asyncio import AsyncSession
from urllib.parse import urljoin, urlparse

import httpx
from dateutil.relativedelta import relativedelta

from app.config import Settings
from app.services.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError
from app.services.subscription_urls import canonicalize_subscription_url, normalize_public_subscription_origin
from app.db.models import NodeHealthStatus, NodeSourceStatus, NodeSyncState
from app.db.repositories.node_registry import NodeRegistryRepository

logger = logging.getLogger(__name__)


def _secret_or_attr(obj, secure_attr: str, fallback_attr: str) -> str | None:
    value = getattr(obj, secure_attr, None)
    if value is not None:
        return _secret_to_str(value)
    return _secret_to_str(getattr(obj, fallback_attr, None))


def _secret_to_str(value: Any) -> str | None:
    if value is None:
        return None
    getter = getattr(value, 'get_secret_value', None)
    if callable(getter):
        raw = getter()
        if raw is None:
            return None
        normalized = str(raw).strip()
        return normalized or None
    if isinstance(value, str):
        normalized = value.strip()
        return normalized or None
    return str(value).strip() or None


def _marzban_breaker_is_failure(exc: BaseException) -> bool:
    if isinstance(exc, UserAlreadyExistsError):
        return False
    if isinstance(exc, (httpx.NetworkError, httpx.TimeoutException, httpx.RemoteProtocolError)):
        return True
    if isinstance(exc, MarzbanAPIError):
        return exc.status_code is None or exc.status_code >= 500
    return False


class MarzbanAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class UserAlreadyExistsError(MarzbanAPIError):
    pass


@dataclass(slots=True)
class MarzbanUser:
    username: str
    status: str
    expire: int | None
    data_limit: int | None
    used_traffic: int
    subscription_url: str | None
    links: list[str]
    raw: dict[str, Any]

    @property
    def expire_datetime(self) -> datetime | None:
        if not self.expire:
            return None
        return datetime.fromtimestamp(self.expire, tz=timezone.utc)


@dataclass(slots=True)
class MarzbanNodeSnapshot:
    source_node_id: str
    code: str
    display_name: str
    source_status: NodeSourceStatus
    health_status: NodeHealthStatus
    api_base_url: str | None
    subscription_base_url: str | None
    location_code: str | None
    provider_name: str | None
    transport_hint: str | None
    capabilities_json: dict[str, Any]
    raw: dict[str, Any]


@dataclass(slots=True)
class NodeRegistrySyncResult:
    fetched_count: int
    created_count: int
    updated_count: int
    missing_count: int
    error_count: int
    synced_source_ids: list[str]
    detail_messages: list[str]
    source_status_counts: dict[str, int]

    @property
    def total_seen(self) -> int:
        return self.fetched_count

    @property
    def created(self) -> int:
        return self.created_count

    @property
    def updated(self) -> int:
        return self.updated_count

    @property
    def missing_marked(self) -> int:
        return self.missing_count




@dataclass(slots=True)
class XrayRoutingFragmentPreview:
    selected_node_code: str | None
    selected_node_id: str | None
    outbound_tag: str | None
    balancer_tag: str | None
    fragment_patch: dict[str, Any]
    routing_rule: dict[str, Any] | None
    restart_required: bool
    restart_warning: str


@dataclass(slots=True)
class XrayConfigApplyPreview:
    target_config_path: str | None
    backup_path: str | None
    validation_command: list[str]
    restart_command: list[str]
    merged_config: dict[str, Any]
    fragment_preview: XrayRoutingFragmentPreview
    restart_required: bool
    restart_warning: str


@dataclass(slots=True)
class XrayConfigApplyResult:
    target_config_path: str
    backup_path: str | None
    applied: bool
    restart_performed: bool
    rollback_performed: bool
    validation_command: list[str]
    restart_command: list[str]
    validation_stdout: str
    validation_stderr: str
    restart_stdout: str
    restart_stderr: str




class MarzbanClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

        api_base_url = getattr(settings, 'marzban_api_base_url', None) or settings.marzban_base_url
        self._base_url = (api_base_url or '').rstrip('/') or 'http://localhost'
        self._public_subscription_base_url = self._detect_public_subscription_base_url()
        self._marzban_password = _secret_or_attr(settings, 'marzban_password_value', 'marzban_password')

        self._enabled = bool(
            settings.marzban_enabled
            and api_base_url
            and settings.marzban_username
            and self._marzban_password
        )
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=settings.marzban_request_timeout_seconds,
            follow_redirects=True,
        )
        self._token: str | None = None
        self._token_expires_at: datetime | None = None
        self._token_lock = asyncio.Lock()
        self._breaker = CircuitBreaker(
            'marzban',
            failure_threshold=int(getattr(settings, 'marzban_circuit_failure_threshold', 5)),
            cooldown_seconds=float(getattr(settings, 'marzban_circuit_cooldown_seconds', 30.0)),
            is_failure=_marzban_breaker_is_failure,
        )

    async def close(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _resolve_reset_strategy(*, is_trial: bool) -> str:
        return 'no_reset' if is_trial else 'month'

    def _is_absolute_url(self, value: str | None) -> bool:
        if not value:
            return False
        parsed = urlparse(value)
        return bool(parsed.scheme and parsed.netloc)

    @staticmethod
    def _extract_origin(value: str | None) -> str | None:
        return normalize_public_subscription_origin(value)

    def _detect_public_subscription_base_url(self) -> str:
        configured_public = normalize_public_subscription_origin(
            getattr(self.settings, 'marzban_subscription_base_url', None)
        )
        if configured_public:
            return configured_public.rstrip('/')

        api_origin = normalize_public_subscription_origin(
            getattr(self.settings, 'marzban_api_base_url', None) or self.settings.marzban_base_url
        )
        if api_origin:
            return api_origin.rstrip('/')

        fallback_origin = normalize_public_subscription_origin(self._base_url)
        if fallback_origin:
            return fallback_origin.rstrip('/')

        return self._base_url.rstrip('/')

    @property
    def public_subscription_base_url(self) -> str:
        return self._public_subscription_base_url

    def _resolve_public_url(self, value: str | None) -> str | None:
        normalized = (value or '').strip()
        if not normalized:
            return None

        if self._is_absolute_url(normalized):
            return normalized

        public_base = (self._public_subscription_base_url or self._base_url).rstrip('/')
        if normalized.startswith('/'):
            return urljoin(f'{public_base}/', normalized.lstrip('/'))

        return urljoin(f'{public_base}/', normalized)

    def _resolve_public_subscription_url(self, value: str | None) -> str | None:
        normalized = (value or '').strip()
        if not normalized:
            return None

        return canonicalize_subscription_url(
            normalized,
            public_origin=self._public_subscription_base_url,
            allow_bare_token=True,
        )

    def _normalize_links(self, links: list[Any] | None) -> list[str]:
        normalized_links: list[str] = []
        seen: set[str] = set()

        for item in links or []:
            if not isinstance(item, str):
                continue
            resolved = self._resolve_public_url(item)
            if not resolved or resolved in seen:
                continue
            seen.add(resolved)
            normalized_links.append(resolved)

        return normalized_links

    async def _authenticate(self) -> str:
        if not self._enabled:
            raise MarzbanAPIError('Marzban не настроен')

        password = self._marzban_password
        if not password:
            raise MarzbanAPIError('Marzban password is empty')

        response = await self._client.post(
            '/api/admin/token',
            data={
                'grant_type': 'password',
                'username': self.settings.marzban_username,
                'password': password,
            },
        )
        if response.status_code != 200:
            raise MarzbanAPIError(
                f'Marzban auth failed: {response.text}',
                status_code=response.status_code,
            )

        payload = response.json()
        token = payload.get('access_token')
        if not token:
            raise MarzbanAPIError('Marzban auth response does not contain access_token')
        return token

    async def _ensure_token(self) -> str:
        now = datetime.now(timezone.utc)
        if self._token and self._token_expires_at and now < self._token_expires_at:
            return self._token

        async with self._token_lock:
            now = datetime.now(timezone.utc)
            if self._token and self._token_expires_at and now < self._token_expires_at:
                return self._token

            self._token = await self._authenticate()
            ttl = max(30, int(self.settings.marzban_token_ttl_seconds))
            self._token_expires_at = now + timedelta(seconds=ttl)
            return self._token

    async def _request(self, method: str, path: str, *, auth: bool = True, **kwargs: Any) -> httpx.Response:
        if not self._enabled:
            raise MarzbanAPIError('Marzban не настроен')

        try:
            async with self._breaker:
                return await self._do_request(method, path, auth=auth, **kwargs)
        except CircuitBreakerOpenError as exc:
            raise MarzbanAPIError(
                f'Marzban временно недоступен (circuit breaker open, retry in {exc.retry_after:.1f}s)'
            ) from exc

    async def _do_request(self, method: str, path: str, *, auth: bool = True, **kwargs: Any) -> httpx.Response:
        retries = max(0, int(self.settings.marzban_request_retries))
        last_exc: Exception | None = None
        headers = dict(kwargs.pop('headers', {}) or {})

        for attempt in range(retries + 1):
            request_headers = dict(headers)
            try:
                if auth:
                    token = await self._ensure_token()
                    request_headers['Authorization'] = f'Bearer {token}'

                response = await self._client.request(method, path, headers=request_headers, **kwargs)

                if response.status_code == 401 and auth:
                    async with self._token_lock:
                        self._token = None
                        self._token_expires_at = None

                    token = await self._ensure_token()
                    request_headers['Authorization'] = f'Bearer {token}'
                    response = await self._client.request(method, path, headers=request_headers, **kwargs)

                if response.status_code >= 400:
                    raise MarzbanAPIError(
                        f'Marzban error {response.status_code}: {response.text}',
                        status_code=response.status_code,
                    )

                return response

            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                last_exc = exc
                if attempt >= retries:
                    break

                delay = 0.5 * (attempt + 1)
                logger.warning(
                    'Retrying Marzban request %s %s via %s in %.1fs due to %s',
                    method,
                    path,
                    self._base_url,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)

            except MarzbanAPIError as exc:
                if exc.status_code and exc.status_code >= 500 and attempt < retries:
                    delay = 0.5 * (attempt + 1)
                    logger.warning('Retrying Marzban API %s %s in %.1fs due to %s', method, path, delay, exc)
                    await asyncio.sleep(delay)
                    continue
                raise

        assert last_exc is not None
        raise MarzbanAPIError(f'Failed to connect to Marzban at {self._base_url}: {last_exc}')

    def _parse_user(self, payload: dict[str, Any]) -> MarzbanUser:
        raw_subscription_url = payload.get('subscription_url')
        resolved_subscription_url = self._resolve_public_subscription_url(raw_subscription_url)
        if raw_subscription_url and not resolved_subscription_url:
            logger.warning(
                'Ignoring non-canonical Marzban subscription_url for username=%s value=%s',
                payload.get('username'),
                raw_subscription_url,
            )

        return MarzbanUser(
            username=payload['username'],
            status=payload.get('status', 'unknown'),
            expire=payload.get('expire'),
            data_limit=payload.get('data_limit'),
            used_traffic=payload.get('used_traffic', 0),
            subscription_url=resolved_subscription_url,
            links=self._normalize_links(payload.get('links', [])),
            raw=payload,
        )

    @staticmethod
    def _normalize_node_id(value: Any) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    @staticmethod
    def _slugify_node_code(value: str | None) -> str:
        normalized = (value or '').strip().lower()
        normalized = re.sub(r'[^a-z0-9]+', '-', normalized)
        normalized = normalized.strip('-')
        return normalized or 'node'

    @staticmethod
    def _normalize_node_source_status(payload: dict[str, Any]) -> NodeSourceStatus:
        raw_status = str(
            payload.get('status')
            or payload.get('connection_status')
            or payload.get('health')
            or payload.get('result')
            or payload.get('state')
            or ''
        ).strip().lower()

        if payload.get('disabled') is True or raw_status in {'disabled', 'inactive', 'offline', 'expired'}:
            return NodeSourceStatus.disabled
        if payload.get('connected') is True or raw_status in {'connected', 'online', 'active', 'healthy', 'running'}:
            return NodeSourceStatus.active
        return NodeSourceStatus.unknown

    @classmethod
    def _normalize_node_health_status(cls, payload: dict[str, Any], source_status: NodeSourceStatus) -> NodeHealthStatus:
        if source_status == NodeSourceStatus.disabled:
            return NodeHealthStatus.disabled

        raw_status = str(
            payload.get('status')
            or payload.get('connection_status')
            or payload.get('health')
            or payload.get('result')
            or payload.get('state')
            or ''
        ).strip().lower()

        if payload.get('connected') is True or raw_status in {'connected', 'healthy', 'online', 'running'}:
            return NodeHealthStatus.healthy
        if raw_status in {'connecting', 'degraded', 'warning', 'timeout', 'expired'}:
            return NodeHealthStatus.degraded
        if raw_status in {'error', 'failed', 'disconnected', 'unhealthy'}:
            return NodeHealthStatus.unhealthy
        return NodeHealthStatus.unknown

    @staticmethod
    def _extract_node_items(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return []

        for key in ('nodes', 'items', 'results', 'data'):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]

        if payload and any(key in payload for key in ('id', 'name', 'status', 'address', 'host')):
            return [payload]
        return []

    def _parse_node(self, payload: dict[str, Any]) -> MarzbanNodeSnapshot:
        source_node_id = self._normalize_node_id(
            payload.get('id')
            or payload.get('node_id')
            or payload.get('uuid')
            or payload.get('name')
            or payload.get('address')
            or payload.get('host')
        )
        if not source_node_id:
            raise MarzbanAPIError('Marzban node payload does not contain stable node identifier')

        raw_name = (
            payload.get('name')
            or payload.get('title')
            or payload.get('display_name')
            or payload.get('remark')
            or source_node_id
        )
        display_name = str(raw_name).strip() or source_node_id
        code = self._slugify_node_code(str(payload.get('code') or display_name))

        source_status = self._normalize_node_source_status(payload)
        health_status = self._normalize_node_health_status(payload, source_status)

        capabilities_json = payload.get('capabilities')
        if not isinstance(capabilities_json, dict):
            capabilities_json = {}

        api_base_url = self._resolve_public_url(
            payload.get('api_base_url')
            or payload.get('api_url')
            or payload.get('base_url')
            or payload.get('url')
        )
        subscription_base_url = self._resolve_public_url(
            payload.get('subscription_base_url')
            or payload.get('subscription_url')
            or payload.get('sub_url')
        )

        return MarzbanNodeSnapshot(
            source_node_id=source_node_id,
            code=code,
            display_name=display_name,
            source_status=source_status,
            health_status=health_status,
            api_base_url=api_base_url,
            subscription_base_url=subscription_base_url,
            location_code=(str(payload.get('location_code')).strip() if payload.get('location_code') is not None else None),
            provider_name=(str(payload.get('provider_name')).strip() if payload.get('provider_name') is not None else None),
            transport_hint=(str(payload.get('transport_hint') or payload.get('transport')).strip() if (payload.get('transport_hint') or payload.get('transport')) is not None else None),
            capabilities_json=capabilities_json,
            raw=payload,
        )

    async def get_node_settings(self) -> dict[str, Any]:
        response = await self._request('GET', '/api/node/settings')
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    async def get_system_stats(self) -> dict[str, Any]:
        """Сырые stats панели Marzban (FEA-ADMIN-NODE-MONITOR).

        Используется `NodeProbeService` для извлечения `total_user` и
        `users_active` (online). Полей у Marzban много (mem/cpu/bandwidth);
        возвращаем raw payload — фильтрация на стороне consumer'а.
        """
        response = await self._request('GET', '/api/system')
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    async def list_nodes(self) -> list[MarzbanNodeSnapshot]:
        response = await self._request('GET', '/api/nodes')
        payload = response.json()
        items = self._extract_node_items(payload)
        nodes: list[MarzbanNodeSnapshot] = []
        for item in items:
            try:
                nodes.append(self._parse_node(item))
            except Exception as exc:
                node_hint = None
                if isinstance(item, dict):
                    node_hint = item.get('id') or item.get('node_id') or item.get('uuid') or item.get('name')
                logger.warning('Skipping malformed Marzban node payload: %s node_hint=%r', exc, node_hint)
                logger.debug('Malformed Marzban node full payload=%r', item)
        return nodes

    async def sync_node_registry(self, session: AsyncSession) -> NodeRegistrySyncResult:
        repo = NodeRegistryRepository(session)
        remote_nodes = await self.list_nodes()
        synced_source_ids: list[str] = []
        detail_messages: list[str] = []
        source_status_counts: Counter[str] = Counter()
        created_count = 0
        updated_count = 0
        error_count = 0
        now = datetime.now(timezone.utc)

        for node in remote_nodes:
            source_status_value = str(getattr(getattr(node, 'source_status', None), 'value', getattr(node, 'source_status', 'unknown')) or 'unknown')
            source_status_counts[source_status_value] += 1
            try:
                existing = await repo.get_by_source_node_id(node.source_node_id)
                if existing is None:
                    existing = await repo.get_by_code(node.code)

                row = await repo.sync_from_source(
                    source_node_id=node.source_node_id,
                    source_status=node.source_status,
                    synced_at=now,
                    source_payload_json=node.raw,
                    suggested_code=node.code,
                    suggested_display_name=node.display_name,
                    suggested_api_base_url=node.api_base_url,
                    suggested_subscription_base_url=node.subscription_base_url,
                    suggested_location_code=node.location_code,
                    suggested_provider_name=node.provider_name,
                    suggested_transport_hint=node.transport_hint,
                    suggested_capabilities_json=node.capabilities_json,
                )
                if row.health_status == NodeHealthStatus.unknown and node.health_status != NodeHealthStatus.unknown:
                    await repo.set_health(row, health_status=node.health_status, checked_at=now)
                synced_source_ids.append(node.source_node_id)
                if existing is None:
                    created_count += 1
                else:
                    updated_count += 1
            except Exception as exc:
                error_count += 1
                detail_messages.append(f'{node.display_name}: {exc}')
                logger.warning('Failed to sync Marzban node %s: %s', node.source_node_id, exc)

        missing_rows = await repo.mark_missing_by_source_ids(set(synced_source_ids), synced_at=now)
        missing_count = len(missing_rows)

        return NodeRegistrySyncResult(
            fetched_count=len(remote_nodes),
            created_count=created_count,
            updated_count=updated_count,
            missing_count=missing_count,
            error_count=error_count,
            synced_source_ids=synced_source_ids,
            detail_messages=detail_messages,
            source_status_counts=dict(source_status_counts),
        )

    @staticmethod
    def xray_restart_warning_text() -> str:
        return (
            'Применение routing/Xray-конфигурации приведёт к перезапуску Xray. '
            'Перед apply убедитесь, что активные пользователи и администратор готовы к кратковременному рестарту.'
        )

    @staticmethod
    def _node_attr(node: Any, name: str, default: Any = None) -> Any:
        if node is None:
            return default
        if isinstance(node, dict):
            return node.get(name, default)
        return getattr(node, name, default)

    @staticmethod
    def _merge_named_object_lists(base_list: list[Any], patch_list: list[Any]) -> list[Any]:
        if not isinstance(base_list, list):
            base_list = []
        if not isinstance(patch_list, list):
            patch_list = []

        def _key(item: Any) -> tuple[str, str] | None:
            if not isinstance(item, dict):
                return None
            for field in ('tag', 'name'):
                value = item.get(field)
                if value is not None and str(value).strip():
                    return (field, str(value).strip())
            return None

        merged = [copy.deepcopy(item) for item in base_list]
        index_map: dict[tuple[str, str], int] = {}
        for index, item in enumerate(merged):
            key = _key(item)
            if key is not None:
                index_map[key] = index

        for patch_item in patch_list:
            patch_copy = copy.deepcopy(patch_item)
            key = _key(patch_copy)
            if key is None or key not in index_map:
                merged.append(patch_copy)
                if key is not None:
                    index_map[key] = len(merged) - 1
                continue
            merged[index_map[key]] = patch_copy

        return merged

    @classmethod
    def _deep_merge_xray_config(cls, base: Any, patch: Any) -> Any:
        if isinstance(base, dict) and isinstance(patch, dict):
            merged = {key: copy.deepcopy(value) for key, value in base.items()}
            for key, value in patch.items():
                if key in merged:
                    merged[key] = cls._deep_merge_xray_config(merged[key], value)
                else:
                    merged[key] = copy.deepcopy(value)
            return merged

        if isinstance(base, list) and isinstance(patch, list):
            if all(isinstance(item, dict) for item in base + patch):
                return cls._merge_named_object_lists(base, patch)
            return [copy.deepcopy(item) for item in patch]

        return copy.deepcopy(patch)

    @staticmethod
    def _ensure_json_object(value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, dict):
            return copy.deepcopy(value)
        if isinstance(value, (str, Path)):
            raw = Path(value).read_text(encoding='utf-8') if isinstance(value, Path) or Path(str(value)).exists() else str(value)
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise MarzbanAPIError('XRAY config должен быть JSON-объектом верхнего уровня')
            return payload
        raise MarzbanAPIError('Неподдерживаемый формат XRAY config source')

    @staticmethod
    def _normalize_command(value: str | list[str] | tuple[str, ...] | None, *, config_path: str | None = None) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            parts = shlex.split(value)
        else:
            parts = [str(item) for item in value if str(item).strip()]
        if not parts:
            return []

        normalized: list[str] = []
        for part in parts:
            if config_path is not None:
                normalized.append(part.replace('{config_path}', config_path))
            else:
                normalized.append(part)
        return normalized

    def _build_xray_rule(self, profile_config: dict[str, Any], *, selected_node: Any | None = None) -> dict[str, Any] | None:
        overrides = profile_config.get('xray_rule_overrides')
        if overrides is None:
            overrides = {}
        if not isinstance(overrides, dict):
            raise MarzbanAPIError('xray_rule_overrides должен быть JSON-объектом')

        preferred_outbound = str(profile_config.get('xray_outbound_tag') or '').strip() or None
        preferred_balancer = str(profile_config.get('xray_balancer_tag') or '').strip() or None

        node_code = self._node_attr(selected_node, 'code')
        node_source_id = self._node_attr(selected_node, 'source_node_id')
        outbound_tag = preferred_outbound or (str(node_code).strip() if node_code is not None else None)
        balancer_tag = preferred_balancer

        rule: dict[str, Any] = {'type': 'field'}
        if balancer_tag:
            rule['balancerTag'] = balancer_tag
        elif outbound_tag:
            rule['outboundTag'] = outbound_tag

        domains = profile_config.get('xray_domain_match') or []
        if isinstance(domains, str):
            domains = [domains]
        domains = [str(item).strip() for item in domains if str(item).strip()]
        if domains:
            rule['domain'] = domains

        ips = profile_config.get('xray_ip_match') or []
        if isinstance(ips, str):
            ips = [ips]
        ips = [str(item).strip() for item in ips if str(item).strip()]
        if ips:
            rule['ip'] = ips

        protocols = profile_config.get('xray_rule_protocols') or []
        if isinstance(protocols, str):
            protocols = [protocols]
        protocols = [str(item).strip() for item in protocols if str(item).strip()]
        if protocols:
            rule['protocol'] = protocols

        networks = profile_config.get('xray_rule_networks') or []
        if isinstance(networks, str):
            networks = [networks]
        networks = [str(item).strip() for item in networks if str(item).strip()]
        if networks:
            rule['network'] = ','.join(networks)

        for key, value in overrides.items():
            if key in {'type', 'outboundTag', 'balancerTag'}:
                continue
            rule[key] = copy.deepcopy(value)

        if len(rule) == 1 and node_source_id is None:
            return None
        return rule

    def build_xray_fragment_preview(
        self,
        profile_config: dict[str, Any] | None,
        *,
        selected_node: Any | None = None,
    ) -> XrayRoutingFragmentPreview:
        config = copy.deepcopy(profile_config or {})
        fragment = config.get('xray_fragment')
        if fragment is None:
            fragment = {}
        if not isinstance(fragment, dict):
            raise MarzbanAPIError('xray_fragment должен быть JSON-объектом')

        rule = self._build_xray_rule(config, selected_node=selected_node)
        patch = copy.deepcopy(fragment)
        if rule is not None:
            routing_section = patch.setdefault('routing', {})
            if not isinstance(routing_section, dict):
                raise MarzbanAPIError('xray_fragment.routing должен быть JSON-объектом')
            rules = routing_section.setdefault('rules', [])
            if not isinstance(rules, list):
                raise MarzbanAPIError('xray_fragment.routing.rules должен быть JSON-массивом')
            rules.append(rule)

        selected_node_code = None
        if selected_node is not None:
            selected_node_code = self._node_attr(selected_node, 'code')
            if selected_node_code is not None:
                selected_node_code = str(selected_node_code).strip() or None
        selected_node_id = None
        if selected_node is not None:
            selected_node_id = self._node_attr(selected_node, 'source_node_id')
            if selected_node_id is None:
                selected_node_id = self._node_attr(selected_node, 'id')
            if selected_node_id is not None:
                selected_node_id = str(selected_node_id).strip() or None

        outbound_tag = None
        balancer_tag = None
        if rule is not None:
            outbound_tag = rule.get('outboundTag')
            balancer_tag = rule.get('balancerTag')

        return XrayRoutingFragmentPreview(
            selected_node_code=selected_node_code,
            selected_node_id=selected_node_id,
            outbound_tag=str(outbound_tag).strip() if outbound_tag is not None and str(outbound_tag).strip() else None,
            balancer_tag=str(balancer_tag).strip() if balancer_tag is not None and str(balancer_tag).strip() else None,
            fragment_patch=patch,
            routing_rule=rule,
            restart_required=True,
            restart_warning=self.xray_restart_warning_text(),
        )

    def build_xray_apply_preview(
        self,
        *,
        base_config: dict[str, Any] | str | Path,
        profile_config: dict[str, Any] | None,
        selected_node: Any | None = None,
        target_config_path: str | Path | None = None,
        xray_test_command: str | list[str] | tuple[str, ...] | None = None,
        restart_command: str | list[str] | tuple[str, ...] | None = None,
    ) -> XrayConfigApplyPreview:
        base_payload = self._ensure_json_object(base_config)
        fragment_preview = self.build_xray_fragment_preview(profile_config, selected_node=selected_node)
        merged_config = self._deep_merge_xray_config(base_payload, fragment_preview.fragment_patch)

        target_path_str = str(target_config_path) if target_config_path is not None else None
        validation_command = self._normalize_command(
            xray_test_command or getattr(self.settings, 'xray_test_command', None),
            config_path=target_path_str or '{config_path}',
        )
        restart_cmd = self._normalize_command(
            restart_command or getattr(self.settings, 'xray_restart_command', None),
            config_path=target_path_str or '{config_path}',
        )

        backup_path = None
        if target_path_str:
            backup_path = f'{target_path_str}.bak'

        return XrayConfigApplyPreview(
            target_config_path=target_path_str,
            backup_path=backup_path,
            validation_command=validation_command,
            restart_command=restart_cmd,
            merged_config=merged_config,
            fragment_preview=fragment_preview,
            restart_required=True,
            restart_warning=self.xray_restart_warning_text(),
        )

    async def validate_xray_config(
        self,
        config: dict[str, Any] | str | Path,
        *,
        xray_test_command: str | list[str] | tuple[str, ...] | None = None,
        timeout_seconds: float = 30.0,
    ) -> tuple[bool, list[str], str, str]:
        payload = self._ensure_json_object(config)
        command_value = xray_test_command or getattr(self.settings, 'xray_test_command', None)

        def _write_tmp() -> str:
            with tempfile.NamedTemporaryFile('w', encoding='utf-8', suffix='.json', delete=False) as tmp:
                json.dump(payload, tmp, ensure_ascii=False, indent=2)
                tmp.flush()
                return tmp.name

        tmp_path = await asyncio.to_thread(_write_tmp)

        try:
            command = self._normalize_command(command_value, config_path=tmp_path)
            if not command:
                return True, [], '', ''

            try:
                proc = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except (OSError, FileNotFoundError) as exc:
                logger.exception('Failed to spawn XRAY validation command: %s', command)
                return False, command, '', f'failed to spawn: {exc}'

            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_seconds
                )
            except asyncio.TimeoutError:
                logger.error('XRAY validation timed out after %ss; killing process', timeout_seconds)
                proc.kill()
                with suppress(BaseException):
                    await proc.wait()
                return False, command, '', f'validation timed out after {timeout_seconds}s'

            return (
                proc.returncode == 0,
                command,
                stdout_b.decode(errors='replace') if stdout_b else '',
                stderr_b.decode(errors='replace') if stderr_b else '',
            )
        finally:
            def _unlink() -> None:
                try:
                    Path(tmp_path).unlink(missing_ok=True)
                except Exception:
                    logger.debug('Failed to remove temporary XRAY config %s', tmp_path, exc_info=True)
            await asyncio.to_thread(_unlink)

    @staticmethod
    def _write_json_atomic(target_path: Path, payload: dict[str, Any]) -> None:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', suffix='.json', dir=str(target_path.parent), delete=False) as tmp:
            json.dump(payload, tmp, ensure_ascii=False, indent=2)
            tmp.flush()
            temp_path = Path(tmp.name)

        temp_path.replace(target_path)

    async def _write_json_atomic_async(self, target_path: Path, payload: dict[str, Any]) -> None:
        await asyncio.to_thread(self._write_json_atomic, target_path, payload)

    async def _rollback_xray_apply(self, target_path: Path, backup_path: Path | None) -> None:
        """Restore previous XRAY config from backup, or remove the freshly written file.

        Best-effort: never raises. Used after failed restart.
        """
        def _do() -> None:
            try:
                if backup_path is not None and backup_path.exists():
                    self._write_json_atomic(target_path, self._ensure_json_object(backup_path))
                else:
                    target_path.unlink(missing_ok=True)
            except Exception:
                logger.warning(
                    'Rollback of XRAY config at %s failed (backup=%s)',
                    target_path,
                    backup_path,
                    exc_info=True,
                )

        await asyncio.to_thread(_do)

    async def apply_xray_config_patch(
        self,
        *,
        base_config: dict[str, Any] | str | Path,
        target_config_path: str | Path,
        profile_config: dict[str, Any] | None,
        selected_node: Any | None = None,
        xray_test_command: str | list[str] | tuple[str, ...] | None = None,
        restart_command: str | list[str] | tuple[str, ...] | None = None,
        validate_timeout_seconds: float = 30.0,
        restart_timeout_seconds: float = 120.0,
    ) -> XrayConfigApplyResult:
        target_path = Path(target_config_path)
        preview = self.build_xray_apply_preview(
            base_config=base_config,
            profile_config=profile_config,
            selected_node=selected_node,
            target_config_path=target_path,
            xray_test_command=xray_test_command,
            restart_command=restart_command,
        )

        is_valid, validation_command, validation_stdout, validation_stderr = await self.validate_xray_config(
            preview.merged_config,
            xray_test_command=xray_test_command,
            timeout_seconds=validate_timeout_seconds,
        )
        if not is_valid:
            raise MarzbanAPIError(
                'XRAY config validation failed before apply: '
                + (validation_stderr.strip() or validation_stdout.strip() or 'unknown validation error')
            )

        backup_path: Path | None = None
        target_exists = await asyncio.to_thread(target_path.exists)
        if target_exists:
            timestamp = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
            backup_path = target_path.with_name(f'{target_path.name}.bak.{timestamp}')

            def _make_backup() -> None:
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                backup_path.write_bytes(target_path.read_bytes())

            await asyncio.to_thread(_make_backup)

        await self._write_json_atomic_async(target_path, preview.merged_config)

        restart_stdout = ''
        restart_stderr = ''
        restart_cmd = self._normalize_command(
            restart_command or getattr(self.settings, 'xray_restart_command', None),
            config_path=str(target_path),
        )
        if restart_cmd:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *restart_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except (OSError, FileNotFoundError) as exc:
                await self._rollback_xray_apply(target_path, backup_path)
                raise MarzbanAPIError(
                    f'XRAY restart command failed to spawn: {exc}'
                ) from exc

            timed_out = False
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=restart_timeout_seconds
                )
            except asyncio.TimeoutError:
                timed_out = True
                proc.kill()
                with suppress(BaseException):
                    await proc.wait()
                stdout_b = b''
                stderr_b = f'restart timed out after {restart_timeout_seconds}s'.encode()

            restart_stdout = stdout_b.decode(errors='replace') if stdout_b else ''
            restart_stderr = stderr_b.decode(errors='replace') if stderr_b else ''
            if timed_out or proc.returncode != 0:
                await self._rollback_xray_apply(target_path, backup_path)
                raise MarzbanAPIError(
                    'XRAY restart failed after config apply: '
                    + (restart_stderr.strip() or restart_stdout.strip() or 'unknown restart error')
                )

        return XrayConfigApplyResult(
            target_config_path=str(target_path),
            backup_path=str(backup_path) if backup_path is not None else None,
            applied=True,
            restart_performed=bool(restart_cmd),
            rollback_performed=False,
            validation_command=validation_command,
            restart_command=restart_cmd,
            validation_stdout=validation_stdout,
            validation_stderr=validation_stderr,
            restart_stdout=restart_stdout,
            restart_stderr=restart_stderr,
        )

    async def get_user(self, username: str) -> MarzbanUser:
        response = await self._request('GET', f'/api/user/{username}')
        return self._parse_user(response.json())

    async def create_user(self, payload: dict[str, Any]) -> MarzbanUser:
        try:
            response = await self._request('POST', '/api/user', json=payload)
        except MarzbanAPIError as exc:
            if exc.status_code == 409:
                raise UserAlreadyExistsError(str(exc), status_code=exc.status_code) from exc
            logger.error('Marzban create_user error', exc_info=exc)
            raise
        return self._parse_user(response.json())

    async def modify_user(
        self,
        username: str,
        payload: dict[str, Any] | None = None,
        *,
        is_trial: bool = False,
        **kwargs: Any,
    ) -> MarzbanUser:
        """Safely update a user while preserving existing proxies/inbounds/state."""
        current_user = await self.get_user(username)
        raw = dict(current_user.raw)

        safe_payload: dict[str, Any] = {
            'proxies': raw.get('proxies', {}),
            'inbounds': raw.get('inbounds', {}),
            'status': raw.get('status', 'active'),
            'data_limit': raw.get('data_limit', 0),
            'expire': raw.get('expire', 0),
        }

        if self.settings.marzban_online_limit_field and raw.get(self.settings.marzban_online_limit_field) is not None:
            safe_payload[self.settings.marzban_online_limit_field] = raw.get(self.settings.marzban_online_limit_field)

        if raw.get('note') is not None:
            safe_payload['note'] = raw.get('note')

        if payload:
            safe_payload.update(payload)
        if kwargs:
            safe_payload.update(kwargs)

        safe_payload['data_limit_reset_strategy'] = self._resolve_reset_strategy(is_trial=is_trial)

        response = await self._request('PUT', f'/api/user/{username}', json=safe_payload)
        return self._parse_user(response.json())

    async def safe_modify_user(self, username: str, *, is_trial: bool = False, **kwargs: Any) -> MarzbanUser:
        """Compatibility alias for a safe point-in-time update preserving proxies/inbounds."""
        return await self.modify_user(username, is_trial=is_trial, **kwargs)

    async def reset_user_usage(self, username: str) -> MarzbanUser:
        response = await self._request('POST', f'/api/user/{username}/reset')
        return self._parse_user(response.json())

    async def revoke_subscription_url(self, username: str) -> MarzbanUser:
        """Перевыпустить subscription URL (Marzban POST /api/user/{u}/revoke_sub).

        Используется FEA-ADMIN-SUB-CRM #3 «Re-issue URL» — старая ссылка
        перестаёт быть валидной, пользователь получает новую (саппорт
        пересылает). Marzban перегенерирует токен и возвращает обновлённого
        user'а с новым `subscription_url`.
        """
        response = await self._request('POST', f'/api/user/{username}/revoke_sub')
        return self._parse_user(response.json())

    async def reset_user_traffic(self, username: str) -> MarzbanUser:
        """Compatibility alias for resetting used traffic to zero."""
        return await self.reset_user_usage(username)

    async def renew_subscription(
        self,
        username: str,
        months: int,
        tariff_limit_gb: int | None,
        *,
        online_limit: int | None = None,
        note: str | None = None,
        status: str = 'active',
    ) -> MarzbanUser:
        """
        Renew a subscription without burning remaining days:
        - if active, extend from the current expire timestamp
        - if expired, start from now
        - reset used traffic for the new billing period
        """
        current_user = await self.get_user(username)
        now_dt = datetime.now(timezone.utc)

        base_dt = current_user.expire_datetime if current_user.expire_datetime and current_user.expire_datetime > now_dt else now_dt
        new_expire_dt = base_dt + relativedelta(months=months)
        new_expire = int(new_expire_dt.timestamp())

        await self.reset_user_traffic(username)

        data_limit = 0 if not tariff_limit_gb else int(tariff_limit_gb) * (1024 ** 3)
        payload: dict[str, Any] = {
            'data_limit': data_limit,
            'expire': new_expire,
            'status': status,
        }
        if online_limit is not None and self.settings.marzban_online_limit_field:
            payload[self.settings.marzban_online_limit_field] = online_limit
        if note is not None:
            payload['note'] = note
        return await self.safe_modify_user(username, is_trial=False, **payload)

    async def topup_traffic(self, username: str, extra_gb: int) -> MarzbanUser:
        """Increase traffic limit only, without touching expiry."""
        current_user = await self.get_user(username)
        current_limit = current_user.data_limit

        if current_limit in (None, 0):
            raise ValueError('Для безлимитного тарифа докупка трафика не требуется и недоступна.')

        new_limit = int(current_limit) + int(extra_gb) * (1024 ** 3)
        return await self.safe_modify_user(username, data_limit=new_limit, status='active', is_trial=False)

    async def set_online_limit(self, username: str, online_limit: int | None) -> MarzbanUser | None:
        """Update only the online-limit field (mid-cycle device topup, FEA-A9).

        Returns None if the Marzban deployment doesn't expose an
        online-limit field (`marzban_online_limit_field` пуст) — каллер
        в этом случае ограничивается локальным апдейтом.
        """
        field = self.settings.marzban_online_limit_field
        if not field:
            return None
        return await self.safe_modify_user(
            username,
            is_trial=False,
            **{field: online_limit},
        )

    def _default_create_payload(
        self,
        *,
        username: str,
        expire_at: datetime | None,
        data_limit_bytes: int | None,
        online_limit: int | None,
        note: str | None,
        status: str | None = None,
        is_trial: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'username': username,
            'status': status or self.settings.marzban_status_on_create,
            'proxies': {'vless': {}},
            'inbounds': {'vless': self.settings.marzban_vless_inbounds},
            'expire': int(expire_at.timestamp()) if expire_at else 0,
            'data_limit': 0 if data_limit_bytes is None else data_limit_bytes,
            'data_limit_reset_strategy': self._resolve_reset_strategy(is_trial=is_trial),
            'note': note or '',
        }
        if online_limit is not None and self.settings.marzban_online_limit_field:
            payload[self.settings.marzban_online_limit_field] = online_limit
        return payload

    def _build_safe_update_payload(
        self,
        remote: MarzbanUser,
        *,
        expire_at: datetime | None,
        data_limit_bytes: int | None,
        online_limit: int | None,
        note: str | None,
        status: str | None,
        is_trial: bool = False,
    ) -> dict[str, Any]:
        raw = dict(remote.raw)
        payload: dict[str, Any] = {
            'username': raw.get('username', remote.username),
            'status': status or raw.get('status') or self.settings.marzban_status_on_create,
            'proxies': raw.get('proxies') or {'vless': {}},
            'inbounds': raw.get('inbounds') or {'vless': self.settings.marzban_vless_inbounds},
            'expire': int(expire_at.timestamp()) if expire_at else 0,
            'data_limit': 0 if data_limit_bytes is None else data_limit_bytes,
            'data_limit_reset_strategy': self._resolve_reset_strategy(is_trial=is_trial),
            'note': note if note is not None else raw.get('note', ''),
        }
        if self.settings.marzban_online_limit_field:
            current_limit = raw.get(self.settings.marzban_online_limit_field)
            payload[self.settings.marzban_online_limit_field] = online_limit if online_limit is not None else current_limit
        return payload

    async def create_or_update_vless_user(
        self,
        *,
        username: str,
        expire_at: datetime | None,
        data_limit_bytes: int | None,
        online_limit: int | None,
        note: str | None = None,
        reset_traffic: bool = False,
        status: str | None = None,
        is_trial: bool = False,
    ) -> MarzbanUser:
        try:
            remote = await self.get_user(username)
        except MarzbanAPIError as exc:
            if exc.status_code != 404:
                logger.error('Marzban get_user error during upsert', exc_info=exc)
                raise
            remote = None

        if remote is None:
            payload = self._default_create_payload(
                username=username,
                expire_at=expire_at,
                data_limit_bytes=data_limit_bytes,
                online_limit=online_limit,
                note=note,
                status=status,
                is_trial=is_trial,
            )
            try:
                user = await self.create_user(payload)
            except UserAlreadyExistsError:
                remote = await self.get_user(username)
                payload = self._build_safe_update_payload(
                    remote,
                    expire_at=expire_at,
                    data_limit_bytes=data_limit_bytes,
                    online_limit=online_limit,
                    note=note,
                    status=status,
                    is_trial=is_trial,
                )
                user = await self.modify_user(username, payload, is_trial=is_trial)
        else:
            payload = self._build_safe_update_payload(
                remote,
                expire_at=expire_at,
                data_limit_bytes=data_limit_bytes,
                online_limit=online_limit,
                note=note,
                status=status,
                is_trial=is_trial,
            )
            user = await self.modify_user(username, payload, is_trial=is_trial)

        if reset_traffic:
            user = await self.reset_user_usage(username)
        return user

    async def add_traffic(self, username: str, extra_bytes: int) -> MarzbanUser:
        current = await self.get_user(username)
        current_limit = current.data_limit

        if current_limit in (None, 0):
            raise ValueError('Для безлимитного тарифа докупка трафика не требуется и недоступна.')

        payload = self._build_safe_update_payload(
            current,
            expire_at=current.expire_datetime,
            data_limit_bytes=int(current_limit) + extra_bytes,
            online_limit=current.raw.get(self.settings.marzban_online_limit_field) if self.settings.marzban_online_limit_field else None,
            note=current.raw.get('note'),
            status=current.status,
            is_trial=False,
        )
        return await self.modify_user(username, payload, is_trial=False)
