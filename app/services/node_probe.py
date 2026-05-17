"""Реал-тайм health-probe нод (FEA-ADMIN-NODE-MONITOR).

Один тик `probe_nodes_health` (90 сек):
  1. Один HTTP-вызов `/api/nodes` через MarzbanClient (под circuit breaker).
     Время этого вызова — общая panel-latency. Если breaker open или
     panel вернул ошибку — все enabled-ноды получают status=error.
  2. Опциональный вызов `/api/system` для total_user/users_active —
     attributed только к default-ноде (Marzban API не даёт per-node
     breakdown, поэтому копировать одно и то же значение на все строки
     было бы недостоверно).
  3. Per-node:
       панель упала         → error (latency=null, error_text)
       node missing в /api/nodes → down
       source_status=disabled    → degraded
       source_status=active      → ok (latency = panel-latency)
  4. Insert sample + update денорм + инкремент/сброс consecutive_fail_count.
  5. Если `consecutive_fail_count` транзитнулся в 5 — алерт `node_down`
     через NotificationDispatcher всем admin_ids (один раз на streak,
     correlation_key привязан к моменту срабатывания).

Cleanup-job `cleanup_node_health_samples` удаляет samples старше 30 дней.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import Settings
from app.db.models import (
    AuditAction,
    AuditActorType,
    NodeHealthProbeStatus,
    NodeRegistry,
    NodeSourceStatus,
)
from app.db.repositories import AppSettingsRepository, AuditLogRepository
from app.db.repositories.node_health import NodeHealthSampleRepository
from app.db.repositories.node_registry import NodeRegistryRepository
from app.observability.metrics import (
    NODE_HEALTH,
    NODE_LATENCY_SECONDS,
    NODE_USERS_ONLINE,
)
from app.services.marzban import MarzbanAPIError, MarzbanClient, MarzbanNodeSnapshot
from app.services.notification_dispatcher import NotificationDispatcher

logger = logging.getLogger(__name__)


NODE_DOWN_ALERT_THRESHOLD = 5
HEALTH_SAMPLES_TTL_DAYS = 30


def _coerce_optional_non_negative_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        iv = int(value)
    except (TypeError, ValueError):
        return None
    return iv if iv >= 0 else None


def _truncate_error(value: str | None, limit: int = 512) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1] + '…'


def _resolve_probe_status(
    *,
    node: NodeRegistry,
    panel_error: str | None,
    panel_latency_ms: int | None,
    snapshots_by_source_id: dict[str, MarzbanNodeSnapshot],
) -> tuple[NodeHealthProbeStatus, int | None, str | None]:
    """Маппит результат панели в (status, latency_ms, error_text) для одной ноды."""
    if panel_error is not None:
        return (NodeHealthProbeStatus.error, None, panel_error)

    source_id = (node.source_node_id or '').strip() or None
    if source_id is None:
        return (
            NodeHealthProbeStatus.error,
            panel_latency_ms,
            'node has no source_node_id (run /admin/nodes sync)',
        )

    snap = snapshots_by_source_id.get(source_id)
    if snap is None:
        return (
            NodeHealthProbeStatus.down,
            panel_latency_ms,
            f'node {source_id} отсутствует в /api/nodes',
        )

    if snap.source_status == NodeSourceStatus.active:
        return (NodeHealthProbeStatus.ok, panel_latency_ms, None)

    if snap.source_status == NodeSourceStatus.disabled:
        return (
            NodeHealthProbeStatus.degraded,
            panel_latency_ms,
            f'source_status={snap.source_status.value}',
        )

    return (
        NodeHealthProbeStatus.down,
        panel_latency_ms,
        f'source_status={snap.source_status.value}',
    )


def _update_node_metrics(
    node: NodeRegistry,
    *,
    status: NodeHealthProbeStatus,
    latency_ms: int | None,
    users_online: int | None,
) -> None:
    NODE_HEALTH.labels(node=node.code).set(
        1.0 if status == NodeHealthProbeStatus.ok else 0.0
    )
    if latency_ms is not None:
        NODE_LATENCY_SECONDS.labels(node=node.code).set(latency_ms / 1000.0)
    if users_online is not None:
        NODE_USERS_ONLINE.labels(node=node.code).set(float(users_online))


class NodeProbeService:
    """Реал-тайм мониторинг нод. См. модуль-docstring для контракта."""

    def __init__(
        self,
        *,
        sessionmaker,
        settings: Settings,
        marzban: MarzbanClient,
        dispatcher: NotificationDispatcher | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings
        self._marzban = marzban
        self._dispatcher = dispatcher

    async def probe_all(self) -> None:
        """Один тик probe для всех enabled-нод."""
        panel_error: str | None = None
        panel_latency_ms: int | None = None
        snapshots_by_source_id: dict[str, MarzbanNodeSnapshot] = {}

        t0 = time.monotonic()
        try:
            snapshots = await self._marzban.list_nodes()
            panel_latency_ms = int((time.monotonic() - t0) * 1000)
            for snap in snapshots:
                source_id = (snap.source_node_id or '').strip()
                if source_id:
                    snapshots_by_source_id[source_id] = snap
        except MarzbanAPIError as exc:
            panel_latency_ms = int((time.monotonic() - t0) * 1000)
            panel_error = _truncate_error(str(exc))
            logger.warning('Marzban /api/nodes probe failed: %s', exc)
        except Exception as exc:
            panel_latency_ms = int((time.monotonic() - t0) * 1000)
            panel_error = _truncate_error(f'unexpected: {exc!r}')
            logger.exception('Marzban /api/nodes probe raised unexpectedly')

        users_total: int | None = None
        users_online: int | None = None
        if panel_error is None:
            try:
                system_stats = await self._marzban.get_system_stats()
                users_total = _coerce_optional_non_negative_int(system_stats.get('total_user'))
                users_online = _coerce_optional_non_negative_int(
                    system_stats.get('users_active')
                )
            except MarzbanAPIError as exc:
                logger.warning(
                    'Marzban /api/system fetch failed (probe continues without users counts): %s',
                    exc,
                )
            except Exception:
                logger.exception('Marzban /api/system fetch raised unexpectedly')

        now = datetime.now(timezone.utc)
        async with self._sessionmaker.begin() as session:
            node_repo = NodeRegistryRepository(session)
            sample_repo = NodeHealthSampleRepository(session)
            nodes = await node_repo.list_enabled()

            for node in nodes:
                status, latency_ms, error_text = _resolve_probe_status(
                    node=node,
                    panel_error=panel_error,
                    panel_latency_ms=panel_latency_ms,
                    snapshots_by_source_id=snapshots_by_source_id,
                )
                # users — только для default-ноды и только при ok-статусе,
                # чтобы не дублировать одно и то же значение на все строки.
                node_users_total = (
                    users_total
                    if (node.is_default and status == NodeHealthProbeStatus.ok)
                    else None
                )
                node_users_online = (
                    users_online
                    if (node.is_default and status == NodeHealthProbeStatus.ok)
                    else None
                )

                await sample_repo.insert_sample(
                    node_id=node.id,
                    status=status,
                    latency_ms=latency_ms,
                    users_total=node_users_total,
                    users_online=node_users_online,
                    error_text=error_text,
                    ts=now,
                )

                success = status == NodeHealthProbeStatus.ok
                prev_fail = int(node.consecutive_fail_count or 0)
                await sample_repo.update_node_denorm(
                    node,
                    latency_ms=latency_ms,
                    users_online=node_users_online,
                    users_total=node_users_total,
                    probed_at=now,
                    success=success,
                )

                _update_node_metrics(
                    node,
                    status=status,
                    latency_ms=latency_ms,
                    users_online=node_users_online,
                )

                # Алерт только в момент транзита prev<5 → curr==5 за один тик.
                if (
                    not success
                    and node.consecutive_fail_count == NODE_DOWN_ALERT_THRESHOLD
                    and prev_fail < NODE_DOWN_ALERT_THRESHOLD
                ):
                    await self._fire_node_down_alert(
                        session=session,
                        node=node,
                        status=status,
                        latency_ms=latency_ms,
                        error_text=error_text,
                        probed_at=now,
                    )

    async def _fire_node_down_alert(
        self,
        *,
        session,
        node: NodeRegistry,
        status: NodeHealthProbeStatus,
        latency_ms: int | None,
        error_text: str | None,
        probed_at: datetime,
    ) -> None:
        if self._dispatcher is None:
            logger.warning(
                'NotificationDispatcher не сконфигурирован; node_down алерт для node_id=%s пропущен',
                node.id,
            )
            return

        admin_ids = await self._load_admin_ids(session)
        if not admin_ids:
            logger.warning(
                'admin_ids пусты; node_down алерт для node_id=%s (code=%s) пропущен',
                node.id, node.code,
            )
            return

        default_text_parts = [
            f'🚨 <b>Нода {node.display_name} ({node.code}) недоступна</b>',
            f'Подряд {NODE_DOWN_ALERT_THRESHOLD} fail-probe.',
        ]
        if error_text:
            default_text_parts.append(f'Ошибка: <code>{error_text}</code>')
        default_text_parts.append(f'Проверьте /admin/nodes/{node.id}.')
        default_text = '\n'.join(default_text_parts)

        bucket = int(probed_at.timestamp())
        context = {
            'node_code': node.code,
            'node_display_name': node.display_name,
            'node_id': node.id,
            'consecutive_fails': int(node.consecutive_fail_count or 0),
            'error_text': error_text or '',
            'latency_ms': latency_ms,
            'status': status.value,
        }

        delivered_admin_ids: list[int] = []
        for admin_tg_id in admin_ids:
            try:
                ok = await self._dispatcher.dispatch(
                    session=session,
                    code='node_down',
                    chat_id=int(admin_tg_id),
                    user_id=int(admin_tg_id),
                    default_text=default_text,
                    default_parse_mode='HTML',
                    context=context,
                    correlation_key=f'node_down:{node.id}:{bucket}:{admin_tg_id}',
                )
                if ok:
                    delivered_admin_ids.append(int(admin_tg_id))
            except Exception:
                logger.exception(
                    'Failed to dispatch node_down to admin_tg_id=%s for node_id=%s',
                    admin_tg_id, node.id,
                )

        await AuditLogRepository(session).create(
            action=AuditAction.node_health_alert,
            actor_type=AuditActorType.system,
            actor_tg_id=None,
            entity_type='node_registry',
            entity_id=str(node.id),
            details={
                'code': node.code,
                'display_name': node.display_name,
                'consecutive_fails': int(node.consecutive_fail_count or 0),
                'error_text': error_text,
                'admin_tg_ids': delivered_admin_ids,
                'probed_at': probed_at.isoformat(),
            },
        )

    async def _load_admin_ids(self, session) -> list[int]:
        """Резолв admin_ids из AppSettings, с fallback на env settings."""
        try:
            row = await AppSettingsRepository(session).get()
        except Exception:
            row = None
            logger.exception('Failed to load AppSettings for node_down alert; falling back to env')

        candidates: list[Any] = []
        if row and getattr(row, 'admin_ids', None):
            candidates = list(row.admin_ids)
        if not candidates:
            candidates = list(self._settings.admin_ids or [])

        normalized: list[int] = []
        seen: set[int] = set()
        for value in candidates:
            try:
                iv = int(value)
            except (TypeError, ValueError):
                continue
            if iv in seen:
                continue
            seen.add(iv)
            normalized.append(iv)
        return normalized


async def probe_nodes_health(
    *,
    bot=None,
    sessionmaker,
    settings: Settings,
    marzban: MarzbanClient,
    dispatcher: NotificationDispatcher | None = None,
) -> None:
    """APScheduler job wrapper. `bot` принимается для единообразия с другими jobs."""
    service = NodeProbeService(
        sessionmaker=sessionmaker,
        settings=settings,
        marzban=marzban,
        dispatcher=dispatcher,
    )
    await service.probe_all()


async def cleanup_node_health_samples(
    *,
    bot=None,
    sessionmaker,
    settings: Settings | None = None,
    marzban: MarzbanClient | None = None,
    dispatcher: NotificationDispatcher | None = None,
) -> None:
    """APScheduler job: TTL 30 дней для node_health_samples."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=HEALTH_SAMPLES_TTL_DAYS)
    async with sessionmaker.begin() as session:
        repo = NodeHealthSampleRepository(session)
        deleted = await repo.cleanup_older_than(cutoff)
        if deleted:
            logger.info(
                'Cleaned up %d node_health_samples older than %s', deleted, cutoff.isoformat()
            )
