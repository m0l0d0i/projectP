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
