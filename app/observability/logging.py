from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

request_id_var: ContextVar[str | None] = ContextVar('request_id', default=None)
correlation_id_var: ContextVar[str | None] = ContextVar('correlation_id', default=None)

_RESERVED_LOG_FIELDS = frozenset({
    'name', 'msg', 'args', 'levelname', 'levelno', 'pathname', 'filename',
    'module', 'exc_info', 'exc_text', 'stack_info', 'lineno', 'funcName',
    'created', 'msecs', 'relativeCreated', 'thread', 'threadName',
    'processName', 'process', 'message', 'asctime', 'taskName',
    'request_id', 'correlation_id',
})


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, 'request_id', None):
            record.request_id = request_id_var.get()
        if not getattr(record, 'correlation_id', None):
            record.correlation_id = correlation_id_var.get()
        return True


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            'ts': datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            'level': record.levelname,
            'logger': record.name,
            'msg': record.getMessage(),
        }
        request_id = getattr(record, 'request_id', None)
        if request_id:
            payload['request_id'] = request_id
        correlation_id = getattr(record, 'correlation_id', None)
        if correlation_id:
            payload['correlation_id'] = correlation_id

        if record.exc_info:
            payload['exc_info'] = self.formatException(record.exc_info)
        if record.stack_info:
            payload['stack_info'] = self.formatStack(record.stack_info)

        for key, value in record.__dict__.items():
            if key in _RESERVED_LOG_FIELDS or key.startswith('_'):
                continue
            try:
                json.dumps(value, default=str)
            except (TypeError, ValueError):
                payload[key] = repr(value)
            else:
                payload[key] = value

        return json.dumps(payload, ensure_ascii=False, default=str)
