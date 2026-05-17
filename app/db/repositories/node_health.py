from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import case, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    NodeHealthProbeStatus,
    NodeHealthSample,
    NodeRegistry,
)


@dataclass(slots=True, frozen=True)
class NodeHealthRangePoint:
    """Точка downsample-агрегата для графика на /admin/nodes/{id}."""

    ts: datetime
    latency_ms_avg: float | None
    users_online_avg: float | None
    users_total_avg: float | None
    ok_count: int
    fail_count: int


def _normalize_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _normalize_status(value: NodeHealthProbeStatus | str) -> NodeHealthProbeStatus:
    if isinstance(value, NodeHealthProbeStatus):
        return value
    normalized = (str(value or '').strip().lower())
    try:
        return NodeHealthProbeStatus(normalized)
    except ValueError as exc:
        raise ValueError(f'Некорректный node_health_probe_status: {value}') from exc


def _normalize_non_negative_int(value: int | None) -> int | None:
    if value is None:
        return None
    iv = int(value)
    if iv < 0:
        return None
    return iv


class NodeHealthSampleRepository:
    """Repository для node_health_samples (FEA-ADMIN-NODE-MONITOR).

    Заметки:
    - `insert_sample` всегда нормализует ts в UTC; для тестов и backfill
      допускается явный `ts`, иначе ставится текущее UTC-время.
    - `range_for_node` поддерживает server-side downsample через date_trunc:
      `bucket_seconds=60` → 'minute', `bucket_seconds=3600` → 'hour'.
      Любое другое значение приводит к ошибке (контракт: только две
      гранулярности — 24h/7d).
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def insert_sample(
        self,
        *,
        node_id: int,
        status: NodeHealthProbeStatus | str,
        latency_ms: int | None = None,
        users_total: int | None = None,
        users_online: int | None = None,
        error_text: str | None = None,
        ts: datetime | None = None,
    ) -> NodeHealthSample:
        normalized_error = (error_text or '').strip() or None
        row = NodeHealthSample(
            node_id=int(node_id),
            status=_normalize_status(status),
            latency_ms=_normalize_non_negative_int(latency_ms),
            users_total=_normalize_non_negative_int(users_total),
            users_online=_normalize_non_negative_int(users_online),
            error_text=normalized_error,
            ts=_normalize_utc(ts) or datetime.now(timezone.utc),
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def latest_for_node(self, node_id: int) -> NodeHealthSample | None:
        res = await self.session.execute(
            select(NodeHealthSample)
            .where(NodeHealthSample.node_id == int(node_id))
            .order_by(NodeHealthSample.ts.desc(), NodeHealthSample.id.desc())
            .limit(1)
        )
        return res.scalar_one_or_none()

    async def latest_per_node(
        self,
        node_ids: Iterable[int] | None = None,
    ) -> dict[int, NodeHealthSample]:
        """Последний sample для каждой ноды (или для подмножества `node_ids`).

        Реализовано через distinct on (node_id, ts desc) — Postgres-specific,
        но это единственный consumer и проект Postgres-only в проде.
        """
        ids: list[int] | None = None
        if node_ids is not None:
            ids = [int(x) for x in node_ids]
            if not ids:
                return {}

        stmt = (
            select(NodeHealthSample)
            .distinct(NodeHealthSample.node_id)
            .order_by(
                NodeHealthSample.node_id,
                NodeHealthSample.ts.desc(),
                NodeHealthSample.id.desc(),
            )
        )
        if ids is not None:
            stmt = stmt.where(NodeHealthSample.node_id.in_(ids))

        res = await self.session.execute(stmt)
        return {row.node_id: row for row in res.scalars().all()}

    async def range_for_node(
        self,
        node_id: int,
        *,
        since: datetime,
        until: datetime | None = None,
        bucket_seconds: int | None = None,
    ) -> list[NodeHealthRangePoint]:
        """Точки графика для ноды в окне `[since, until]`.

        При `bucket_seconds=None` — возвращает «сырьё» (без агрегации).
        При `bucket_seconds=60`/`3600` — агрегирует по date_trunc('minute'|'hour'),
        с avg по latency/users и счётчиками ok/fail.
        """
        if bucket_seconds is not None and bucket_seconds not in (60, 3600):
            raise ValueError('bucket_seconds: ожидается 60 (minute) или 3600 (hour)')

        normalized_since = _normalize_utc(since)
        normalized_until = _normalize_utc(until) or datetime.now(timezone.utc)
        if normalized_since is None:
            raise ValueError('since не может быть None')

        ok_case = case(
            (NodeHealthSample.status == NodeHealthProbeStatus.ok, 1),
            else_=0,
        )
        fail_case = case(
            (
                NodeHealthSample.status.in_(
                    [NodeHealthProbeStatus.down, NodeHealthProbeStatus.error]
                ),
                1,
            ),
            else_=0,
        )

        if bucket_seconds is None:
            res = await self.session.execute(
                select(NodeHealthSample)
                .where(
                    NodeHealthSample.node_id == int(node_id),
                    NodeHealthSample.ts >= normalized_since,
                    NodeHealthSample.ts <= normalized_until,
                )
                .order_by(NodeHealthSample.ts.asc(), NodeHealthSample.id.asc())
            )
            rows = list(res.scalars().all())
            return [
                NodeHealthRangePoint(
                    ts=row.ts,
                    latency_ms_avg=float(row.latency_ms) if row.latency_ms is not None else None,
                    users_online_avg=(
                        float(row.users_online) if row.users_online is not None else None
                    ),
                    users_total_avg=(
                        float(row.users_total) if row.users_total is not None else None
                    ),
                    ok_count=1 if row.status == NodeHealthProbeStatus.ok else 0,
                    fail_count=1
                    if row.status in (NodeHealthProbeStatus.down, NodeHealthProbeStatus.error)
                    else 0,
                )
                for row in rows
            ]

        trunc_unit = 'minute' if bucket_seconds == 60 else 'hour'
        bucket_expr = func.date_trunc(trunc_unit, NodeHealthSample.ts).label('bucket_ts')

        res = await self.session.execute(
            select(
                bucket_expr,
                func.avg(NodeHealthSample.latency_ms).label('latency_avg'),
                func.avg(NodeHealthSample.users_online).label('online_avg'),
                func.avg(NodeHealthSample.users_total).label('total_avg'),
                func.sum(ok_case).label('ok_count'),
                func.sum(fail_case).label('fail_count'),
            )
            .where(
                NodeHealthSample.node_id == int(node_id),
                NodeHealthSample.ts >= normalized_since,
                NodeHealthSample.ts <= normalized_until,
            )
            .group_by(bucket_expr)
            .order_by(bucket_expr.asc())
        )

        out: list[NodeHealthRangePoint] = []
        for row in res.all():
            latency_avg = row.latency_avg
            online_avg = row.online_avg
            total_avg = row.total_avg
            out.append(
                NodeHealthRangePoint(
                    ts=row.bucket_ts,
                    latency_ms_avg=float(latency_avg) if latency_avg is not None else None,
                    users_online_avg=float(online_avg) if online_avg is not None else None,
                    users_total_avg=float(total_avg) if total_avg is not None else None,
                    ok_count=int(row.ok_count or 0),
                    fail_count=int(row.fail_count or 0),
                )
            )
        return out

    async def cleanup_older_than(self, cutoff: datetime) -> int:
        """Удалить замеры старше `cutoff`. Возвращает количество удалённых строк."""
        normalized = _normalize_utc(cutoff)
        if normalized is None:
            raise ValueError('cutoff не может быть None')
        res = await self.session.execute(
            delete(NodeHealthSample).where(NodeHealthSample.ts < normalized)
        )
        return int(res.rowcount or 0)

    async def update_node_denorm(
        self,
        node: NodeRegistry,
        *,
        latency_ms: int | None,
        users_online: int | None,
        users_total: int | None,
        probed_at: datetime,
        success: bool,
    ) -> NodeRegistry:
        """Записать денорм last_* поля + обновить `consecutive_fail_count`.

        Принимает уже-загруженный `NodeRegistry` (вызывающий сам берёт
        SELECT FOR UPDATE если нужно). Возвращает то же row для чейнинга.
        """
        node.last_latency_ms = _normalize_non_negative_int(latency_ms)
        node.last_users_online = _normalize_non_negative_int(users_online)
        node.last_users_total = _normalize_non_negative_int(users_total)
        node.last_probe_at = _normalize_utc(probed_at) or datetime.now(timezone.utc)
        if success:
            node.consecutive_fail_count = 0
        else:
            node.consecutive_fail_count = int(node.consecutive_fail_count or 0) + 1
        await self.session.flush()
        return node
