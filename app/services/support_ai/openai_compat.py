"""OpenAI-совместимый chat-completions провайдер (FEA-C32).

Покрывает любые backend'ы, у которых API повторяет
`POST {base_url}/v1/chat/completions` с заголовком
`Authorization: Bearer <api_key>` и форматом OpenAI: Together / Groq /
локальный vLLM / Ollama (через openai-compatibility) / OpenRouter / и т.п.

Для DeepSeek используется отдельный thin-wrapper `DeepSeekProvider`,
который предзаполняет `api_base_url`, но логика — та же.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urljoin

import httpx

from app.services.support_ai.base import (
    LLMMessage,
    LLMProvider,
    LLMProviderError,
    LLMResponse,
)


class OpenAICompatProvider(LLMProvider):
    """OpenAI-compatible /v1/chat/completions клиент (httpx)."""

    def __init__(
        self,
        *,
        api_base_url: str,
        api_key: str,
        model_name: str,
    ) -> None:
        if not api_base_url:
            raise ValueError('api_base_url не может быть пустым')
        if not api_key:
            raise ValueError('api_key не может быть пустым')
        if not model_name:
            raise ValueError('model_name не может быть пустым')
        self._api_base_url = api_base_url.rstrip('/')
        self._api_key = api_key
        self._model_name = model_name

    def _build_url(self) -> str:
        # Если в base_url уже есть `/v1` — не дублируем; иначе добавляем.
        # Это позволяет указывать как `https://api.deepseek.com`, так и
        # `https://api.deepseek.com/v1` без сюрпризов.
        if self._api_base_url.endswith('/v1'):
            return f'{self._api_base_url}/chat/completions'
        return urljoin(f'{self._api_base_url}/', 'v1/chat/completions')

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float,
        max_tokens: int,
        timeout_seconds: float = 30.0,
    ) -> LLMResponse:
        if not messages:
            raise ValueError('messages не может быть пустым')

        url = self._build_url()
        payload: dict[str, Any] = {
            'model': self._model_name,
            'messages': list(messages),
            'temperature': float(temperature),
            'max_tokens': int(max_tokens),
            'stream': False,
        }
        headers = {
            'Authorization': f'Bearer {self._api_key}',
            'Content-Type': 'application/json',
        }

        started_at = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                response = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise LLMProviderError(
                f'HTTP error: {exc.__class__.__name__}: {exc}',
                status_code=None,
                provider_message=str(exc),
            ) from exc

        latency_ms = int((time.monotonic() - started_at) * 1000)

        if response.status_code >= 400:
            # Попробуем достать сообщение об ошибке из тела (OpenAI/DeepSeek
            # возвращают `{"error":{"message":"..."}}`); для не-JSON
            # сохраним сырой text.
            provider_message: str | None
            try:
                err_body = response.json()
                provider_message = (
                    err_body.get('error', {}).get('message')
                    if isinstance(err_body, dict)
                    else None
                )
            except Exception:  # noqa: BLE001
                provider_message = response.text[:500]
            raise LLMProviderError(
                f'LLM HTTP {response.status_code}',
                status_code=response.status_code,
                provider_message=provider_message,
            )

        try:
            body = response.json()
        except Exception as exc:  # noqa: BLE001
            raise LLMProviderError(
                'LLM ответ не является валидным JSON',
                status_code=response.status_code,
                provider_message=response.text[:500],
            ) from exc

        return _parse_chat_completion(body, fallback_model=self._model_name, latency_ms=latency_ms)


def _parse_chat_completion(
    body: dict[str, Any],
    *,
    fallback_model: str,
    latency_ms: int,
) -> LLMResponse:
    choices = body.get('choices') or []
    if not choices:
        raise LLMProviderError(
            'LLM вернул пустой choices',
            provider_message=str(body)[:500],
        )
    first = choices[0]
    message = first.get('message') if isinstance(first, dict) else None
    if not isinstance(message, dict):
        raise LLMProviderError(
            'LLM choice не содержит message',
            provider_message=str(first)[:500],
        )
    content = message.get('content') or ''
    if not isinstance(content, str) or not content.strip():
        raise LLMProviderError(
            'LLM вернул пустой content',
            provider_message=str(message)[:500],
        )

    usage = body.get('usage') or {}
    tokens_in = int(usage.get('prompt_tokens', 0) or 0)
    tokens_out = int(usage.get('completion_tokens', 0) or 0)
    model = body.get('model') or fallback_model

    return LLMResponse(
        text=content.strip(),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        model=str(model),
        latency_ms=latency_ms,
    )
