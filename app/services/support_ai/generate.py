"""Сборка prompt'а и вызов LLM для генерации черновика ответа саппорта (FEA-C32 #3).

Берёт активный `LLMConfig`, последние сообщения тикета (PII-замаскированы),
few-shot из `canned_responses` (по тегам тикета или top-N по usage_count),
вызывает провайдер и возвращает `SupportDraftResult`.

Не пишет в БД — увеличение usage и audit делает вызывающий handler в
своей транзакции (чтобы факт генерации был связан с реальным admin_id и
ticket_id, и чтобы при ошибке транзакция целиком откатилась).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

from app.db.models import (
    CannedResponse,
    LLMConfig,
    SupportMessage,
    SupportSenderType,
    SupportTicket,
)
from app.services.support_ai.base import LLMMessage, LLMResponse
from app.services.support_ai.factory import build_provider
from app.services.support_ai.pii import mask_pii

logger = logging.getLogger(__name__)

# Сколько последних сообщений тикета подкладываем как контекст. 10
# покрывает большинство тикетов (медиана < 5); больше — risk превышения
# token-window дешёвых LLM (DeepSeek context 32k достаточен, но
# OpenAI-compatible backend могут быть скромнее).
_MAX_TICKET_MESSAGES = 10
# Сколько few-shot примеров (canned-responses) кладём перед user-вопросом.
# Цель — direction prompt'а, не replication; 3 — sweet spot между
# латенцией и качеством.
_MAX_FEW_SHOT = 3
# Лимит на длину одного отдельного сообщения тикета в prompt'е, чтобы
# юзер не мог раздуть запрос вложением 50к символов.
_MAX_MESSAGE_CHARS = 1500


@dataclass(frozen=True, slots=True)
class SupportDraftResult:
    """Результат генерации черновика ответа.

    `draft` — готовый текст, который саппорт правит и отправляет через
    Telegram (или сохраняет как новый canned response).
    `used_canned_codes` — какие шаблоны вошли в few-shot (для UI:
    "сгенерировано на основе X, Y, Z" + инкремент usage_count в repo).
    """

    draft: str
    used_canned_codes: list[str]
    response: LLMResponse


def _select_few_shot(
    ticket_tags: Iterable[str],
    canned: list[CannedResponse],
    *,
    limit: int = _MAX_FEW_SHOT,
) -> list[CannedResponse]:
    """Выбор few-shot шаблонов по релевантности тегам тикета.

    Скоринг: сначала шаблоны с пересечением тегов (по числу совпадений);
    при равенстве — по usage_count desc, затем sort_order asc. Если
    тегов нет/совпадений нет — fallback: top-N по usage_count desc, затем
    sort_order asc. Возвращает не больше `limit` записей.
    """
    tag_set = {t.lower() for t in ticket_tags if t}
    enabled = [cr for cr in canned if cr.is_active]

    def score(cr: CannedResponse) -> tuple[int, int, int]:
        cr_tags = {t.lower() for t in (cr.tags or [])}
        overlap = len(tag_set & cr_tags) if tag_set else 0
        # Чем больше overlap → выше; usage_count desc; sort_order asc.
        return (overlap, int(cr.usage_count or 0), -int(cr.sort_order or 0))

    enabled.sort(key=score, reverse=True)
    return enabled[:limit]


def _truncate_message(text: str | None) -> str | None:
    if not text:
        return None
    cleaned = text.strip()
    if not cleaned:
        return None
    if len(cleaned) > _MAX_MESSAGE_CHARS:
        return cleaned[:_MAX_MESSAGE_CHARS] + ' …[обрезано]'
    return cleaned


def build_messages(
    config: LLMConfig,
    ticket: SupportTicket,
    messages: list[SupportMessage],
    canned_responses: list[CannedResponse],
) -> tuple[list[LLMMessage], list[str]]:
    """Собрать chat-completion messages для LLM.

    Структура:
    1. system: config.system_prompt (без подмены).
    2. system: «вот примеры хороших ответов» + few-shot canned-responses
       (если есть).
    3. system: краткое описание тикета (теги + статус).
    4. для каждого последнего сообщения тикета (≤ 10): role=user (если
       SupportSenderType.user) или role=assistant (если admin) — с
       PII-маскированием.
    5. user: «Сгенерируй черновик ответа на последнее сообщение
       пользователя.» (явная инструкция).

    Возвращает (messages, used_canned_codes).
    """
    out: list[LLMMessage] = [
        {'role': 'system', 'content': config.system_prompt},
    ]

    used_codes: list[str] = []
    few_shot = _select_few_shot(ticket.tags or [], canned_responses)
    if few_shot:
        few_shot_block = ['Примеры хороших ответов саппорта (используй стиль и тон):']
        for idx, cr in enumerate(few_shot, 1):
            few_shot_block.append(
                f'\nПример {idx} ({cr.code} — {cr.title}):\n{cr.content}'
            )
            used_codes.append(cr.code)
        out.append({'role': 'system', 'content': '\n'.join(few_shot_block)})

    ticket_meta = f'Тикет #{ticket.id}, статус: {ticket.status.value}'
    if ticket.tags:
        ticket_meta += f', теги: {", ".join(ticket.tags)}'
    out.append({'role': 'system', 'content': ticket_meta})

    recent_messages = messages[-_MAX_TICKET_MESSAGES:]
    for msg in recent_messages:
        text = _truncate_message(msg.text)
        if not text:
            # Сообщение без текста (только media) — короткая метка.
            text = f'[вложение: {msg.media_type or "файл"}]'
        masked = mask_pii(text)
        if msg.sender_type is SupportSenderType.user:
            out.append({'role': 'user', 'content': masked})
        else:
            out.append({'role': 'assistant', 'content': masked})

    out.append(
        {
            'role': 'user',
            'content': (
                'Сгенерируй черновик ответа саппорта на последнее сообщение '
                'пользователя выше. Без приветствия (саппорт уже в диалоге), '
                'кратко и по делу. Только текст ответа, без префиксов вроде '
                '«Ответ:» или «Черновик:».'
            ),
        }
    )
    return out, used_codes


async def generate_support_draft(
    config: LLMConfig,
    ticket: SupportTicket,
    messages: list[SupportMessage],
    canned_responses: list[CannedResponse],
    *,
    timeout_seconds: float = 30.0,
) -> SupportDraftResult:
    """Сгенерировать черновик ответа на тикет.

    Поднимает `LLMProviderError` (HTTP / network ошибки провайдера) и
    `LLMSecretsKeyError` (не удалось расшифровать api_key). Вызывающий
    код обрабатывает это и показывает понятное сообщение в UI + audit
    записывает status=error.
    """
    provider = build_provider(config)
    payload, used_codes = build_messages(config, ticket, messages, canned_responses)

    response = await provider.complete(
        messages=payload,
        temperature=float(config.temperature),
        max_tokens=int(config.max_tokens),
        timeout_seconds=timeout_seconds,
    )
    return SupportDraftResult(
        draft=response.text,
        used_canned_codes=used_codes,
        response=response,
    )
