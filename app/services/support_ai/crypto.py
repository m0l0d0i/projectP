"""Шифрование api_key для LLMConfig (FEA-C32).

Используется Fernet (AES-128-CBC + HMAC-SHA256, lib `cryptography`).
Симметричный ключ берётся из:

1. `LLM_SECRETS_KEY` env (urlsafe base64, 32 байта). Рекомендованный
   prod-вариант — генерируется отдельно и хранится в secret manager.
2. Fallback — детерминированно derive из `BOT_TOKEN` через HKDF-SHA256
   с салтом 'swoivpn.support_ai.v1'. Это позволяет не вводить новый env
   на стадии разработки/staging, но в проде стоит явно задать
   `LLM_SECRETS_KEY` (логируется warning при использовании fallback).

API:
* `encrypt_api_key(plain)` → токен (str), пригодный для DB.
* `decrypt_api_key(token)` → plain (str). Поднимает `LLMSecretsKeyError`,
  если токен повреждён или ключ неправильный (например, после ротации).
* `mask_api_key_preview(plain)` — показ безопасного превью в UI
  (`sk-...XYZ` или `not_set` для пустого).
"""

from __future__ import annotations

import base64
import logging
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.config import get_settings

logger = logging.getLogger(__name__)

_HKDF_INFO = b'swoivpn.support_ai.v1'
_HKDF_SALT = b'swoivpn.fernet.fea-c32'


class LLMSecretsKeyError(RuntimeError):
    """LLM_SECRETS_KEY не задан/некорректен или токен повреждён.

    Хендлеры, которые показывают plain api_key (тест соединения,
    реальный вызов LLM), должны обрабатывать это и предлагать
    обновить запись в /admin/support-ai/.
    """


def _resolve_fernet_key_bytes() -> tuple[bytes, bool]:
    """Возвращает (key_bytes_urlsafe_b64, is_derived_from_bot_token).

    Не кешируется здесь — кеширование делает `_get_fernet`. Идея:
    при ротации env будет автоматически подхвачена при перезапуске;
    в рантайме ключ стабилен.
    """
    settings = get_settings()
    raw = settings.llm_secrets_key_value
    if raw:
        try:
            decoded = base64.urlsafe_b64decode(raw)
        except Exception as exc:  # noqa: BLE001
            raise LLMSecretsKeyError(
                'LLM_SECRETS_KEY должен быть urlsafe base64 (44 символа)'
            ) from exc
        if len(decoded) != 32:
            raise LLMSecretsKeyError(
                'LLM_SECRETS_KEY должен декодироваться в 32 байта'
            )
        return raw.encode('ascii'), False

    bot_token = getattr(settings, 'bot_token', None) or ''
    if not bot_token:
        raise LLMSecretsKeyError(
            'LLM_SECRETS_KEY не задан и BOT_TOKEN недоступен — нечем '
            'derive Fernet-ключ'
        )
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_HKDF_SALT,
        info=_HKDF_INFO,
    ).derive(bot_token.encode('utf-8'))
    return base64.urlsafe_b64encode(derived), True


@lru_cache(maxsize=1)
def _get_fernet() -> Fernet:
    key_bytes, is_derived = _resolve_fernet_key_bytes()
    if is_derived:
        logger.warning(
            'LLM_SECRETS_KEY не задан — Fernet-ключ derive из BOT_TOKEN. '
            'В production задайте отдельный LLM_SECRETS_KEY (urlsafe base64, '
            '32 байта; `python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"`).'
        )
    return Fernet(key_bytes)


def encrypt_api_key(plain: str) -> str:
    """Зашифровать plain api_key в Fernet-токен (str)."""
    if not plain:
        raise ValueError('api_key не может быть пустым')
    token = _get_fernet().encrypt(plain.encode('utf-8'))
    return token.decode('ascii')


def decrypt_api_key(token: str) -> str:
    """Расшифровать Fernet-токен в plain api_key.

    Поднимает `LLMSecretsKeyError`, если ключ ротирован/токен повреждён.
    """
    if not token:
        raise LLMSecretsKeyError('Пустой шифротекст api_key_encrypted')
    try:
        plain = _get_fernet().decrypt(token.encode('ascii'))
    except InvalidToken as exc:
        raise LLMSecretsKeyError(
            'Не удалось расшифровать api_key (токен повреждён или '
            'LLM_SECRETS_KEY изменён). Перезалейте api_key через '
            '/admin/support-ai/.'
        ) from exc
    return plain.decode('utf-8')


def mask_api_key_preview(plain: str | None) -> str:
    """Безопасное превью api_key для UI: первые 3 + '…' + последние 3."""
    if not plain:
        return 'не задан'
    if len(plain) <= 8:
        return '••••' + plain[-2:]
    return f'{plain[:3]}…{plain[-3:]}'
