from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware


class ServicesMiddleware(BaseMiddleware):
    def __init__(self, **services: Any) -> None:
        self.services = services

    async def __call__(self, handler: Callable[[Any, dict[str, Any]], Awaitable[Any]], event: Any, data: dict[str, Any]) -> Any:
        data.update(self.services)
        return await handler(event, data)
