from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.observability.logging import correlation_id_var


def _derive_correlation_id(event: TelegramObject) -> str:
    update_id = getattr(event, 'update_id', None)
    if update_id is not None:
        return f'tg-update-{update_id}'
    message_id = getattr(event, 'message_id', None)
    if message_id is not None:
        return f'tg-msg-{message_id}'
    cb_id = getattr(event, 'id', None)
    if cb_id is not None:
        return f'tg-cb-{cb_id}'
    return f'tg-{uuid.uuid4().hex[:12]}'


class CorrelationIdMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        correlation_id = _derive_correlation_id(event)
        token = correlation_id_var.set(correlation_id)
        data['correlation_id'] = correlation_id
        try:
            return await handler(event, data)
        finally:
            correlation_id_var.reset(token)
