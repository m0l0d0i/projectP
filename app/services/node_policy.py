from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    NodeHealthStatus,
    NodeRegistry,
    NodeSourceStatus,
    NodeSyncState,
)
from app.db.repositories.node_registry import NodeRegistryRepository


DEFAULT_POLICY_CATALOG: dict[str, dict[str, Any]] = {
    'default': {
        'description': 'Fallback routing profile when no specialized policy is requested.',
        'required_tags': [],
        'preferred_tags': [],
        'avoid_tags': [],
        'transport_hints': [],
        'location_allow': [],
        'require_synced_source': True,
        'allow_missing_source': False,
        'allow_degraded': False,
    },
    'ai_clean': {
        'description': 'Prefer nodes tagged for cleaner AI traffic handling and low-noise routing.',
        'required_tags': ['ai_clean'],
        'preferred_tags': ['stable', 'clean'],
        'avoid_tags': ['experimental'],
        'transport_hints': ['reality', 'tcp'],
        'location_allow': [],
        'require_synced_source': True,
        'allow_missing_source': False,
        'allow_degraded': False,
    },
    'ru_bridge': {
        'description': 'Prefer nodes appropriate for RU bridge scenarios.',
        'required_tags': ['ru_bridge'],
        'preferred_tags': ['stable'],
        'avoid_tags': ['experimental'],
        'transport_hints': ['reality', 'grpc', 'ws'],
        'location_allow': ['ru', 'fi', 'nl', 'de'],
        'require_synced_source': True,
        'allow_missing_source': False,
        'allow_degraded': False,
    },
    'mobile_stable': {
        'description': 'Prefer stable mobile-friendly nodes.',
        'required_tags': ['mobile_stable'],
        'preferred_tags': ['stable', 'mobile'],
        'avoid_tags': ['experimental'],
        'transport_hints': ['reality', 'grpc', 'ws', 'tcp'],
        'location_allow': [],
        'require_synced_source': True,
        'allow_missing_source': False,
        'allow_degraded': True,
    },
    'low_risk_reality': {
        'description': 'Prefer lower-risk Reality nodes for conservative routing.',
        'required_tags': ['low_risk_reality'],
        'preferred_tags': ['stable', 'reality'],
        'avoid_tags': ['experimental', 'high_risk'],
        'transport_hints': ['reality'],
        'location_allow': [],
        'require_synced_source': True,
        'allow_missing_source': False,
        'allow_degraded': False,
    },
}


@dataclass(slots=True)
class NodeSelectionRequest:
    policy_name: str = 'default'
    required_tags: set[str] = field(default_factory=set)
    preferred_tags: set[str] = field(default_factory=set)
    avoid_tags: set[str] = field(default_factory=set)
    location_allow: set[str] = field(default_factory=set)
    preferred_locations: set[str] = field(default_factory=set)
    transport_allow: set[str] = field(default_factory=set)
    require_subscription_url: bool = False
    require_api_url: bool = False
    require_synced_source: bool = True
    allow_missing_source: bool = False
    allow_degraded: bool = False
    premium_only: bool = False
    reserve_only: bool = False
    preferred_node_codes: set[str] = field(default_factory=set)
    avoid_node_codes: set[str] = field(default_factory=set)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class NodeSelectionDecision:
    node: NodeRegistry | None
    policy_name: str
    strategy: str
    reason: str
    considered_count: int
    candidate_count: int
    scores: list[dict[str, Any]] = field(default_factory=list)

    @property
    def selected_node_code(self) -> str | None:
        return self.node.code if self.node is not None else None


