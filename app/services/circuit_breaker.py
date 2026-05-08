from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from enum import Enum

logger = logging.getLogger(__name__)


def _default_is_failure(exc: BaseException) -> bool:
    return True


class CircuitState(str, Enum):
    closed = 'closed'
    open = 'open'
    half_open = 'half_open'


class CircuitBreakerOpenError(Exception):
    """Raised when the breaker is open and is fast-failing the call."""

    def __init__(self, name: str, retry_after: float) -> None:
        super().__init__(f'Circuit breaker {name!r} is open; retry in {retry_after:.1f}s')
        self.name = name
        self.retry_after = retry_after


class CircuitBreaker:
    """Async-safe circuit breaker.

    States:
      - closed: requests pass; consecutive failures are counted, on
        `failure_threshold` we OPEN.
      - open: requests fast-fail with `CircuitBreakerOpenError` for
        `cooldown_seconds`. After cooldown, we move to HALF_OPEN.
      - half_open: a single probe is allowed. Success -> CLOSED;
        failure -> OPEN with reset cooldown.
    """

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 5,
        cooldown_seconds: float = 30.0,
        is_failure: Callable[[BaseException], bool] = _default_is_failure,
    ) -> None:
        self.name = name
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_seconds = max(1.0, float(cooldown_seconds))
        self._is_failure = is_failure

        self._state: CircuitState = CircuitState.closed
        self._consecutive_failures = 0
        self._opened_at: float = 0.0
        self._half_open_in_flight = False
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        return self._state

    async def _on_before_call(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._state is CircuitState.open:
                elapsed = now - self._opened_at
                if elapsed < self.cooldown_seconds:
                    raise CircuitBreakerOpenError(self.name, self.cooldown_seconds - elapsed)
                self._state = CircuitState.half_open
                self._half_open_in_flight = False
                logger.warning('Circuit breaker %s -> half_open (probing)', self.name)

            if self._state is CircuitState.half_open:
                if self._half_open_in_flight:
                    raise CircuitBreakerOpenError(self.name, self.cooldown_seconds)
                self._half_open_in_flight = True

    async def _on_success(self) -> None:
        async with self._lock:
            if self._state is not CircuitState.closed:
                logger.warning('Circuit breaker %s -> closed (probe succeeded)', self.name)
            self._state = CircuitState.closed
            self._consecutive_failures = 0
            self._half_open_in_flight = False

    async def _on_failure(self) -> None:
        async with self._lock:
            self._consecutive_failures += 1
            if self._state is CircuitState.half_open:
                self._state = CircuitState.open
                self._opened_at = time.monotonic()
                self._half_open_in_flight = False
                logger.error(
                    'Circuit breaker %s -> open (probe failed; cooldown %.1fs)',
                    self.name,
                    self.cooldown_seconds,
                )
                return
            if self._consecutive_failures >= self.failure_threshold:
                self._state = CircuitState.open
                self._opened_at = time.monotonic()
                logger.error(
                    'Circuit breaker %s -> open (%d consecutive failures; cooldown %.1fs)',
                    self.name,
                    self._consecutive_failures,
                    self.cooldown_seconds,
                )

    async def __aenter__(self) -> 'CircuitBreaker':
        await self._on_before_call()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if exc is None:
            await self._on_success()
        elif self._is_failure(exc):
            await self._on_failure()
        else:
            await self._on_success()
        return False
