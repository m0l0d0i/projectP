"""Smart push-уведомления (FEA-NOTIF) — рендер шаблона + cooldown + outbox.

Каждый сценарий идентифицируется `code`. При вызове `dispatch()`:
  1. Резолвим `NotificationRule` по `code` из БД.
     - Если правило отключено (`is_enabled=False`) → пропускаем доставку.
     - Если правила нет в БД → используем вшитый fallback (`default_text`).
  2. Рендерим текст и кнопки через Jinja2 SandboxedEnvironment.
  3. Cooldown через Redis-key `notif_cooldown:{user_id}:{code}` (если задан).
  4. Постановка в outbox с переданным `correlation_key`.

Cooldown == 0 означает, что отсечка идёт по `correlation_key` outbox'а
(per-record dedup, как в текущей логике с флагами `notified_3d` и т.д.).
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from jinja2.exceptions import TemplateError
from jinja2.sandbox import SandboxedEnvironment
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import NotificationRule
from app.db.repositories import NotificationRuleRepository, OutboxRepository

logger = logging.getLogger(__name__)


_jinja_env = SandboxedEnvironment(
    autoescape=False,
    keep_trailing_newline=True,
)


class NotificationDispatcher:
    """Единая точка отправки push-уведомлений (FEA-NOTIF).

    Один экземпляр на процесс. Принимает Redis-клиент для cooldown'ов и
    использует переданную сессию SQLAlchemy для чтения правила и постановки
    задачи в outbox в рамках одной транзакции с доменной мутацией.
    """

    COOLDOWN_KEY_PREFIX = 'notif_cooldown'

    def __init__(
        self,
        *,
        redis_client: Any | None = None,
        redis_prefix: str = 'vpn_bot',
    ) -> None:
        self._redis = redis_client
        self._redis_prefix = redis_prefix

    async def dispatch(
        self,
        *,
        session: AsyncSession,
        code: str,
        chat_id: int,
        user_id: int,
        default_text: str,
        default_reply_markup: InlineKeyboardMarkup | None = None,
        default_parse_mode: str | None = None,
        context: dict[str, Any] | None = None,
        correlation_key: str | None = None,
    ) -> bool:
        """Отправить уведомление через outbox с учётом правила и cooldown'а.

        Возвращает True, если задача поставлена в outbox; False, если правило
        отключено, cooldown активен или произошла ошибка рендера.
        """
        rule = await NotificationRuleRepository(session).get_by_code(code)
        if rule is not None and not rule.is_enabled:
            logger.debug('Notification rule %s is disabled, skipping', code)
            return False

        ctx: dict[str, Any] = dict(context or {})

        text = default_text
        reply_markup = default_reply_markup
        parse_mode = default_parse_mode

        if rule is not None:
            try:
                text = self._render_text(rule, ctx)
            except TemplateError:
                logger.exception(
                    'Failed to render notification template for code=%s; using fallback', code,
                )
                text = default_text

            if rule.template_keyboard_json is not None:
                rendered_kb = self._render_keyboard(rule.template_keyboard_json, ctx)
                if rendered_kb is not None:
                    reply_markup = rendered_kb

            if rule.cooldown_seconds > 0 and self._redis is not None:
                acquired = await self._acquire_cooldown(
                    user_id=user_id, code=code, ttl_seconds=rule.cooldown_seconds,
                )
                if not acquired:
                    logger.debug(
                        'Notification cooldown active for user_id=%s code=%s; skipping',
                        user_id, code,
                    )
                    return False

        await OutboxRepository(session).enqueue_tg_message(
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            user_id=user_id,
            correlation_key=correlation_key,
        )
        return True

    @staticmethod
    def _render_text(rule: NotificationRule, ctx: dict[str, Any]) -> str:
        template = _jinja_env.from_string(rule.template_text)
        return template.render(**ctx)

    @staticmethod
    def _render_keyboard(
        rows_json: list[Any], ctx: dict[str, Any]
    ) -> InlineKeyboardMarkup | None:
        """JSON формат: [[{text, url|callback_data}, ...], ...]; значения
        пропускаются через Jinja с переданным контекстом. Возвращает None,
        если структура некорректна — вызывающий тогда оставит default."""
        try:
            rendered_rows: list[list[InlineKeyboardButton]] = []
            for row in rows_json:
                if not isinstance(row, list):
                    return None
                rendered_row: list[InlineKeyboardButton] = []
                for btn in row:
                    if not isinstance(btn, dict):
                        return None
                    text_raw = btn.get('text')
                    if not isinstance(text_raw, str) or not text_raw.strip():
                        return None
                    text = _jinja_env.from_string(text_raw).render(**ctx)
                    url_raw = btn.get('url')
                    cb_raw = btn.get('callback_data')
                    if isinstance(url_raw, str) and url_raw.strip():
                        rendered_row.append(
                            InlineKeyboardButton(
                                text=text,
                                url=_jinja_env.from_string(url_raw).render(**ctx),
                            )
                        )
                    elif isinstance(cb_raw, str) and cb_raw.strip():
                        rendered_row.append(
                            InlineKeyboardButton(
                                text=text,
                                callback_data=_jinja_env.from_string(cb_raw).render(**ctx),
                            )
                        )
                    else:
                        return None
                if rendered_row:
                    rendered_rows.append(rendered_row)
            if not rendered_rows:
                return None
            return InlineKeyboardMarkup(inline_keyboard=rendered_rows)
        except (TemplateError, ValueError, TypeError):
            logger.exception('Failed to render notification keyboard template')
            return None

    async def _acquire_cooldown(
        self, *, user_id: int, code: str, ttl_seconds: int
    ) -> bool:
        """SET NX EX: True — захватили (отправляем), False — cooldown активен."""
        key = f'{self._redis_prefix}:{self.COOLDOWN_KEY_PREFIX}:{user_id}:{code}'
        try:
            result = await self._redis.set(key, '1', nx=True, ex=ttl_seconds)
            return bool(result)
        except Exception:
            # При ошибке Redis не блокируем отправку — это лучше чем терять
            # уведомления; повторные дубли отсекаются correlation_key outbox'а.
            logger.exception('Failed to acquire notification cooldown for code=%s', code)
            return True
