from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import RoutingProfile
from app.services.node_policy import NodeSelectionRequest


class RoutingProfileError(Exception):
    """Base error for routing profile operations."""


class RoutingProfileValidationError(RoutingProfileError):
    """Raised when routing profile payload is invalid."""


_ALLOWED_STRATEGIES = {'default', 'reserve', 'country', 'premium', 'balancing', 'fallback'}
_ALLOWED_XRAY_RULE_TYPES = {'field'}
_ALLOWED_XRAY_NETWORKS = {'tcp', 'grpc', 'ws', 'httpupgrade', 'splithttp', 'kcp', 'quic'}
_ALLOWED_XRAY_PROTOCOLS = {'http', 'tls', 'bittorrent'}
_ALLOWED_FRAGMENT_ACTIONS = {'insert', 'replace', 'append'}


@dataclass(slots=True)
class RoutingRuntimeConfig:
    strategy: str
    required_tags: list[str]
    preferred_tags: list[str]
    avoid_tags: list[str]
    location_allow: list[str]
    transport_allow: list[str]
    require_subscription_url: bool
    require_api_url: bool
    require_synced_source: bool
    allow_missing_source: bool
    allow_degraded: bool
    premium_only: bool
    reserve_only: bool
    xray_outbound_tag: str | None
    xray_balancer_tag: str | None
    xray_domain_match: list[str]
    xray_ip_match: list[str]
    xray_rule_protocols: list[str]
    xray_rule_networks: list[str]
    xray_extra_rule_fields: dict[str, Any]
    xray_rule_overrides: list[dict[str, Any]]
    xray_fragment: dict[str, Any]
    metadata: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            'strategy': self.strategy,
            'required_tags': list(self.required_tags),
            'preferred_tags': list(self.preferred_tags),
            'avoid_tags': list(self.avoid_tags),
            'location_allow': list(self.location_allow),
            'transport_allow': list(self.transport_allow),
            'require_subscription_url': self.require_subscription_url,
            'require_api_url': self.require_api_url,
            'require_synced_source': self.require_synced_source,
            'allow_missing_source': self.allow_missing_source,
            'allow_degraded': self.allow_degraded,
            'premium_only': self.premium_only,
            'reserve_only': self.reserve_only,
            'xray_outbound_tag': self.xray_outbound_tag,
            'xray_balancer_tag': self.xray_balancer_tag,
            'xray_domain_match': list(self.xray_domain_match),
            'xray_ip_match': list(self.xray_ip_match),
            'xray_rule_protocols': list(self.xray_rule_protocols),
            'xray_rule_networks': list(self.xray_rule_networks),
            'xray_extra_rule_fields': dict(self.xray_extra_rule_fields),
            'xray_rule_overrides': [dict(item) for item in self.xray_rule_overrides],
            'xray_fragment': dict(self.xray_fragment),
            'metadata': dict(self.metadata),
        }


@dataclass(slots=True)
class ResolvedRoutingProfile:
    profile: RoutingProfile | None
    requested_tags: list[str]
    matched_by_default: bool
    runtime_config: RoutingRuntimeConfig | None = None

    @property
    def config(self) -> dict[str, Any]:
        if self.profile is None:
            return {}
        return dict(self.profile.config_json or {})


@dataclass(slots=True)
class RoutingPreview:
    resolved: ResolvedRoutingProfile
    node_request: NodeSelectionRequest
    xray_fragment_preview: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            'profile_id': self.resolved.profile.id if self.resolved.profile is not None else None,
            'profile_code': self.resolved.profile.code if self.resolved.profile is not None else None,
            'matched_by_default': self.resolved.matched_by_default,
            'requested_tags': list(self.resolved.requested_tags),
            'runtime_config': self.resolved.runtime_config.as_dict() if self.resolved.runtime_config else {},
            'node_request': {
                'policy_name': self.node_request.policy_name,
                'required_tags': sorted(self.node_request.required_tags),
                'preferred_tags': sorted(self.node_request.preferred_tags),
                'avoid_tags': sorted(self.node_request.avoid_tags),
                'location_allow': sorted(self.node_request.location_allow),
                'transport_allow': sorted(self.node_request.transport_allow),
                'require_subscription_url': self.node_request.require_subscription_url,
                'require_api_url': self.node_request.require_api_url,
                'require_synced_source': self.node_request.require_synced_source,
                'allow_missing_source': self.node_request.allow_missing_source,
                'metadata': dict(self.node_request.metadata),
            },
            'xray_fragment_preview': dict(self.xray_fragment_preview),
        }


