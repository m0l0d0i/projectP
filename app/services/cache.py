from __future__ import annotations

import copy
import json
import logging
import math
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

try:
    from redis.asyncio import Redis
except Exception:  # pragma: no cover
    Redis = None  # type: ignore

logger = logging.getLogger(__name__)


# Fallback storage caps: keep memory usage bounded if Redis is unavailable for
# extended periods. Values are intentionally generous; they protect against a
# pathological flood, not normal operation.
_BUCKETS_MAX_ENTRIES = 50_000
_BUCKETS_TTL_SECONDS = 3600.0  # buckets older than this are evicted on access
_USER_CACHE_MAX_ENTRIES = 20_000


@dataclass(slots=True)
class _Bucket:
    timestamps: deque[float] = field(default_factory=deque)
    blocked_until: float = 0.0
    last_touched: float = field(default_factory=time.monotonic)


class CacheService:
    LUA_RATE_LIMIT = """
    local key = KEYS[1]
    local block_key = KEYS[2]
    local now = tonumber(ARGV[1])
    local window = tonumber(ARGV[2])
    local limit = tonumber(ARGV[3])
    local member = ARGV[4]
    local block_seconds = tonumber(ARGV[5])
    local min_interval = tonumber(ARGV[6])

    local blocked_ttl = redis.call('TTL', block_key)
    if blocked_ttl and blocked_ttl > 0 then
        return -(blocked_ttl * 1000)
    end

    redis.call('ZREMRANGEBYSCORE', key, 0, now - window)

    local latest = redis.call('ZREVRANGE', key, 0, 0, 'WITHSCORES')
    if min_interval > 0 and latest and #latest >= 2 then
        local latest_ts = tonumber(latest[2])
        local delta = now - latest_ts
        if delta < min_interval then
            return -(min_interval - delta)
        end
    end

    local count = redis.call('ZCARD', key)
    if count >= limit then
        redis.call('SETEX', block_key, block_seconds, '1')
        return -(block_seconds * 1000)
    end

    redis.call('ZADD', key, now, member)
    redis.call('EXPIRE', key, math.max(math.ceil(window / 1000), block_seconds) + 5)
    return 1
    """

    def __init__(self, redis_url: str | None, prefix: str = 'vpn_bot') -> None:
        self.redis_url = redis_url
        self.prefix = prefix
        self._redis = Redis.from_url(redis_url, decode_responses=True) if redis_url and Redis is not None else None
        # OrderedDict keeps LRU order; we evict oldest when over capacity.
        self._buckets: OrderedDict[tuple[int, str], _Bucket] = OrderedDict()
        self._lock = Lock()
        self._memory_user_cache: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()

    @property
    def redis(self):
        return self._redis

    @property
    def client(self):
        return self._redis

    async def close(self) -> None:
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:
                logger.exception('Failed to close Redis client')
        with self._lock:
            self._buckets.clear()
            self._memory_user_cache.clear()

    def _key(self, *parts: object) -> str:
        return ':'.join([self.prefix, *[str(p) for p in parts]])

    async def ping(self) -> bool:
        if self._redis is not None:
            try:
                result = await self._redis.ping()
                return bool(result)
            except Exception:
                logger.exception('Redis ping failed')
                return False
        return True

    def _memory_get_json(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._memory_user_cache.get(key)
            if not value:
                return None
            expires_at, payload = value
            if expires_at < time.monotonic():
                self._memory_user_cache.pop(key, None)
                return None
            # Touch: move to end so eviction takes from the actually-old entries.
            self._memory_user_cache.move_to_end(key)
            return copy.deepcopy(payload)

    def _memory_set_json(self, key: str, value: dict[str, Any], ttl_seconds: int) -> None:
        with self._lock:
            self._memory_user_cache[key] = (
                time.monotonic() + max(1, ttl_seconds),
                copy.deepcopy(value),
            )
            self._memory_user_cache.move_to_end(key)
            # Bound capacity: drop oldest entries first (LRU).
            while len(self._memory_user_cache) > _USER_CACHE_MAX_ENTRIES:
                self._memory_user_cache.popitem(last=False)

    def _memory_delete(self, key: str) -> None:
        with self._lock:
            self._memory_user_cache.pop(key, None)

    def _evict_stale_buckets_locked(self, now: float) -> None:
        """Drop buckets unused longer than TTL or trim by LRU when over capacity.

        Caller must already hold ``self._lock``.
        """
        if not self._buckets:
            return
        # Stale eviction: drop entries whose blocked window has expired AND
        # which were not touched within TTL. Walk from oldest while condition holds.
        cutoff = now - _BUCKETS_TTL_SECONDS
        for cache_key in list(self._buckets.keys())[:64]:  # bounded scan per call
            bucket = self._buckets[cache_key]
            if bucket.last_touched <= cutoff and bucket.blocked_until <= now:
                self._buckets.pop(cache_key, None)
            else:
                break
        # Hard cap: if still over, drop oldest unconditionally.
        while len(self._buckets) > _BUCKETS_MAX_ENTRIES:
            self._buckets.popitem(last=False)

    async def get_json(self, key: str) -> dict[str, Any] | None:
        if self._redis is not None:
            try:
                raw = await self._redis.get(self._key(key))
                return json.loads(raw) if raw else None
            except Exception:
                logger.exception('Redis get_json failed for key=%s, falling back to memory cache', key)
        return self._memory_get_json(key)

    async def set_json(self, key: str, value: dict[str, Any], ttl_seconds: int) -> None:
        if self._redis is not None:
            try:
                await self._redis.set(
                    self._key(key),
                    json.dumps(value, ensure_ascii=False, default=str),
                    ex=max(1, ttl_seconds),
                )
                return
            except Exception:
                logger.exception('Redis set_json failed for key=%s, falling back to memory cache', key)
        self._memory_set_json(key, value, ttl_seconds)

    async def delete(self, key: str) -> None:
        if self._redis is not None:
            try:
                await self._redis.delete(self._key(key))
            except Exception:
                logger.exception('Redis delete failed for key=%s, falling back to memory cache', key)

        # Always clean memory fallback too, so mixed-mode runtime stays consistent.
        self._memory_delete(key)

    async def check_rate_limit(
        self,
        user_id: int,
        event_kind: str,
        *,
        limit: int,
        window_seconds: int,
        block_seconds: int,
        min_interval_seconds: float = 0.0,
    ) -> tuple[bool, int]:
        if self._redis is not None:
            try:
                return await self._check_rate_limit_redis(
                    user_id,
                    event_kind,
                    limit,
                    window_seconds,
                    block_seconds,
                    min_interval_seconds,
                )
            except Exception:
                logger.exception(
                    'Redis rate-limit check failed for user_id=%s event_kind=%s, falling back to memory',
                    user_id,
                    event_kind,
                )
        return self._check_rate_limit_memory(
            user_id,
            event_kind,
            limit,
            window_seconds,
            block_seconds,
            min_interval_seconds,
        )

    async def _check_rate_limit_redis(
        self,
        user_id: int,
        event_kind: str,
        limit: int,
        window_seconds: int,
        block_seconds: int,
        min_interval_seconds: float,
    ) -> tuple[bool, int]:
        assert self._redis is not None
        key = self._key('rl', event_kind, user_id)
        block_key = self._key('rl_block', event_kind, user_id)
        now_ms = int(time.time() * 1000)
        window_ms = window_seconds * 1000
        min_interval_ms = int(min_interval_seconds * 1000)
        member = f'{now_ms}:{uuid.uuid4().hex}'
        result = await self._redis.eval(
            self.LUA_RATE_LIMIT,
            2,
            key,
            block_key,
            now_ms,
            window_ms,
            limit,
            member,
            block_seconds,
            min_interval_ms,
        )
        if int(result) == 1:
            return True, 0
        retry_after = max(1, int((abs(int(result)) + 999) / 1000))
        return False, retry_after

    def _check_rate_limit_memory(
        self,
        user_id: int,
        event_kind: str,
        limit: int,
        window_seconds: int,
        block_seconds: int,
        min_interval_seconds: float,
    ) -> tuple[bool, int]:
        now = time.monotonic()
        key = (user_id, event_kind)

        with self._lock:
            self._evict_stale_buckets_locked(now)

            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket()
                self._buckets[key] = bucket
            else:
                # LRU touch
                self._buckets.move_to_end(key)
            bucket.last_touched = now

            if bucket.blocked_until > now:
                remaining = bucket.blocked_until - now
                return False, max(1, math.ceil(remaining))

            if bucket.blocked_until:
                bucket.blocked_until = 0.0

            # Keep memory-window behavior aligned with Redis ZREMRANGEBYSCORE <= now-window.
            while bucket.timestamps and now - bucket.timestamps[0] >= window_seconds:
                bucket.timestamps.popleft()

            if bucket.timestamps and min_interval_seconds > 0:
                delta = now - bucket.timestamps[-1]
                if delta < min_interval_seconds:
                    remaining = min_interval_seconds - delta
                    return False, max(1, math.ceil(remaining))

            if len(bucket.timestamps) >= limit:
                bucket.blocked_until = now + block_seconds
                bucket.timestamps.clear()
                return False, max(1, int(block_seconds))

            bucket.timestamps.append(now)
            return True, 0
