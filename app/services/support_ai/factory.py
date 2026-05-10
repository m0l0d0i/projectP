"""Фабрика LLM-провайдеров по записи `LLMConfig` (FEA-C32).

Расшифровывает `api_key_encrypted` через `crypto.decrypt_api_key` и
строит соответствующий `LLMProvider` по `provider` enum-значению.
Вызывающие хендлеры (тест соединения, генерация ответа) получают
готовый объект и работают через единый Protocol-интерфейс.
"""

from __future__ import annotations

from app.db.models import LLMConfig, LLMProviderKind
from app.services.support_ai.base import LLMProvider
from app.services.support_ai.crypto import decrypt_api_key
from app.services.support_ai.deepseek import DeepSeekProvider
from app.services.support_ai.openai_compat import OpenAICompatProvider


def build_provider(config: LLMConfig) -> LLMProvider:
    """Построить провайдер по записи LLMConfig.

    Поднимает `LLMSecretsKeyError`, если api_key не расшифровывается;
    `ValueError` — если provider неизвестен (защита от рассинхрона
    enum'а БД и кода после downgrade migrations).
    """
    api_key = decrypt_api_key(config.api_key_encrypted)

    if config.provider is LLMProviderKind.deepseek:
        return DeepSeekProvider(
            api_key=api_key,
            api_base_url=config.api_base_url,
            model_name=config.model_name,
        )
    if config.provider is LLMProviderKind.openai_compat:
        return OpenAICompatProvider(
            api_base_url=config.api_base_url,
            api_key=api_key,
            model_name=config.model_name,
        )
    raise ValueError(f'Неизвестный LLM provider: {config.provider!r}')