class NodePolicyService:
    """
    Policy-based, sync-aware node selector built on top of the live Node Registry.

    Current scope:
    - policy-based candidate filtering
    - sync-aware routability checks
    - deterministic ranking with business preferences preserved
    - routing-profile aware request metadata support
    - safe fallback to default node
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = NodeRegistryRepository(session)

    async def list_catalog(self) -> dict[str, dict[str, Any]]:
        return dict(DEFAULT_POLICY_CATALOG)

    async def select_node(self, request: NodeSelectionRequest | None = None) -> NodeSelectionDecision:
        req = self._normalize_request(request)
        nodes = await self.repo.list_all()
        considered = len(nodes)

        filtered = [node for node in nodes if self._matches_required_filters(node, req)]
        ranked = self._rank_nodes(filtered, req)

        if ranked:
            selected = ranked[0]['node']
            return NodeSelectionDecision(
                node=selected,
                policy_name=req.policy_name,
                strategy=str(req.metadata.get('routing_strategy') or 'ranked_match'),
                reason='Matched requested policy and selected highest-ranked routable node.',
                considered_count=considered,
                candidate_count=len(filtered),
                scores=[self._serialize_scored_item(item) for item in ranked[:10]],
            )

        fallback = await self._fallback_node(req)
        if fallback is not None:
            return NodeSelectionDecision(
                node=fallback,
                policy_name=req.policy_name,
                strategy='default_fallback',
                reason='No strict policy match found; fell back to the best routable default candidate.',
                considered_count=considered,
                candidate_count=0,
                scores=[],
            )

        return NodeSelectionDecision(
            node=None,
            policy_name=req.policy_name,
            strategy='no_match',
            reason='No enabled sync-valid node satisfied the routing policy or fallback rules.',
            considered_count=considered,
            candidate_count=0,
            scores=[],
        )

    async def resolve_policy_for_tags(
        self,
        *,
        policy_name: str,
        extra_required_tags: list[str] | set[str] | tuple[str, ...] | None = None,
        extra_preferred_tags: list[str] | set[str] | tuple[str, ...] | None = None,
        avoid_tags: list[str] | set[str] | tuple[str, ...] | None = None,
        location_allow: list[str] | set[str] | tuple[str, ...] | None = None,
        transport_allow: list[str] | set[str] | tuple[str, ...] | None = None,
        require_subscription_url: bool = False,
        require_api_url: bool = False,
        require_synced_source: bool = True,
        allow_missing_source: bool = False,
        allow_degraded: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> NodeSelectionDecision:
        request = NodeSelectionRequest(
            policy_name=policy_name,
            required_tags=self._normalize_tags(extra_required_tags),
            preferred_tags=self._normalize_tags(extra_preferred_tags),
            avoid_tags=self._normalize_tags(avoid_tags),
            location_allow=self._normalize_tags(location_allow),
            transport_allow=self._normalize_tags(transport_allow),
            require_subscription_url=require_subscription_url,
            require_api_url=require_api_url,
            require_synced_source=require_synced_source,
            allow_missing_source=allow_missing_source,
            allow_degraded=allow_degraded,
            metadata=dict(metadata or {}),
        )
        return await self.select_node(request)

    async def get_default_routable_node(self) -> NodeRegistry | None:
        default_node = await self._repo_default_node()
        if default_node is not None and self._is_routable(default_node):
            return default_node

        ranked = self._rank_nodes(
            [node for node in await self.repo.list_all() if self._is_routable(node)],
            self._normalize_request(NodeSelectionRequest()),
        )
        return ranked[0]['node'] if ranked else None

    async def explain_selection(self, request: NodeSelectionRequest | None = None) -> dict[str, Any]:
        normalized = self._normalize_request(request)
        decision = await self.select_node(normalized)
        return {
            'policy_name': decision.policy_name,
            'strategy': decision.strategy,
            'reason': decision.reason,
            'selected_node_code': decision.selected_node_code,
            'considered_count': decision.considered_count,
            'candidate_count': decision.candidate_count,
            'request': {
                'required_tags': sorted(normalized.required_tags),
                'preferred_tags': sorted(normalized.preferred_tags),
                'avoid_tags': sorted(normalized.avoid_tags),
                'location_allow': sorted(normalized.location_allow),
                'preferred_locations': sorted(normalized.preferred_locations),
                'transport_allow': sorted(normalized.transport_allow),
                'require_subscription_url': normalized.require_subscription_url,
                'require_api_url': normalized.require_api_url,
                'require_synced_source': normalized.require_synced_source,
                'allow_missing_source': normalized.allow_missing_source,
                'allow_degraded': normalized.allow_degraded,
                'premium_only': normalized.premium_only,
                'reserve_only': normalized.reserve_only,
                'preferred_node_codes': sorted(normalized.preferred_node_codes),
                'avoid_node_codes': sorted(normalized.avoid_node_codes),
                'metadata': dict(normalized.metadata),
            },
            'scores': decision.scores,
        }

    async def _fallback_node(self, request: NodeSelectionRequest) -> NodeRegistry | None:
        default_node = await self._repo_default_node()
        if default_node is not None and self._matches_soft_filters(default_node, request):
            return default_node

        routable = [
            node
            for node in await self.repo.list_all()
            if self._is_routable(
                node,
                require_synced_source=request.require_synced_source,
                allow_missing_source=request.allow_missing_source,
                allow_degraded=request.allow_degraded,
            )
        ]
        softened = [node for node in routable if self._matches_soft_filters(node, request)]
        ranked = self._rank_nodes(softened, request)
        return ranked[0]['node'] if ranked else None

    async def _repo_default_node(self) -> NodeRegistry | None:
        if hasattr(self.repo, 'get_default_node'):
            return await self.repo.get_default_node()
        return await self.repo.get_default()

    def _normalize_request(self, request: NodeSelectionRequest | None) -> NodeSelectionRequest:
        base = request or NodeSelectionRequest()
        policy_name = (base.policy_name or 'default').strip().lower()
        policy = DEFAULT_POLICY_CATALOG.get(policy_name, DEFAULT_POLICY_CATALOG['default'])
        metadata = dict(base.metadata or {})

        required_tags = self._normalize_tags(policy.get('required_tags')) | self._normalize_tags(base.required_tags)
        preferred_tags = self._normalize_tags(policy.get('preferred_tags')) | self._normalize_tags(base.preferred_tags)
        avoid_tags = self._normalize_tags(policy.get('avoid_tags')) | self._normalize_tags(base.avoid_tags)
        location_allow = self._normalize_tags(policy.get('location_allow')) | self._normalize_tags(base.location_allow)
        preferred_locations = self._normalize_tags(base.preferred_locations) | self._normalize_tags(metadata.get('preferred_locations'))
        transport_allow = self._normalize_tags(policy.get('transport_hints')) | self._normalize_tags(base.transport_allow)
        preferred_node_codes = self._normalize_tags(base.preferred_node_codes) | self._normalize_tags(metadata.get('preferred_node_codes'))
        avoid_node_codes = self._normalize_tags(base.avoid_node_codes) | self._normalize_tags(metadata.get('avoid_node_codes'))

        premium_only = bool(base.premium_only or self._metadata_bool(metadata, 'premium_only'))
        reserve_only = bool(base.reserve_only or self._metadata_bool(metadata, 'reserve_only'))
        allow_degraded = bool(base.allow_degraded or policy.get('allow_degraded', False) or self._metadata_bool(metadata, 'allow_degraded'))

        if premium_only:
            required_tags.add('premium')
        if reserve_only:
            required_tags.add('reserve')

        return NodeSelectionRequest(
            policy_name=policy_name,
            required_tags=required_tags,
            preferred_tags=preferred_tags,
            avoid_tags=avoid_tags,
            location_allow=location_allow,
            preferred_locations=preferred_locations,
            transport_allow=transport_allow,
            require_subscription_url=bool(base.require_subscription_url or self._metadata_bool(metadata, 'require_subscription_url')),
            require_api_url=bool(base.require_api_url or self._metadata_bool(metadata, 'require_api_url')),
            require_synced_source=bool(
                policy.get('require_synced_source', True)
                if not base.require_synced_source and 'require_synced_source' not in metadata
                else self._bool_with_default(base.require_synced_source, metadata.get('require_synced_source'), default=policy.get('require_synced_source', True))
            ),
            allow_missing_source=bool(policy.get('allow_missing_source', False) or base.allow_missing_source or self._metadata_bool(metadata, 'allow_missing_source')),
            allow_degraded=allow_degraded,
            premium_only=premium_only,
            reserve_only=reserve_only,
            preferred_node_codes=preferred_node_codes,
            avoid_node_codes=avoid_node_codes,
            metadata=metadata,
        )

    def _matches_required_filters(self, node: NodeRegistry, request: NodeSelectionRequest) -> bool:
        if not self._is_routable(
            node,
            require_synced_source=request.require_synced_source,
            allow_missing_source=request.allow_missing_source,
            allow_degraded=request.allow_degraded,
        ):
            return False

        node_code = (node.code or '').strip().lower()
        node_tags = self._node_tags(node)

        if request.preferred_node_codes and node_code in request.preferred_node_codes:
            pass
        if request.avoid_node_codes and node_code in request.avoid_node_codes:
            return False
        if request.required_tags and not request.required_tags.issubset(node_tags):
            return False
        if request.avoid_tags and node_tags.intersection(request.avoid_tags):
            return False
        if request.location_allow:
            node_location = (node.location_code or '').strip().lower()
            if node_location not in request.location_allow:
                return False
        if request.transport_allow:
            transport_hint = (node.transport_hint or '').strip().lower()
            if transport_hint not in request.transport_allow:
                return False
        if request.require_subscription_url and not (node.subscription_base_url or '').strip():
            return False
        if request.require_api_url and not (node.api_base_url or '').strip():
            return False
        if request.premium_only and not self._has_capability(node, 'premium') and 'premium' not in node_tags:
            return False
        if request.reserve_only and not self._has_capability(node, 'reserve') and 'reserve' not in node_tags:
            return False

        return True

    def _matches_soft_filters(self, node: NodeRegistry, request: NodeSelectionRequest) -> bool:
        if not self._is_routable(
            node,
            require_synced_source=request.require_synced_source,
            allow_missing_source=request.allow_missing_source,
            allow_degraded=request.allow_degraded,
        ):
            return False

        node_code = (node.code or '').strip().lower()
        node_tags = self._node_tags(node)
        if request.avoid_node_codes and node_code in request.avoid_node_codes:
            return False
        if request.avoid_tags and node_tags.intersection(request.avoid_tags):
            return False
        if request.require_subscription_url and not (node.subscription_base_url or '').strip():
            return False
        if request.require_api_url and not (node.api_base_url or '').strip():
            return False
        return True

    def _rank_nodes(self, nodes: list[NodeRegistry], request: NodeSelectionRequest) -> list[dict[str, Any]]:
        scored: list[dict[str, Any]] = []
        for node in nodes:
            node_tags = self._node_tags(node)
            node_code = (node.code or '').strip().lower()
            node_location = (node.location_code or '').strip().lower()
            score = 0

            score += len(request.preferred_tags.intersection(node_tags)) * 100
            if request.preferred_node_codes and node_code in request.preferred_node_codes:
                score += 250
            if request.preferred_locations and node_location in request.preferred_locations:
                score += 40
            if node.is_default:
                score += 40
            if node.health_status == NodeHealthStatus.healthy:
                score += 30
            elif node.health_status == NodeHealthStatus.degraded:
                score += 10 if request.allow_degraded else -80

            source_status = self._node_source_status(node)
            sync_state = self._node_sync_state(node)
            if sync_state == NodeSyncState.synced:
                score += 25
            elif sync_state == NodeSyncState.never_synced:
                score += 5
            elif sync_state == NodeSyncState.error:
                score -= 50
            elif sync_state == NodeSyncState.missing:
                score -= 200

            if source_status == NodeSourceStatus.active:
                score += 15
            elif source_status == NodeSourceStatus.disabled:
                score -= 100

            if request.premium_only and self._has_capability(node, 'premium'):
                score += 20
            if request.reserve_only and self._has_capability(node, 'reserve'):
                score += 20

            score += max(int(node.weight or 0), 0)
            score -= max(int(node.priority or 0), 0)
            score -= max(int(node.sort_order or 0), 0)

            if request.transport_allow:
                transport_hint = (node.transport_hint or '').strip().lower()
                if transport_hint in request.transport_allow:
                    score += 15

            if request.location_allow and node_location in request.location_allow:
                score += 10

            scored.append(
                {
                    'node': node,
                    'score': score,
                    'matched_preferred_tags': sorted(request.preferred_tags.intersection(node_tags)),
                }
            )

        scored.sort(
            key=lambda item: (
                -item['score'],
                int(item['node'].priority or 0),
                int(item['node'].sort_order or 0),
                item['node'].id,
            )
        )
        return scored

    @classmethod
    def _is_routable(
        cls,
        node: NodeRegistry,
        *,
        require_synced_source: bool = True,
        allow_missing_source: bool = False,
        allow_degraded: bool = False,
    ) -> bool:
        if not bool(node.is_enabled):
            return False
        allowed_health = {NodeHealthStatus.healthy}
        if allow_degraded:
            allowed_health.add(NodeHealthStatus.degraded)
        else:
            allowed_health.add(NodeHealthStatus.degraded)
        if node.health_status not in allowed_health:
            return False

        source_status = cls._node_source_status(node)
        sync_state = cls._node_sync_state(node)

        if source_status == NodeSourceStatus.disabled:
            return False
        if sync_state == NodeSyncState.error:
            return False
        if sync_state == NodeSyncState.missing and not allow_missing_source:
            return False
        if require_synced_source and sync_state not in {NodeSyncState.synced, NodeSyncState.never_synced}:
            return False

        return True

    @staticmethod
    def _node_source_status(node: NodeRegistry) -> NodeSourceStatus:
        value = getattr(node, 'source_status', None)
        if isinstance(value, NodeSourceStatus):
            return value
        raw = (str(getattr(value, 'value', value) or '')).strip().lower()
        if not raw:
            return NodeSourceStatus.unknown
        try:
            return NodeSourceStatus(raw)
        except ValueError:
            return NodeSourceStatus.unknown

    @staticmethod
    def _node_sync_state(node: NodeRegistry) -> NodeSyncState:
        value = getattr(node, 'sync_state', None)
        if isinstance(value, NodeSyncState):
            return value
        raw = (str(getattr(value, 'value', value) or '')).strip().lower()
        if not raw:
            return NodeSyncState.never_synced
        try:
            return NodeSyncState(raw)
        except ValueError:
            return NodeSyncState.never_synced

    @staticmethod
    def _node_tags(node: NodeRegistry) -> set[str]:
        tags = getattr(node, 'policy_tags', None) or []
        return {str(tag).strip().lower() for tag in tags if str(tag).strip()}

    @staticmethod
    def _normalize_tags(values: list[str] | set[str] | tuple[str, ...] | None) -> set[str]:
        if not values:
            return set()
        return {str(value).strip().lower() for value in values if str(value).strip()}

    @staticmethod
    def _has_capability(node: NodeRegistry, key: str) -> bool:
        payload = getattr(node, 'capabilities_json', None) or {}
        if not isinstance(payload, dict):
            return False
        value = payload.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            return value.strip().lower() in {'1', 'true', 'yes', 'on'}
        return False

    @staticmethod
    def _metadata_bool(metadata: dict[str, Any], key: str) -> bool:
        value = metadata.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            return value.strip().lower() in {'1', 'true', 'yes', 'on'}
        return False

    @classmethod
    def _bool_with_default(cls, explicit_value: bool, metadata_value: Any, *, default: bool) -> bool:
        if metadata_value is None:
            return bool(explicit_value or default)
        return cls._metadata_bool({'value': metadata_value}, 'value')

    @classmethod
    def _serialize_scored_item(cls, item: dict[str, Any]) -> dict[str, Any]:
        node: NodeRegistry = item['node']
        source_status = cls._node_source_status(node)
        sync_state = cls._node_sync_state(node)
        return {
            'node_id': node.id,
            'code': node.code,
            'display_name': node.display_name,
            'score': item['score'],
            'priority': node.priority,
            'weight': node.weight,
            'sort_order': node.sort_order,
            'health_status': node.health_status.value if hasattr(node.health_status, 'value') else str(node.health_status),
            'source_status': source_status.value,
            'sync_state': sync_state.value,
            'subscription_base_url_present': bool((node.subscription_base_url or '').strip()),
            'api_base_url_present': bool((node.api_base_url or '').strip()),
            'matched_preferred_tags': item.get('matched_preferred_tags', []),
            'policy_tags': sorted(cls._node_tags(node)),
        }