def _normalize_optional_str(value: str | None) -> str | None:
    normalized = (value or '').strip()
    return normalized or None


def _normalize_required_str(value: str | None, *, field_name: str) -> str:
    normalized = (value or '').strip()
    if not normalized:
        raise RoutingProfileValidationError(f'{field_name} не может быть пустым')
    return normalized


def _normalize_code(value: str | None) -> str:
    normalized = _normalize_required_str(value, field_name='code').lower()
    return normalized


def _normalize_match_tags(value: list[str] | tuple[str, ...] | set[str] | str | None) -> list[str]:
    if value is None:
        return []

    if isinstance(value, str):
        raw_items = [part.strip() for part in value.replace(';', ',').split(',')]
    else:
        raw_items = [str(item).strip() for item in value]

    normalized: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        candidate = item.lower()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        normalized.append(candidate)
    return normalized


def _normalize_config_json(value: dict[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RoutingProfileValidationError('config_json должен быть объектом JSON')
    return dict(value)


def _normalize_bool(value: Any, *, field_name: str, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {'1', 'true', 'yes', 'y', 'on'}:
            return True
        if normalized in {'0', 'false', 'no', 'n', 'off'}:
            return False
    raise RoutingProfileValidationError(f'{field_name} должен быть логическим значением')


def _normalize_str_list(value: Any, *, field_name: str) -> list[str]:
    items = _normalize_match_tags(value)
    if not isinstance(items, list):
        raise RoutingProfileValidationError(f'{field_name} должен быть списком строк')
    return items


def _normalize_strategy(value: Any) -> str:
    if value is None:
        return 'default'
    if not isinstance(value, str):
        raise RoutingProfileValidationError('strategy должен быть строкой')
    normalized = value.strip().lower()
    if normalized not in _ALLOWED_STRATEGIES:
        raise RoutingProfileValidationError(
            f"strategy должен быть одним из: {', '.join(sorted(_ALLOWED_STRATEGIES))}"
        )
    return normalized


def _normalize_xray_rule_overrides(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise RoutingProfileValidationError('xray_rule_overrides должен быть массивом JSON-объектов')

    normalized: list[dict[str, Any]] = []
    for idx, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise RoutingProfileValidationError(f'xray_rule_overrides[{idx}] должен быть объектом')
        rule_type = str(item.get('type', 'field')).strip().lower()
        if rule_type not in _ALLOWED_XRAY_RULE_TYPES:
            raise RoutingProfileValidationError(f'xray_rule_overrides[{idx}].type не поддерживается')
        candidate = dict(item)
        candidate['type'] = rule_type
        normalized.append(candidate)
    return normalized


def _normalize_xray_fragment(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RoutingProfileValidationError('xray_fragment должен быть JSON-объектом')

    normalized = dict(value)
    action = normalized.get('action')
    if action is not None:
        if not isinstance(action, str):
            raise RoutingProfileValidationError('xray_fragment.action должен быть строкой')
        action_value = action.strip().lower()
        if action_value not in _ALLOWED_FRAGMENT_ACTIONS:
            raise RoutingProfileValidationError(
                f"xray_fragment.action должен быть одним из: {', '.join(sorted(_ALLOWED_FRAGMENT_ACTIONS))}"
            )
        normalized['action'] = action_value
    else:
        normalized['action'] = 'insert'

    target_path = normalized.get('target_path')
    if target_path is not None and (not isinstance(target_path, str) or not target_path.strip()):
        raise RoutingProfileValidationError('xray_fragment.target_path должен быть непустой строкой')

    patch = normalized.get('patch')
    if patch is not None and not isinstance(patch, dict):
        raise RoutingProfileValidationError('xray_fragment.patch должен быть JSON-объектом')

    return normalized


def _normalize_runtime_config(config_json: dict[str, Any] | None) -> RoutingRuntimeConfig:
    raw = _normalize_config_json(config_json)
    strategy = _normalize_strategy(raw.get('strategy'))

    xray_rule_networks = _normalize_str_list(raw.get('xray_rule_networks'), field_name='xray_rule_networks')
    invalid_networks = [item for item in xray_rule_networks if item not in _ALLOWED_XRAY_NETWORKS]
    if invalid_networks:
        raise RoutingProfileValidationError(
            f"xray_rule_networks содержит неподдерживаемые значения: {', '.join(invalid_networks)}"
        )

    xray_rule_protocols = _normalize_str_list(raw.get('xray_rule_protocols'), field_name='xray_rule_protocols')
    invalid_protocols = [item for item in xray_rule_protocols if item not in _ALLOWED_XRAY_PROTOCOLS]
    if invalid_protocols:
        raise RoutingProfileValidationError(
            f"xray_rule_protocols содержит неподдерживаемые значения: {', '.join(invalid_protocols)}"
        )

    extra_rule_fields = raw.get('xray_extra_rule_fields') or {}
    if not isinstance(extra_rule_fields, dict):
        raise RoutingProfileValidationError('xray_extra_rule_fields должен быть JSON-объектом')

    metadata = raw.get('metadata') or {}
    if not isinstance(metadata, dict):
        raise RoutingProfileValidationError('metadata должен быть JSON-объектом')

    outbound_tag = _normalize_optional_str(raw.get('xray_outbound_tag'))
    balancer_tag = _normalize_optional_str(raw.get('xray_balancer_tag'))
    if strategy == 'balancing' and not (outbound_tag or balancer_tag):
        raise RoutingProfileValidationError(
            'Для strategy=balancing укажите xray_outbound_tag или xray_balancer_tag'
        )

    return RoutingRuntimeConfig(
        strategy=strategy,
        required_tags=_normalize_str_list(raw.get('required_tags'), field_name='required_tags'),
        preferred_tags=_normalize_str_list(raw.get('preferred_tags'), field_name='preferred_tags'),
        avoid_tags=_normalize_str_list(raw.get('avoid_tags'), field_name='avoid_tags'),
        location_allow=_normalize_str_list(raw.get('location_allow'), field_name='location_allow'),
        transport_allow=_normalize_str_list(raw.get('transport_allow'), field_name='transport_allow'),
        require_subscription_url=_normalize_bool(
            raw.get('require_subscription_url'), field_name='require_subscription_url', default=False
        ),
        require_api_url=_normalize_bool(raw.get('require_api_url'), field_name='require_api_url', default=False),
        require_synced_source=_normalize_bool(
            raw.get('require_synced_source'), field_name='require_synced_source', default=True
        ),
        allow_missing_source=_normalize_bool(
            raw.get('allow_missing_source'), field_name='allow_missing_source', default=False
        ),
        allow_degraded=_normalize_bool(raw.get('allow_degraded'), field_name='allow_degraded', default=False),
        premium_only=_normalize_bool(raw.get('premium_only'), field_name='premium_only', default=False),
        reserve_only=_normalize_bool(raw.get('reserve_only'), field_name='reserve_only', default=False),
        xray_outbound_tag=outbound_tag,
        xray_balancer_tag=balancer_tag,
        xray_domain_match=_normalize_str_list(raw.get('xray_domain_match'), field_name='xray_domain_match'),
        xray_ip_match=_normalize_str_list(raw.get('xray_ip_match'), field_name='xray_ip_match'),
        xray_rule_protocols=xray_rule_protocols,
        xray_rule_networks=xray_rule_networks,
        xray_extra_rule_fields=dict(extra_rule_fields),
        xray_rule_overrides=_normalize_xray_rule_overrides(raw.get('xray_rule_overrides')),
        xray_fragment=_normalize_xray_fragment(raw.get('xray_fragment')),
        metadata=dict(metadata),
    )


class RoutingProfilesService:
    """
    Runtime-aware routing profile service.

    Current scope:
    - CRUD and default semantics for routing profiles
    - strict validation for config_json so web-admin/runtime share one schema
    - deterministic resolution by code or tags
    - conversion of profile config into NodeSelectionRequest
    - preview generation of Xray routing fragments for future safe apply-flow
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, profile_id: int) -> RoutingProfile | None:
        result = await self.session.execute(select(RoutingProfile).where(RoutingProfile.id == profile_id))
        return result.scalar_one_or_none()

    async def get_by_id_for_update(self, profile_id: int) -> RoutingProfile | None:
        result = await self.session.execute(
            select(RoutingProfile)
            .where(RoutingProfile.id == profile_id)
            .with_for_update()
        )
        return result.scalar_one_or_none()

    async def get_by_code(self, code: str) -> RoutingProfile | None:
        normalized_code = _normalize_code(code)
        result = await self.session.execute(
            select(RoutingProfile).where(func.lower(RoutingProfile.code) == normalized_code)
        )
        return result.scalar_one_or_none()

    async def get_by_code_for_update(self, code: str) -> RoutingProfile | None:
        normalized_code = _normalize_code(code)
        result = await self.session.execute(
            select(RoutingProfile)
            .where(func.lower(RoutingProfile.code) == normalized_code)
            .with_for_update()
        )
        return result.scalar_one_or_none()

    async def list_all(self) -> list[RoutingProfile]:
        result = await self.session.execute(
            select(RoutingProfile)
            .order_by(RoutingProfile.sort_order.asc(), RoutingProfile.id.asc())
        )
        return list(result.scalars().all())

    async def list_enabled(self) -> list[RoutingProfile]:
        result = await self.session.execute(
            select(RoutingProfile)
            .where(RoutingProfile.is_enabled.is_(True))
            .order_by(RoutingProfile.sort_order.asc(), RoutingProfile.id.asc())
        )
        return list(result.scalars().all())

    async def get_default(self) -> RoutingProfile | None:
        result = await self.session.execute(
            select(RoutingProfile)
            .where(RoutingProfile.is_default.is_(True))
            .order_by(RoutingProfile.id.asc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def clear_default(self, *, except_profile_id: int | None = None) -> None:
        stmt = select(RoutingProfile).where(RoutingProfile.is_default.is_(True))
        if except_profile_id is not None:
            stmt = stmt.where(RoutingProfile.id != except_profile_id)

        result = await self.session.execute(stmt)
        for row in result.scalars().all():
            row.is_default = False
        await self.session.flush()

    def validate_runtime_config(self, config_json: dict[str, Any] | None) -> RoutingRuntimeConfig:
        return _normalize_runtime_config(config_json)

    def build_node_selection_request(
        self,
        resolved: ResolvedRoutingProfile,
        *,
        runtime_overrides: dict[str, Any] | None = None,
    ) -> NodeSelectionRequest:
        runtime = resolved.runtime_config or _normalize_runtime_config(resolved.config)
        overrides = dict(runtime_overrides or {})

        required_tags = set(runtime.required_tags) | set(resolved.requested_tags)
        preferred_tags = set(runtime.preferred_tags)
        avoid_tags = set(runtime.avoid_tags)
        location_allow = set(runtime.location_allow)
        transport_allow = set(runtime.transport_allow)

        if runtime.premium_only:
            required_tags.add('premium')
        if runtime.reserve_only:
            required_tags.add('reserve')

        if overrides.get('required_tags'):
            required_tags |= set(_normalize_match_tags(overrides.get('required_tags')))
        if overrides.get('preferred_tags'):
            preferred_tags |= set(_normalize_match_tags(overrides.get('preferred_tags')))
        if overrides.get('avoid_tags'):
            avoid_tags |= set(_normalize_match_tags(overrides.get('avoid_tags')))
        if overrides.get('location_allow'):
            location_allow |= set(_normalize_match_tags(overrides.get('location_allow')))
        if overrides.get('transport_allow'):
            transport_allow |= set(_normalize_match_tags(overrides.get('transport_allow')))

        require_subscription_url = runtime.require_subscription_url
        require_api_url = runtime.require_api_url
        require_synced_source = runtime.require_synced_source
        allow_missing_source = runtime.allow_missing_source

        if 'require_subscription_url' in overrides:
            require_subscription_url = _normalize_bool(
                overrides.get('require_subscription_url'), field_name='require_subscription_url', default=require_subscription_url
            )
        if 'require_api_url' in overrides:
            require_api_url = _normalize_bool(
                overrides.get('require_api_url'), field_name='require_api_url', default=require_api_url
            )
        if 'require_synced_source' in overrides:
            require_synced_source = _normalize_bool(
                overrides.get('require_synced_source'), field_name='require_synced_source', default=require_synced_source
            )
        if 'allow_missing_source' in overrides:
            allow_missing_source = _normalize_bool(
                overrides.get('allow_missing_source'), field_name='allow_missing_source', default=allow_missing_source
            )

        metadata = dict(runtime.metadata)
        metadata.update({
            'routing_profile_id': resolved.profile.id if resolved.profile is not None else None,
            'routing_profile_code': resolved.profile.code if resolved.profile is not None else None,
            'routing_strategy': runtime.strategy,
            'allow_degraded': runtime.allow_degraded,
            'xray_outbound_tag': runtime.xray_outbound_tag,
            'xray_balancer_tag': runtime.xray_balancer_tag,
            'xray_rule_overrides_count': len(runtime.xray_rule_overrides),
        })
        extra_metadata = overrides.get('metadata')
        if isinstance(extra_metadata, dict):
            metadata.update(extra_metadata)

        policy_name = resolved.profile.code if resolved.profile is not None else 'default'
        return NodeSelectionRequest(
            policy_name=policy_name,
            required_tags=required_tags,
            preferred_tags=preferred_tags,
            avoid_tags=avoid_tags,
            location_allow=location_allow,
            transport_allow=transport_allow,
            require_subscription_url=require_subscription_url,
            require_api_url=require_api_url,
            require_synced_source=require_synced_source,
            allow_missing_source=allow_missing_source,
            metadata=metadata,
        )

    def build_xray_fragment_preview(
        self,
        resolved: ResolvedRoutingProfile,
        *,
        selected_outbound_tag: str | None = None,
        selected_balancer_tag: str | None = None,
    ) -> dict[str, Any]:
        runtime = resolved.runtime_config or _normalize_runtime_config(resolved.config)
        outbound_tag = selected_outbound_tag or runtime.xray_outbound_tag or 'vpn_auto_selected'
        balancer_tag = selected_balancer_tag or runtime.xray_balancer_tag or 'routing_profile_balancer'

        base_rule: dict[str, Any] = {'type': 'field'}
        if runtime.xray_domain_match:
            base_rule['domain'] = list(runtime.xray_domain_match)
        if runtime.xray_ip_match:
            base_rule['ip'] = list(runtime.xray_ip_match)
        if runtime.xray_rule_protocols:
            base_rule['protocol'] = list(runtime.xray_rule_protocols)
        if runtime.xray_rule_networks:
            base_rule['network'] = ','.join(runtime.xray_rule_networks)
        base_rule.update(dict(runtime.xray_extra_rule_fields))

        if runtime.strategy == 'balancing':
            base_rule['balancerTag'] = balancer_tag
        else:
            base_rule['outboundTag'] = outbound_tag

        rules = [base_rule]
        rules.extend(dict(item) for item in runtime.xray_rule_overrides)

        preview: dict[str, Any] = {
            'profile_code': resolved.profile.code if resolved.profile is not None else None,
            'strategy': runtime.strategy,
            'action': runtime.xray_fragment.get('action', 'insert'),
            'target_path': runtime.xray_fragment.get('target_path', 'routing.rules'),
            'routing': {'rules': rules},
            'metadata': {
                'requested_tags': list(resolved.requested_tags),
                'matched_by_default': resolved.matched_by_default,
            },
        }

        if runtime.strategy == 'balancing':
            preview['routing']['balancers'] = [
                {
                    'tag': balancer_tag,
                    'selector': [outbound_tag],
                    'strategy': {'type': 'random'},
                }
            ]

        if runtime.xray_fragment.get('patch'):
            preview['patch'] = dict(runtime.xray_fragment['patch'])

        return preview

    async def preview(
        self,
        *,
        profile_code: str | None = None,
        requested_tags: list[str] | tuple[str, ...] | set[str] | str | None = None,
        fallback_to_default: bool = True,
        runtime_overrides: dict[str, Any] | None = None,
    ) -> RoutingPreview:
        resolved = await self.resolve(
            profile_code=profile_code,
            requested_tags=requested_tags,
            fallback_to_default=fallback_to_default,
        )
        node_request = self.build_node_selection_request(resolved, runtime_overrides=runtime_overrides)
        fragment_preview = self.build_xray_fragment_preview(resolved)
        return RoutingPreview(resolved=resolved, node_request=node_request, xray_fragment_preview=fragment_preview)

    async def create(
        self,
        *,
        code: str,
        title: str,
        description: str | None = None,
        is_enabled: bool = True,
        is_default: bool = False,
        sort_order: int = 100,
        match_tags: list[str] | tuple[str, ...] | set[str] | str | None = None,
        config_json: dict[str, Any] | None = None,
        notes: str | None = None,
    ) -> RoutingProfile:
        normalized_code = _normalize_code(code)
        normalized_title = _normalize_required_str(title, field_name='title')
        normalized_config = _normalize_config_json(config_json)
        self.validate_runtime_config(normalized_config)

        if is_default:
            await self.clear_default()

        row = RoutingProfile(
            code=normalized_code,
            title=normalized_title,
            description=_normalize_optional_str(description),
            is_enabled=bool(is_enabled),
            is_default=bool(is_default),
            sort_order=max(0, int(sort_order)),
            match_tags=_normalize_match_tags(match_tags),
            config_json=normalized_config,
            notes=_normalize_optional_str(notes),
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def update(
        self,
        row: RoutingProfile,
        *,
        title: str | None = None,
        description: str | None = None,
        is_enabled: bool | None = None,
        is_default: bool | None = None,
        sort_order: int | None = None,
        match_tags: list[str] | tuple[str, ...] | set[str] | str | None = None,
        config_json: dict[str, Any] | None = None,
        notes: str | None = None,
    ) -> RoutingProfile:
        if title is not None:
            row.title = _normalize_required_str(title, field_name='title')

        if description is not None:
            row.description = _normalize_optional_str(description)

        if is_enabled is not None:
            row.is_enabled = bool(is_enabled)

        if is_default is not None:
            if bool(is_default):
                await self.clear_default(except_profile_id=row.id)
            row.is_default = bool(is_default)

        if sort_order is not None:
            row.sort_order = max(0, int(sort_order))

        if match_tags is not None:
            row.match_tags = _normalize_match_tags(match_tags)

        if config_json is not None:
            normalized_config = _normalize_config_json(config_json)
            self.validate_runtime_config(normalized_config)
            row.config_json = normalized_config

        if notes is not None:
            row.notes = _normalize_optional_str(notes)

        await self.session.flush()
        return row

    async def upsert(
        self,
        *,
        code: str,
        title: str,
        description: str | None = None,
        is_enabled: bool = True,
        is_default: bool = False,
        sort_order: int = 100,
        match_tags: list[str] | tuple[str, ...] | set[str] | str | None = None,
        config_json: dict[str, Any] | None = None,
        notes: str | None = None,
    ) -> RoutingProfile:
        existing = await self.get_by_code_for_update(code)
        if existing is None:
            return await self.create(
                code=code,
                title=title,
                description=description,
                is_enabled=is_enabled,
                is_default=is_default,
                sort_order=sort_order,
                match_tags=match_tags,
                config_json=config_json,
                notes=notes,
            )

        return await self.update(
            existing,
            title=title,
            description=description,
            is_enabled=is_enabled,
            is_default=is_default,
            sort_order=sort_order,
            match_tags=match_tags,
            config_json=config_json,
            notes=notes,
        )

    async def resolve(
        self,
        *,
        profile_code: str | None = None,
        requested_tags: list[str] | tuple[str, ...] | set[str] | str | None = None,
        fallback_to_default: bool = True,
    ) -> ResolvedRoutingProfile:
        normalized_requested_tags = _normalize_match_tags(requested_tags)

        if profile_code:
            row = await self.get_by_code(profile_code)
            if row is None:
                raise RoutingProfileValidationError('Routing profile с указанным code не найден')
            if not row.is_enabled and not fallback_to_default:
                raise RoutingProfileValidationError('Routing profile отключён')
            runtime = self.validate_runtime_config(dict(row.config_json or {}))
            return ResolvedRoutingProfile(
                profile=row,
                requested_tags=normalized_requested_tags,
                matched_by_default=False,
                runtime_config=runtime,
            )

        return await self.select_for_tags(normalized_requested_tags, fallback_to_default=fallback_to_default)

    async def select_for_tags(
        self,
        requested_tags: list[str] | tuple[str, ...] | set[str] | str | None,
        *,
        fallback_to_default: bool = True,
    ) -> ResolvedRoutingProfile:
        normalized_requested_tags = _normalize_match_tags(requested_tags)
        enabled_profiles = await self.list_enabled()

        if normalized_requested_tags:
            for row in enabled_profiles:
                profile_tags = _normalize_match_tags(getattr(row, 'match_tags', None))
                if not profile_tags:
                    continue
                if all(tag in normalized_requested_tags for tag in profile_tags):
                    return ResolvedRoutingProfile(
                        profile=row,
                        requested_tags=normalized_requested_tags,
                        matched_by_default=False,
                        runtime_config=self.validate_runtime_config(dict(row.config_json or {})),
                    )

        if fallback_to_default:
            default_profile = await self.get_default()
            if default_profile is not None and default_profile.is_enabled:
                return ResolvedRoutingProfile(
                    profile=default_profile,
                    requested_tags=normalized_requested_tags,
                    matched_by_default=True,
                    runtime_config=self.validate_runtime_config(dict(default_profile.config_json or {})),
                )

        return ResolvedRoutingProfile(
            profile=None,
            requested_tags=normalized_requested_tags,
            matched_by_default=False,
            runtime_config=_normalize_runtime_config({}),
        )

    async def delete(self, profile_id: int) -> bool:
        row = await self.get_by_id_for_update(profile_id)
        if row is None:
            return False
        await self.session.delete(row)
        await self.session.flush()
        return True
