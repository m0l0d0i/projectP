"""Базовые типы для support-AI провайдеров (FEA-C32).

Provider — Protocol с одним методом `complete(messages)`. Это даёт
свободу подменять backend (DeepSeek / OpenAI-compat / локальный vLLM)
без изменения вызывающего кода в UI и сервисах.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict


class LLMMessage(TypedDict):
    role: Literal['system', 'user', 'assistant']
    content: str


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """Результат вызова LLM.

    `text` — сгенерированный текст ответа (assistant content).
    `tokens_in` / `tokens_out` — usage из ответа провайдера (для метрик
    и накопления `LLMConfig.usage_total_*`); если провайдер не вернул
    usage — оба равны 0.
    `model` — фактическое имя модели из ответа (может отличаться от
    запрошенного, например, у Together при alias-router'ах).
    `latency_ms` — wall-clock латенция вызова.
    """

    text: str
    tokens_in: int
    tokens_out: int
    model: str
    latency_ms: int


class LLMProviderError(RuntimeError):
    """LLM-вызов завершился ошибкой (HTTP ≥ 400, network error, parse error).

    Хранит `status_code` (HTTP) и `provider_message` для UI/audit. Не
    наследуется от httpx.HTTPError, чтобы вызывающий код не зависел
    от конкретного backend'а.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        provider_message: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.provider_message = provider_message


class LLMProvider(Protocol):
    """Унифицированный интерфейс LLM-провайдера.

    `complete(messages, ...)` принимает chat-completion message list
    (system + user + (optional) few-shot assistant/user пары) и
    возвращает `LLMResponse`. Провайдер сам обрабатывает auth,
    timeouts, ретраи (если нужно) и нормализацию ответа.
    """

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float,
        max_tokens: int,
        timeout_seconds: float = 30.0,
    ) -> LLMResponse:
        ...
