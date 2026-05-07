from __future__ import annotations

import hashlib
import time
from typing import Any

INVOICE_IDEMPOTENCY_BUCKET_SECONDS = 60
INVOICE_IDEMPOTENCY_KEY_VERSION = 'v1'


def _serialize_extras(extras: dict[str, Any] | None) -> str:
    if not extras:
        return ''
    return '|'.join(f'{k}={extras[k]}' for k in sorted(extras))


def build_invoice_idempotency_key(
    *,
    tg_id: int,
    purpose: str,
    code: str,
    units: int | str,
    extras: dict[str, Any] | None = None,
    ts: float | None = None,
    bucket_seconds: int = INVOICE_IDEMPOTENCY_BUCKET_SECONDS,
) -> str:
    bucket = int((ts if ts is not None else time.time()) // bucket_seconds)
    fingerprint = '|'.join(
        (
            INVOICE_IDEMPOTENCY_KEY_VERSION,
            str(tg_id),
            purpose,
            str(code),
            str(units),
            _serialize_extras(extras),
            str(bucket),
        )
    )
    return hashlib.sha256(fingerprint.encode('utf-8')).hexdigest()
