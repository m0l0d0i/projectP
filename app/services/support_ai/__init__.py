"""Support-AI: pluggable LLM-провайдеры для генерации черновиков ответов
саппорта (FEA-C32).

Public surface:
* `LLMProvider` — Protocol; реализации: `DeepSeekProvider`,
  `OpenAICompatProvider` (через httpx).
* `LLMResponse` — dataclass с `text` + token usage + latency.
* `build_provider(config)` — фабрика по `LLMConfig` из БД.
* `mask_pii(text)` — нормализация PII перед отправкой в LLM.
* `encrypt_api_key`/`decrypt_api_key` — Fernet wrapper для api_key.

Используется UI `/admin/support-ai/` (FEA-C32 #2) и кнопкой генерации
ответа на странице тикета `/admin/tickets/{id}` (FEA-C32 #3).
"""

from __future__ import annotations

from app.services.support_ai.base import (
    LLMProvider,
    LLMProviderError,
    LLMResponse,
)
from app.services.support_ai.crypto import (
    LLMSecretsKeyError,
    decrypt_api_key,
    encrypt_api_key,
    mask_api_key_preview,
)
from app.services.support_ai.deepseek import DEEPSEEK_DEFAULT_API_BASE_URL
from app.services.support_ai.factory import build_provider
from app.services.support_ai.openai_compat import OpenAICompatProvider
from app.services.support_ai.pii import mask_pii

__all__ = (
    'DEEPSEEK_DEFAULT_API_BASE_URL',
    'LLMProvider',
    'LLMProviderError',
    'LLMResponse',
    'LLMSecretsKeyError',
    'OpenAICompatProvider',
    'build_provider',
    'decrypt_api_key',
    'encrypt_api_key',
    'mask_api_key_preview',
    'mask_pii',
)
