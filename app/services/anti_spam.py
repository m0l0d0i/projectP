from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable

from app.db.repositories import AppSettingsRepository
from app.services.cache import CacheService
from app.utils.runtime_settings import effective_bool_from_row, effective_int_from_row

if TYPE_CHECKING:
    from collections.abc import AsyncContextManager

    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AntiSpamRuntimeSettings:
    enabled: bool
    message_limit: int
    message_window_seconds: int
    callback_limit: int
    callback_window_seconds: int
    block_seconds: int
    min_interval_seconds: float


class AntiSpamService:
    def __init__(
        self,
        *,
        cache: CacheService | None = None,
        session_factory: Callable[[], AsyncContextManager[AsyncSession]] | None = None,
        settings_cache_ttl_seconds: int = 30,
        enabled: bool = True,
        message_limit: int = 8,
        message_window_seconds: int = 12,
        callback_limit: int = 12,
        callback_window_seconds: int = 8,
        block_seconds: int = 10,
        min_interval_seconds: float = 1.0,
    ) -> None:
        self.cache = cache
        self.session_factory = session_factory
        self.settings_cache_ttl_seconds = max(1, int(settings_cache_ttl_seconds))

        self._fallback_settings = self._build_fallback_settings(
            enabled=enabled,
            message_limit=message_limit,
            message_window_seconds=message_window_seconds,
            callback_limit=callback_limit,
            callback_window_seconds=callback_window_seconds,
            block_seconds=block_seconds,
            min_interval_seconds=min_interval_seconds,
        )

        self._runtime_settings_cache: AntiSpamRuntimeSettings | None = None
        self._runtime_settings_cached_until: datetime | None = None

    @classmethod
    def _build_fallback_settings(
        cls,
        *,
        enabled: bool,
        message_limit: int,
        message_window_seconds: int,
        callback_limit: int,
        callback_window_seconds: int,
        block_seconds: int,
        min_interval_seconds: float | Decimal | int | str,
    ) -> AntiSpamRuntimeSettings:
        return cls._normalize_runtime_settings(
            enabled=enabled,
            message_limit=message_limit,
            message_window_seconds=message_window_seconds,
            callback_limit=callback_limit,
            callback_window_seconds=callback_window_seconds,
            block_seconds=block_seconds,
            min_interval_seconds=min_interval_seconds,
        )

    @staticmethod
    def _normalize_runtime_settings(
        *,
        enabled: bool,
        message_limit: int,
        message_window_seconds: int,
        callback_limit: int,
        callback_window_seconds: int,
        block_seconds: int,
        min_interval_seconds: float | Decimal | int | str,
    ) -> AntiSpamRuntimeSettings:
        return AntiSpamRuntimeSettings(
            enabled=bool(enabled),
            message_limit=max(1, int(message_limit)),
            message_window_seconds=max(1, int(message_window_seconds)),
            callback_limit=max(1, int(callback_limit)),
            callback_window_seconds=max(1, int(callback_window_seconds)),
            block_seconds=max(1, int(block_seconds)),
            min_interval_seconds=max(0.0, float(min_interval_seconds)),
        )

    def _cache_runtime_settings(self, settings: AntiSpamRuntimeSettings) -> AntiSpamRuntimeSettings:
        self._runtime_settings_cache = settings
        self._runtime_settings_cached_until = datetime.now(timezone.utc) + timedelta(
            seconds=self.settings_cache_ttl_seconds
        )
        return settings

    async def _load_runtime_settings_from_db(self) -> AntiSpamRuntimeSettings | None:
        if self.session_factory is None:
            return None

        try:
            async with self.session_factory() as session:
                repo = AppSettingsRepository(session)
                row = await repo.get()

                return self._normalize_runtime_settings(
                    enabled=effective_bool_from_row(row, 'anti_spam_enabled', self._fallback_settings.enabled),
                    message_limit=effective_int_from_row(row, 'anti_spam_message_limit', self._fallback_settings.message_limit, minimum=1),
                    message_window_seconds=effective_int_from_row(
                        row,
                        'anti_spam_message_window_seconds',
                        self._fallback_settings.message_window_seconds,
                        minimum=1,
                    ),
                    callback_limit=effective_int_from_row(row, 'anti_spam_callback_limit', self._fallback_settings.callback_limit, minimum=1),
                    callback_window_seconds=effective_int_from_row(
                        row,
                        'anti_spam_callback_window_seconds',
                        self._fallback_settings.callback_window_seconds,
                        minimum=1,
                    ),
                    block_seconds=effective_int_from_row(row, 'anti_spam_block_seconds', self._fallback_settings.block_seconds, minimum=1),
                    min_interval_seconds=(
                        self._fallback_settings.min_interval_seconds
                        if row is None
                        else getattr(row, 'anti_spam_min_interval_seconds', self._fallback_settings.min_interval_seconds)
                    ),
                )
        except Exception:
            logger.exception('Failed to load anti-spam runtime settings from AppSettings; using fallback settings')
            return None

    async def get_runtime_settings(self, *, force_refresh: bool = False) -> AntiSpamRuntimeSettings:
        now = datetime.now(timezone.utc)

        if (
            not force_refresh
            and self._runtime_settings_cache is not None
            and self._runtime_settings_cached_until is not None
            and now < self._runtime_settings_cached_until
        ):
            return self._runtime_settings_cache

        runtime_settings = await self._load_runtime_settings_from_db()
        if runtime_settings is None:
            runtime_settings = self._fallback_settings

        return self._cache_runtime_settings(runtime_settings)

    def invalidate_runtime_settings_cache(self) -> None:
        self._runtime_settings_cache = None
        self._runtime_settings_cached_until = None

    async def _ensure_cache(self) -> CacheService:
        if self.cache is not None:
            return self.cache

        from app.services.cache import CacheService

        self.cache = CacheService(None)
        return self.cache

    async def check(self, user_id: int, event_kind: str) -> tuple[bool, int]:
        runtime_settings = await self.get_runtime_settings()

        if not runtime_settings.enabled:
            return True, 0

        if event_kind == 'callback':
            limit = runtime_settings.callback_limit
            window = runtime_settings.callback_window_seconds
        else:
            limit = runtime_settings.message_limit
            window = runtime_settings.message_window_seconds

        cache = await self._ensure_cache()
        return await cache.check_rate_limit(
            user_id,
            event_kind,
            limit=limit,
            window_seconds=window,
            block_seconds=runtime_settings.block_seconds,
            min_interval_seconds=runtime_settings.min_interval_seconds,
        )