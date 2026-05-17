from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

BOT_UP = Gauge('vpn_bot_up', 'Bot process health')
TELEGRAM_UPDATES = Counter('vpn_bot_telegram_updates_total', 'Handled telegram updates', ['update_type'])
HANDLER_ERRORS = Counter('vpn_bot_handler_errors_total', 'Unhandled handler errors')
PAYMENT_REQUESTS = Counter('vpn_bot_payment_requests_total', 'Payment provider requests', ['provider', 'result'])
MARZBAN_REQUESTS = Counter('vpn_bot_marzban_requests_total', 'Marzban API requests', ['result'])
REQUEST_LATENCY = Histogram('vpn_bot_http_request_seconds', 'Internal HTTP endpoint latency', ['path'])
ACTIVE_TICKETS = Gauge('vpn_bot_support_open_tickets', 'Open support tickets')


def metrics_response_text() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST

PAYMENTS_CREATED = Counter('vpn_bot_payments_created_total', 'Created invoices', ['purpose'])
PAYMENTS_CONSUMED = Counter('vpn_bot_payments_consumed_total', 'Consumed invoices', ['purpose'])
PAYMENTS_FAILED = Counter('vpn_bot_payments_failed_total', 'Failed invoice processing', ['reason'])
SUPPORT_TICKETS_OPENED = Counter('vpn_bot_support_tickets_opened_total', 'Support tickets opened')
SUPPORT_TICKETS_CLOSED = Counter('vpn_bot_support_tickets_closed_total', 'Support tickets closed', ['reason'])
MARZBAN_ERRORS = Counter('vpn_bot_marzban_errors_total', 'Marzban API errors', ['kind'])
EXTERNAL_API_LATENCY = Histogram('vpn_bot_external_api_latency_seconds', 'External API latency', ['service', 'operation'])

NOTIFICATIONS_SENT = Counter(
    'vpn_bot_notifications_sent_total',
    'FEA-NOTIF: уведомления, поставленные в outbox',
    ['code', 'status'],
)
NOTIFICATIONS_BLOCKED = Counter(
    'vpn_bot_notifications_blocked_total',
    'FEA-NOTIF: пропуски/ошибки рендера в NotificationDispatcher',
    ['code', 'reason'],
)

SUPPORT_AI_CALLS = Counter(
    'vpn_bot_support_ai_calls_total',
    'FEA-C32: вызовы LLM для генерации черновика ответа саппорта',
    ['provider', 'status'],
)

# FEA-ADMIN-NODE-MONITOR (D12): per-node live-метрики, обновляются
# `NodeProbeService` на каждом тике scheduler-job `probe_nodes_health`.
NODE_LATENCY_SECONDS = Gauge(
    'vpn_bot_node_latency_seconds',
    'Последний замер latency probe-вызова /api/nodes для ноды (секунды)',
    ['node'],
)
NODE_USERS_ONLINE = Gauge(
    'vpn_bot_node_users_online',
    'Текущее число активных пользователей панели Marzban (per-node агрегат, '
    'обновляется только для default-ноды — Marzban API не даёт breakdown)',
    ['node'],
)
NODE_HEALTH = Gauge(
    'vpn_bot_node_health',
    'Текущее состояние ноды: 1 — ok, 0 — degraded/down/error (последний probe)',
    ['node'],
)

# OPS-7: scheduler-lag SLO. APScheduler даёт scheduled_run_time job'а;
# фактическое время — момент EVENT_JOB_SUBMITTED. Гистограмма задержек
# (per job_id) позволяет в Grafana увидеть P99 и упустившие SLA задачи.
SCHEDULER_JOB_LAG_SECONDS = Histogram(
    'vpn_bot_scheduler_job_lag_seconds',
    'Задержка между запланированным и фактическим запуском scheduler-job (секунды)',
    ['job_id'],
    buckets=(0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0),
)
SCHEDULER_JOB_MISFIRES = Counter(
    'vpn_bot_scheduler_job_misfires_total',
    'Job не запустился в свой scheduled_run_time (misfire_grace_time превышен)',
    ['job_id'],
)
SCHEDULER_JOB_ERRORS = Counter(
    'vpn_bot_scheduler_job_errors_total',
    'Job завершился с исключением',
    ['job_id'],
)
SCHEDULER_RUNNING = Gauge(
    'vpn_bot_scheduler_running',
    'Scheduler запущен в этом процессе (1 = да, 0 = нет/лидерство потеряно)',
)


def notification_counters_snapshot() -> dict[str, dict[str, float]]:
    """Снимок in-process Prometheus-counter'ов NotificationDispatcher.

    Возвращает `{code: {'sent_ok': N, 'sent_fallback': N,
    'blocked_<reason>': N}}`. Значения — cumulative с момента запуска
    процесса (prometheus_client не хранит time-windowed данные).
    Для 7д/30д нужно опрашивать внешний Prometheus с `increase()`
    или агрегировать по `outbox_messages.created_at`.
    """
    snapshot: dict[str, dict[str, float]] = {}

    for metric in NOTIFICATIONS_SENT.collect():
        for sample in metric.samples:
            if not sample.name.endswith('_total'):
                continue
            code = sample.labels.get('code')
            status = sample.labels.get('status')
            if not code or not status:
                continue
            snapshot.setdefault(code, {})[f'sent_{status}'] = sample.value

    for metric in NOTIFICATIONS_BLOCKED.collect():
        for sample in metric.samples:
            if not sample.name.endswith('_total'):
                continue
            code = sample.labels.get('code')
            reason = sample.labels.get('reason')
            if not code or not reason:
                continue
            snapshot.setdefault(code, {})[f'blocked_{reason}'] = sample.value

    return snapshot
