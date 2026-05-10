"""DeepSeek провайдер для support-AI (FEA-C32, default по D7).

DeepSeek имеет OpenAI-совместимый API на api.deepseek.com — поэтому
наследуемся от OpenAICompatProvider и только подставляем default
`api_base_url`. Это позволяет UI разделить «DeepSeek-preset» (быстрый
выбор) и «openai_compat» (любой URL).
"""

from __future__ import annotations

from app.services.support_ai.openai_compat import OpenAICompatProvider

DEEPSEEK_DEFAULT_API_BASE_URL = 'https://api.deepseek.com'
DEEPSEEK_DEFAULT_MODEL = 'deepseek-chat'


class DeepSeekProvider(OpenAICompatProvider):
    """DeepSeek-обёртка над OpenAICompatProvider.

    Использует тот же endpoint `/v1/chat/completions` и тот же формат
    payload/usage. По состоянию на 2026 рекомендуем модель
    `deepseek-chat` (см. https://platform.deepseek.com/docs).
    """

    def __init__(
        self,
        *,
        api_key: str,
        api_base_url: str | None = None,
        model_name: str | None = None,
    ) -> None:
        super().__init__(
            api_base_url=(api_base_url or DEEPSEEK_DEFAULT_API_BASE_URL).rstrip('/'),
            api_key=api_key,
            model_name=model_name or DEEPSEEK_DEFAULT_MODEL,
        )
