from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    NodeHealthStatus,
    NodeRegistry,
    NodeSourceStatus,
    NodeSyncState,
)



def _normalize_optional_str(value: str | None) -> str | None:
    normalized = (value or '').strip()
    return normalized or None



def _normalize_base_url(value: str | None) -> str | None:
    normalized = _normalize_optional_str(value)
    if normalized is None:
        return None
    return normalized.rstrip('/')



def _validate_public_or_internal_url(value: str | None, *, field_name: str) -> str | None:
    normalized = _normalize_base_url(value)
    if normalized is None:
        return None

    parts = urlparse(normalized)
    if not parts.scheme or not parts.netloc:
        raise ValueError(f'{field_name} должен быть полным URL')
    if parts.scheme not in {'http', 'https'}:
        raise ValueError(f'{field_name} должен начинаться с http:// или https://')
    return normalized



def _normalize_policy_tags(value: list[str] | tuple[str, ...] | set[str] | None) -> list[str]:
    if not value:
        return []

    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        normalized = (str(item or '').strip()).lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result



def _normalize_capabilities(value: dict[str, Any] | None) -> dict[str, Any]:
    if not value:
        return {}
    return dict(value)



def _normalize_source_payload(value: dict[str, Any] | None) -> dict[str, Any]:
    if not value:
        return {}
    return dict(value)



def _normalize_utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)



class NodeRegistryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _normalize_health_status(value: NodeHealthStatus | str | None) -> NodeHealthStatus:
        if isinstance(value, NodeHealthStatus):
            return value
        normalized = (str(value or '').strip().lower()) or NodeHealthStatus.unknown.value
        try:
            return NodeHealthStatus(normalized)
        except ValueError as exc:
            raise ValueError(f'Некорректный health_status: {value}') from exc

    @staticmethod
    def _normalize_source_status(value: NodeSourceStatus | str | None) -> NodeSourceStatus:
        if isinstance(value, NodeSourceStatus):
            return value
        normalized = (str(value or '').strip().lower()) or NodeSourceStatus.unknown.value
        try:
            return NodeSourceStatus(normalized)
        except ValueError as exc:
            raise ValueError(f'Некорректный source_status: {value}') from exc

    @staticmethod
    def _normalize_sync_state(value: NodeSyncState | str | None) -> NodeSyncState:
        if isinstance(value, NodeSyncState):
            return value
        normalized = (str(value or '').strip().lower()) or NodeSyncState.never_synced.value
        try:
            return NodeSyncState(normalized)
        except ValueError as exc:
            raise ValueError(f'Некорректный sync_state: {value}') from exc

    @staticmethod
    def _normalize_bool(value: bool | None, *, default: bool) -> bool:
        if value is None:
            return default
        return bool(value)

    @staticmethod
    def _normalize_source_node_id(value: str | None) -> str | None:
        normalized = (value or '').strip()
        return normalized or None

    @staticmethod
    def _derive_fallback_code(*, suggested_code: str | None, source_node_id: str | None, display_name: str | None) -> str:
        raw = (suggested_code or '').strip().lower()
        if raw:
            return raw

        raw = (source_node_id or '').strip().lower().replace(' ', '_')
        if raw:
            return raw

        raw = (display_name or '').strip().lower().replace(' ', '_')
        if raw:
            return raw

        raise ValueError('Не удалось определить code для node')

    async def get_by_id(self, node_id: int) -> NodeRegistry | None:
        res = await self.session.execute(select(NodeRegistry).where(NodeRegistry.id == node_id))
        return res.scalar_one_or_none()

    async def get_by_id_for_update(self, node_id: int) -> NodeRegistry | None:
        res = await self.session.execute(
            select(NodeRegistry)
            .where(NodeRegistry.id == node_id)
            .with_for_update()
        )
        return res.scalar_one_or_none()

    async def get_by_code(self, code: str) -> NodeRegistry | None:
        normalized_code = (code or '').strip().lower()
        if not normalized_code:
            return None
        res = await self.session.execute(select(NodeRegistry).where(func.lower(NodeRegistry.code) == normalized_code))
        return res.scalar_one_or_none()

    async def get_by_code_for_update(self, code: str) -> NodeRegistry | None:
        normalized_code = (code or '').strip().lower()
        if not normalized_code:
            return None
        res = await self.session.execute(
            select(NodeRegistry)
            .where(func.lower(NodeRegistry.code) == normalized_code)
            .with_for_update()
        )
        return res.scalar_one_or_none()

    async def get_by_source_node_id(self, source_node_id: str) -> NodeRegistry | None:
        normalized = self._normalize_source_node_id(source_node_id)
        if normalized is None:
            return None
        res = await self.session.execute(
            select(NodeRegistry).where(NodeRegistry.source_node_id == normalized)
        )
        return res.scalar_one_or_none()

    async def get_by_source_node_id_for_update(self, source_node_id: str) -> NodeRegistry | None:
        normalized = self._normalize_source_node_id(source_node_id)
        if normalized is None:
            return None
        res = await self.session.execute(
            select(NodeRegistry)
            .where(NodeRegistry.source_node_id == normalized)
            .with_for_update()
        )
        return res.scalar_one_or_none()

    async def list_all(self) -> list[NodeRegistry]:
        res = await self.session.execute(
            select(NodeRegistry)
            .order_by(NodeRegistry.sort_order.asc(), NodeRegistry.priority.asc(), NodeRegistry.id.asc())
        )
        return list(res.scalars().all())

    async def list_enabled(self) -> list[NodeRegistry]:
        res = await self.session.execute(
            select(NodeRegistry)
            .where(NodeRegistry.is_enabled.is_(True))
            .order_by(NodeRegistry.sort_order.asc(), NodeRegistry.priority.asc(), NodeRegistry.id.asc())
        )
        return list(res.scalars().all())

    async def list_routable(self) -> list[NodeRegistry]:
        res = await self.session.execute(
            select(NodeRegistry)
            .where(
                NodeRegistry.is_enabled.is_(True),
                NodeRegistry.health_status.in_([NodeHealthStatus.healthy, NodeHealthStatus.degraded]),
            )
            .order_by(
                NodeRegistry.priority.asc(),
                NodeRegistry.weight.desc(),
                NodeRegistry.sort_order.asc(),
                NodeRegistry.id.asc(),
            )
        )
        return list(res.scalars().all())

    async def list_with_sync_attention(self) -> list[NodeRegistry]:
        res = await self.session.execute(
            select(NodeRegistry)
            .where(NodeRegistry.sync_state.in_([
                NodeSyncState.never_synced,
                NodeSyncState.missing,
                NodeSyncState.error,
            ]))
            .order_by(NodeRegistry.sort_order.asc(), NodeRegistry.priority.asc(), NodeRegistry.id.asc())
        )
        return list(res.scalars().all())

    async def list_synced(self) -> list[NodeRegistry]:
        res = await self.session.execute(
            select(NodeRegistry)
            .where(NodeRegistry.source_node_id.is_not(None))
            .order_by(NodeRegistry.sort_order.asc(), NodeRegistry.priority.asc(), NodeRegistry.id.asc())
        )
        return list(res.scalars().all())

    async def get_default(self) -> NodeRegistry | None:
        res = await self.session.execute(
            select(NodeRegistry)
            .where(NodeRegistry.is_default.is_(True))
            .order_by(NodeRegistry.id.asc())
            .limit(1)
        )
        return res.scalar_one_or_none()

    async def get_default_node(self) -> NodeRegistry | None:
        return await self.get_default()

    async def create(
        self,
        *,
        code: str,
        display_name: str,
        source_node_id: str | None = None,
        source_status: NodeSourceStatus | str = NodeSourceStatus.unknown,
        sync_state: NodeSyncState | str = NodeSyncState.never_synced,
        source_payload_json: dict[str, Any] | None = None,
        last_sync_at: datetime | None = None,
        sync_error: str | None = None,
        api_base_url: str | None = None,
        subscription_base_url: str | None = None,
        location_code: str | None = None,
        provider_name: str | None = None,
        transport_hint: str | None = None,
        policy_tags: list[str] | tuple[str, ...] | set[str] | None = None,
        capabilities_json: dict[str, Any] | None = None,
        is_enabled: bool = True,
        is_default: bool = False,
        priority: int = 100,
        weight: int = 100,
        sort_order: int = 100,
        health_status: NodeHealthStatus | str = NodeHealthStatus.unknown,
        last_healthcheck_at: datetime | None = None,
        last_health_error: str | None = None,
        notes: str | None = None,
    ) -> NodeRegistry:
        normalized_code = (code or '').strip().lower()
        if not normalized_code:
            raise ValueError('code не может быть пустым')

        normalized_display_name = (display_name or '').strip()
        if not normalized_display_name:
            raise ValueError('display_name не может быть пустым')

        if is_default:
            await self.clear_default()

        row = NodeRegistry(
            code=normalized_code,
            display_name=normalized_display_name,
            source_node_id=self._normalize_source_node_id(source_node_id),
            source_status=self._normalize_source_status(source_status),
            sync_state=self._normalize_sync_state(sync_state),
            source_payload_json=_normalize_source_payload(source_payload_json),
            last_sync_at=_normalize_utc_datetime(last_sync_at),
            sync_error=_normalize_optional_str(sync_error),
            api_base_url=_validate_public_or_internal_url(api_base_url, field_name='api_base_url'),
            subscription_base_url=_validate_public_or_internal_url(
                subscription_base_url,
                field_name='subscription_base_url',
            ),
            location_code=_normalize_optional_str(location_code),
            provider_name=_normalize_optional_str(provider_name),
            transport_hint=_normalize_optional_str(transport_hint),
            policy_tags=_normalize_policy_tags(policy_tags),
            capabilities_json=_normalize_capabilities(capabilities_json),
            is_enabled=self._normalize_bool(is_enabled, default=True),
            is_default=self._normalize_bool(is_default, default=False),
            priority=max(0, int(priority)),
            weight=max(0, int(weight)),
            sort_order=max(0, int(sort_order)),
            health_status=self._normalize_health_status(health_status),
            last_healthcheck_at=_normalize_utc_datetime(last_healthcheck_at),
            last_health_error=_normalize_optional_str(last_health_error),
            notes=_normalize_optional_str(notes),
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def update(
        self,
        row: NodeRegistry,
        *,
        display_name: str | None = None,
        api_base_url: str | None = None,
        subscription_base_url: str | None = None,
        location_code: str | None = None,
        provider_name: str | None = None,
        transport_hint: str | None = None,
        policy_tags: list[str] | tuple[str, ...] | set[str] | None = None,
        capabilities_json: dict[str, Any] | None = None,
        is_enabled: bool | None = None,
        is_default: bool | None = None,
        priority: int | None = None,
        weight: int | None = None,
        sort_order: int | None = None,
        health_status: NodeHealthStatus | str | None = None,
        last_healthcheck_at: datetime | None = None,
        last_health_error: str | None = None,
        notes: str | None = None,
    ) -> NodeRegistry:
        if display_name is not None:
            normalized_display_name = (display_name or '').strip()
            if not normalized_display_name:
                raise ValueError('display_name не может быть пустым')
            row.display_name = normalized_display_name

        if api_base_url is not None:
            row.api_base_url = _validate_public_or_internal_url(api_base_url, field_name='api_base_url')

        if subscription_base_url is not None:
            row.subscription_base_url = _validate_public_or_internal_url(
                subscription_base_url,
                field_name='subscription_base_url',
            )

        if location_code is not None:
            row.location_code = _normalize_optional_str(location_code)

        if provider_name is not None:
            row.provider_name = _normalize_optional_str(provider_name)

        if transport_hint is not None:
            row.transport_hint = _normalize_optional_str(transport_hint)

        if policy_tags is not None:
            row.policy_tags = _normalize_policy_tags(policy_tags)

        if capabilities_json is not None:
            row.capabilities_json = _normalize_capabilities(capabilities_json)

        if is_enabled is not None:
            row.is_enabled = bool(is_enabled)
            if not row.is_enabled:
                row.health_status = NodeHealthStatus.disabled

        if is_default is not None:
            if is_default:
                await self.clear_default(except_node_id=row.id)
            row.is_default = bool(is_default)

        if priority is not None:
            row.priority = max(0, int(priority))

        if weight is not None:
            row.weight = max(0, int(weight))

        if sort_order is not None:
            row.sort_order = max(0, int(sort_order))

        if health_status is not None:
            row.health_status = self._normalize_health_status(health_status)

        if last_healthcheck_at is not None:
            row.last_healthcheck_at = _normalize_utc_datetime(last_healthcheck_at)

        if last_health_error is not None:
            row.last_health_error = _normalize_optional_str(last_health_error)

        if notes is not None:
            row.notes = _normalize_optional_str(notes)

        await self.session.flush()
        return row

    async def sync_from_source(
        self,
        *,
        source_node_id: str,
        source_status: NodeSourceStatus | str,
        synced_at: datetime | None = None,
        source_payload_json: dict[str, Any] | None = None,
        suggested_code: str | None = None,
        suggested_display_name: str | None = None,
        suggested_api_base_url: str | None = None,
        suggested_subscription_base_url: str | None = None,
        suggested_location_code: str | None = None,
        suggested_provider_name: str | None = None,
        suggested_transport_hint: str | None = None,
        suggested_capabilities_json: dict[str, Any] | None = None,
    ) -> NodeRegistry:
        normalized_source_node_id = self._normalize_source_node_id(source_node_id)
        if normalized_source_node_id is None:
            raise ValueError('source_node_id не может быть пустым')

        row = await self.get_by_source_node_id_for_update(normalized_source_node_id)
        if row is None and (suggested_code or '').strip():
            candidate = await self.get_by_code_for_update(suggested_code or '')
            if candidate is not None and not (candidate.source_node_id or '').strip():
                row = candidate

        sync_dt = _normalize_utc_datetime(synced_at or datetime.now(timezone.utc))
        normalized_source_status = self._normalize_source_status(source_status)
        normalized_source_payload = _normalize_source_payload(source_payload_json)

        if row is None:
            row = await self.create(
                code=self._derive_fallback_code(
                    suggested_code=suggested_code,
                    source_node_id=normalized_source_node_id,
                    display_name=suggested_display_name,
                ),
                display_name=(suggested_display_name or normalized_source_node_id),
                source_node_id=normalized_source_node_id,
                source_status=normalized_source_status,
                sync_state=NodeSyncState.synced,
                source_payload_json=normalized_source_payload,
                last_sync_at=sync_dt,
                sync_error=None,
                api_base_url=suggested_api_base_url,
                subscription_base_url=suggested_subscription_base_url,
                location_code=suggested_location_code,
                provider_name=suggested_provider_name,
                transport_hint=suggested_transport_hint,
                capabilities_json=suggested_capabilities_json,
            )
            return row

        row.source_node_id = normalized_source_node_id
        row.source_status = normalized_source_status
        row.sync_state = NodeSyncState.synced
        row.source_payload_json = normalized_source_payload
        row.last_sync_at = sync_dt
        row.sync_error = None

        if not (row.display_name or '').strip() and (suggested_display_name or '').strip():
            row.display_name = (suggested_display_name or '').strip()
        if not (row.api_base_url or '').strip() and suggested_api_base_url is not None:
            row.api_base_url = _validate_public_or_internal_url(suggested_api_base_url, field_name='api_base_url')
        if not (row.subscription_base_url or '').strip() and suggested_subscription_base_url is not None:
            row.subscription_base_url = _validate_public_or_internal_url(
                suggested_subscription_base_url,
                field_name='subscription_base_url',
            )
        if not (row.location_code or '').strip() and suggested_location_code is not None:
            row.location_code = _normalize_optional_str(suggested_location_code)
        if not (row.provider_name or '').strip() and suggested_provider_name is not None:
            row.provider_name = _normalize_optional_str(suggested_provider_name)
        if not (row.transport_hint or '').strip() and suggested_transport_hint is not None:
            row.transport_hint = _normalize_optional_str(suggested_transport_hint)
        if not row.capabilities_json and suggested_capabilities_json is not None:
            row.capabilities_json = _normalize_capabilities(suggested_capabilities_json)

        await self.session.flush()
        return row

    async def mark_missing_by_source_ids(
        self,
        present_source_ids: list[str] | tuple[str, ...] | set[str],
        *,
        synced_at: datetime | None = None,
    ) -> list[NodeRegistry]:
        normalized_present = {
            normalized
            for item in present_source_ids
            if (normalized := self._normalize_source_node_id(str(item))) is not None
        }
        res = await self.session.execute(
            select(NodeRegistry)
            .where(NodeRegistry.source_node_id.is_not(None))
            .with_for_update()
        )
        rows = list(res.scalars().all())
        sync_dt = _normalize_utc_datetime(synced_at or datetime.now(timezone.utc))

        changed: list[NodeRegistry] = []
        for row in rows:
            source_node_id = self._normalize_source_node_id(row.source_node_id)
            if source_node_id is None or source_node_id in normalized_present:
                continue
            row.sync_state = NodeSyncState.missing
            row.last_sync_at = sync_dt
            row.sync_error = None
            changed.append(row)

        if changed:
            await self.session.flush()
        return changed

    async def set_sync_error(
        self,
        row: NodeRegistry,
        *,
        error_message: str,
        checked_at: datetime | None = None,
        source_payload_json: dict[str, Any] | None = None,
    ) -> NodeRegistry:
        row.sync_state = NodeSyncState.error
        row.last_sync_at = _normalize_utc_datetime(checked_at or datetime.now(timezone.utc))
        row.sync_error = _normalize_optional_str(error_message)
        if source_payload_json is not None:
            row.source_payload_json = _normalize_source_payload(source_payload_json)
        await self.session.flush()
        return row

    async def touch_sync_state(
        self,
        row: NodeRegistry,
        *,
        sync_state: NodeSyncState | str,
        source_status: NodeSourceStatus | str | None = None,
        checked_at: datetime | None = None,
        sync_error: str | None = None,
        source_payload_json: dict[str, Any] | None = None,
    ) -> NodeRegistry:
        row.sync_state = self._normalize_sync_state(sync_state)
        if source_status is not None:
            row.source_status = self._normalize_source_status(source_status)
        row.last_sync_at = _normalize_utc_datetime(checked_at or datetime.now(timezone.utc))
        row.sync_error = _normalize_optional_str(sync_error)
        if source_payload_json is not None:
            row.source_payload_json = _normalize_source_payload(source_payload_json)
        await self.session.flush()
        return row

    async def set_health(
        self,
        row: NodeRegistry,
        *,
        health_status: NodeHealthStatus | str,
        checked_at: datetime | None = None,
        error_message: str | None = None,
    ) -> NodeRegistry:
        row.health_status = self._normalize_health_status(health_status)
        row.last_healthcheck_at = _normalize_utc_datetime(checked_at or datetime.now(timezone.utc))
        row.last_health_error = _normalize_optional_str(error_message)
        await self.session.flush()
        return row

    async def clear_default(self, *, except_node_id: int | None = None) -> None:
        stmt = select(NodeRegistry).where(NodeRegistry.is_default.is_(True))
        if except_node_id is not None:
            stmt = stmt.where(NodeRegistry.id != except_node_id)
        res = await self.session.execute(stmt)
        for row in res.scalars().all():
            row.is_default = False
        await self.session.flush()
