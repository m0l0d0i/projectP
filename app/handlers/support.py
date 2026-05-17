from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timezone

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.text_decorations import html_decoration as fmt
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import AuditAction, AuditActorType, SupportSenderType, SupportTicketStatus
from app.db.repositories import (
    AppSettingsRepository,
    AuditLogRepository,
    SupportMessageRepository,
    SupportTicketRepository,
    UserRepository,
)
from app.handlers.common import get_or_create_user
from app.i18n import all_translations
from app.keyboards.inline import (
    SupportTicketCallback,
    support_admin_history_keyboard,
    support_admin_ticket_keyboard,
    support_history_keyboard,
    support_list_keyboard,
    support_ticket_keyboard,
)
from app.observability.metrics import SUPPORT_TICKETS_CLOSED, SUPPORT_TICKETS_OPENED
from app.states.support import SupportState
from app.utils.runtime_settings import coerce_int_set, effective_list_from_row, effective_optional_int_from_row
from app.utils.telegram import safe_callback_answer, safe_edit_message_text, safe_edit_reply_markup

router = Router(name='support')
logger = logging.getLogger(__name__)

TICKET_RE = re.compile(r'#(\d+)')
HASHTAG_RE = re.compile(r'#ticket(\d+)', re.IGNORECASE)
USER_ID_TAG_RE = re.compile(r'#id(\d+)', re.IGNORECASE)
HISTORY_PAGE_SIZE = 6
SUPPORT_UNAVAILABLE_TEXT = '❌ Сообщение не отправлено, поддержка временно недоступна. Попробуйте позже.'
ACTIVE_TICKET_STATUSES = {
    SupportTicketStatus.waiting_operator,
    SupportTicketStatus.waiting_user,
}


@dataclass(slots=True)
class SupportRuntimeSettings:
    support_chat_id: int | None
    admin_ids: set[int]
    support_ids: set[int]


@dataclass(slots=True)
class ExtractedSupportPayload:
    text: str | None
    media_type: str | None
    media_file_id: str | None
    media_file_unique_id: str | None
    media_file_name: str | None
    media_mime_type: str | None
    media_size_bytes: int | None


async def _load_support_runtime_settings(session: AsyncSession, settings: Settings) -> SupportRuntimeSettings:
    try:
        repo = AppSettingsRepository(session)
        row = await repo.get()

        admin_ids = coerce_int_set(effective_list_from_row(row, 'admin_ids', settings.admin_ids))
        support_ids = coerce_int_set(effective_list_from_row(row, 'support_ids', settings.support_ids))
        support_chat_id = effective_optional_int_from_row(row, 'support_chat_id', settings.support_chat_id)

        return SupportRuntimeSettings(
            support_chat_id=support_chat_id,
            admin_ids=admin_ids,
            support_ids=support_ids,
        )
    except Exception:
        logger.exception('Failed to load support runtime settings from AppSettings, falling back to env settings')
        return SupportRuntimeSettings(
            support_chat_id=settings.support_chat_id,
            admin_ids=coerce_int_set(settings.admin_ids),
            support_ids=coerce_int_set(settings.support_ids),
        )


def _extract_media(message: Message) -> ExtractedSupportPayload:
    text = (message.caption or message.text or None)

    if message.photo:
        photo = message.photo[-1]
        return ExtractedSupportPayload(
            text=text,
            media_type='photo',
            media_file_id=photo.file_id,
            media_file_unique_id=getattr(photo, 'file_unique_id', None),
            media_file_name=None,
            media_mime_type=None,
            media_size_bytes=getattr(photo, 'file_size', None),
        )

    if message.video:
        video = message.video
        return ExtractedSupportPayload(
            text=text,
            media_type='video',
            media_file_id=video.file_id,
            media_file_unique_id=getattr(video, 'file_unique_id', None),
            media_file_name=getattr(video, 'file_name', None),
            media_mime_type=getattr(video, 'mime_type', None),
            media_size_bytes=getattr(video, 'file_size', None),
        )

    if message.document:
        document = message.document
        return ExtractedSupportPayload(
            text=text,
            media_type='document',
            media_file_id=document.file_id,
            media_file_unique_id=getattr(document, 'file_unique_id', None),
            media_file_name=getattr(document, 'file_name', None),
            media_mime_type=getattr(document, 'mime_type', None),
            media_size_bytes=getattr(document, 'file_size', None),
        )

    if message.audio:
        audio = message.audio
        return ExtractedSupportPayload(
            text=text,
            media_type='audio',
            media_file_id=audio.file_id,
            media_file_unique_id=getattr(audio, 'file_unique_id', None),
            media_file_name=getattr(audio, 'file_name', None),
            media_mime_type=getattr(audio, 'mime_type', None),
            media_size_bytes=getattr(audio, 'file_size', None),
        )

    if message.voice:
        voice = message.voice
        return ExtractedSupportPayload(
            text=text,
            media_type='voice',
            media_file_id=voice.file_id,
            media_file_unique_id=getattr(voice, 'file_unique_id', None),
            media_file_name=None,
            media_mime_type=getattr(voice, 'mime_type', None),
            media_size_bytes=getattr(voice, 'file_size', None),
        )

    if message.video_note:
        video_note = message.video_note
        return ExtractedSupportPayload(
            text=text,
            media_type='video_note',
            media_file_id=video_note.file_id,
            media_file_unique_id=getattr(video_note, 'file_unique_id', None),
            media_file_name=None,
            media_mime_type=None,
            media_size_bytes=getattr(video_note, 'file_size', None),
        )

    if message.animation:
        animation = message.animation
        return ExtractedSupportPayload(
            text=text,
            media_type='animation',
            media_file_id=animation.file_id,
            media_file_unique_id=getattr(animation, 'file_unique_id', None),
            media_file_name=getattr(animation, 'file_name', None),
            media_mime_type=getattr(animation, 'mime_type', None),
            media_size_bytes=getattr(animation, 'file_size', None),
        )

    if message.sticker:
        sticker = message.sticker
        return ExtractedSupportPayload(
            text=text,
            media_type='sticker',
            media_file_id=sticker.file_id,
            media_file_unique_id=getattr(sticker, 'file_unique_id', None),
            media_file_name=None,
            media_mime_type=None,
            media_size_bytes=getattr(sticker, 'file_size', None),
        )

    if message.text:
        return ExtractedSupportPayload(
            text=message.text,
            media_type=None,
            media_file_id=None,
            media_file_unique_id=None,
            media_file_name=None,
            media_mime_type=None,
            media_size_bytes=None,
        )

    raw_content_type = getattr(message, 'content_type', None)
    content_type = getattr(raw_content_type, 'value', raw_content_type)
    return ExtractedSupportPayload(
        text=None,
        media_type=str(content_type).lower() if content_type else 'unknown',
        media_file_id=None,
        media_file_unique_id=None,
        media_file_name=None,
        media_mime_type=None,
        media_size_bytes=None,
    )


def _is_ticket_active(ticket) -> bool:
    return ticket.status in ACTIVE_TICKET_STATUSES


def _ticket_status_label(status: SupportTicketStatus) -> str:
    labels = {
        SupportTicketStatus.waiting_operator: '🟡 Ожидает оператора',
        SupportTicketStatus.waiting_user: '🔵 Ожидает вашего ответа',
        SupportTicketStatus.closed: '🔴 Закрыта',
    }
    return labels.get(status, 'ℹ️ Неизвестный статус')


def _ticket_header(ticket_id: int, user_tg_id: int) -> str:
    return f'Заявка #{ticket_id} #ticket{ticket_id} | Пользователь: {user_tg_id} #id{user_tg_id}'


def _ticket_user_text(ticket) -> str:
    lines = [
        f'📬 <b>Заявка #{ticket.id}</b>',
        f'Статус: {_ticket_status_label(ticket.status)}',
    ]
    if ticket.status == SupportTicketStatus.waiting_operator:
        lines.append('Мы получили ваше сообщение и ждём ответ оператора.')
    elif ticket.status == SupportTicketStatus.waiting_user:
        lines.append('Оператор уже ответил. Вы можете продолжить диалог или закрыть заявку.')
    elif ticket.closed_at:
        closed_at = ticket.closed_at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        lines.append(f'Закрыта: {closed_at}')
    return '\n'.join(lines)


def _render_history(messages, *, page: int) -> tuple[str, bool, bool]:
    chunks: list[str] = []
    current_parts: list[str] = []
    current_len = 0

    for row in messages:
        msg_type = 'Сообщение пользователя' if row.sender_type == SupportSenderType.user else 'Ответ оператора'
        created = row.created_at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC') if row.created_at else '-'
        body_text = row.text or '(медиа без текста)'
        media_parts: list[str] = []
        if row.media_type:
            media_parts.append(f'Медиа: {row.media_type}')
        if getattr(row, 'media_file_name', None):
            media_parts.append(f'Файл: {fmt.quote(row.media_file_name)}')
        media = f"\n{' | '.join(media_parts)}" if media_parts else ''
        part = f'🕒 {created}\nТип: {msg_type}\nТекст: {fmt.quote(body_text)}{media}'
        added_len = len(part) + (2 if current_parts else 0)

        if current_parts and (current_len + added_len > 3500 or len(current_parts) >= HISTORY_PAGE_SIZE):
            chunks.append('\n\n'.join(current_parts))
            current_parts = [part]
            current_len = len(part)
        else:
            current_parts.append(part)
            current_len += added_len

    if current_parts:
        chunks.append('\n\n'.join(current_parts))
    if not chunks:
        chunks = ['Сообщений пока нет.']

    page = max(0, min(page, len(chunks) - 1))
    has_prev = page > 0
    has_next = page + 1 < len(chunks)
    return chunks[page], has_prev, has_next


def _log_support_forward_failure(
    *,
    ticket_id: int,
    user_tg_id: int,
    support_chat_id: int,
    exc: Exception,
) -> None:
    logger.exception(
        'Failed to forward message to support chat: '
        'ticket_id=%s user_tg_id=%s support_chat_id=%s error_type=%s error_message=%s',
        ticket_id,
        user_tg_id,
        support_chat_id,
        exc.__class__.__name__,
        str(exc),
    )


def _support_fallback_summary(*, ticket_id: int, user_tg_id: int, text: str | None) -> str:
    header = _ticket_header(ticket_id, user_tg_id)
    body = (text or '').strip()
    if body:
        return f'{header}\n\nСообщение пользователя:\n{fmt.quote(body[:3000])}'
    return (
        f'{header}\n\n'
        'Пользователь отправил вложение или сообщение, которое не удалось скопировать в чат поддержки. '
        'Проверьте диалог с пользователем вручную.'
    )


def _admin_reply_fallback_summary(*, ticket_id: int, text: str | None) -> str:
    body = (text or '').strip()
    if body:
        return f'💬 <b>Ответ по заявке #{ticket_id}</b>\n\n{fmt.quote(body[:3000])}'
    return (
        f'💬 <b>Ответ по заявке #{ticket_id}</b>\n\n'
        'Оператор отправил вложение или сообщение, которое не удалось переслать автоматически. '
        'При необходимости откройте заявку заново и запросите повторную отправку.'
    )


async def _send_to_support_chat(
    *,
    bot,
    chat_id: int,
    ticket_id: int,
    user_tg_id: int,
    from_chat_id: int,
    message_id: int,
    text: str | None,
) -> int | None:
    header = _ticket_header(ticket_id, user_tg_id)
    reply_markup = support_admin_ticket_keyboard(ticket_id, is_open=True)

    try:
        copied = await bot.copy_message(
            chat_id=chat_id,
            from_chat_id=from_chat_id,
            message_id=message_id,
        )
        summary = header if not text else f'{header}\n\nКомментарий: {fmt.quote(text)}'
        await bot.send_message(
            chat_id,
            summary,
            reply_to_message_id=copied.message_id,
            reply_markup=reply_markup,
        )
        return copied.message_id
    except (TelegramBadRequest, TelegramForbiddenError, TelegramAPIError) as exc:
        _log_support_forward_failure(
            ticket_id=ticket_id,
            user_tg_id=user_tg_id,
            support_chat_id=chat_id,
            exc=exc,
        )

    fallback_text = _support_fallback_summary(ticket_id=ticket_id, user_tg_id=user_tg_id, text=text)
    try:
        sent = await bot.send_message(
            chat_id,
            fallback_text,
            reply_markup=reply_markup,
        )
        logger.warning(
            'Support message delivered via fallback summary only: ticket_id=%s user_tg_id=%s support_chat_id=%s',
            ticket_id,
            user_tg_id,
            chat_id,
        )
        return sent.message_id
    except TelegramAPIError as fallback_exc:
        _log_support_forward_failure(
            ticket_id=ticket_id,
            user_tg_id=user_tg_id,
            support_chat_id=chat_id,
            exc=fallback_exc,
        )
        return None


async def _send_admin_reply_to_user(
    *,
    bot,
    user_tg_id: int,
    ticket_id: int,
    from_chat_id: int,
    message_id: int,
    text: str | None,
) -> int | None:
    try:
        copied = await bot.copy_message(
            chat_id=user_tg_id,
            from_chat_id=from_chat_id,
            message_id=message_id,
            reply_markup=support_ticket_keyboard(ticket_id, is_open=True),
        )
        return copied.message_id
    except TelegramForbiddenError:
        raise
    except TelegramAPIError:
        logger.exception(
            'Failed to copy support admin reply to user: ticket_id=%s user_tg_id=%s',
            ticket_id,
            user_tg_id,
        )

    fallback_text = _admin_reply_fallback_summary(ticket_id=ticket_id, text=text)
    try:
        sent = await bot.send_message(
            user_tg_id,
            fallback_text,
            reply_markup=support_ticket_keyboard(ticket_id, is_open=True),
        )
        logger.warning(
            'Support admin reply delivered via fallback summary only: ticket_id=%s user_tg_id=%s',
            ticket_id,
            user_tg_id,
        )
        return sent.message_id
    except TelegramForbiddenError:
        raise
    except TelegramAPIError:
        logger.exception(
            'Failed to deliver fallback support admin reply: ticket_id=%s user_tg_id=%s',
            ticket_id,
            user_tg_id,
        )
        return None


def _extract_ticket_id_from_admin_message(message: Message) -> int | None:
    if not message.reply_to_message:
        return None
    src = message.reply_to_message.text or message.reply_to_message.caption or ''
    for pattern in (HASHTAG_RE, TICKET_RE):
        match = pattern.search(src)
        if match:
            return int(match.group(1))
    return None


async def _resolve_ticket_id_from_admin_reply(message: Message, session: AsyncSession) -> int | None:
    if not message.reply_to_message:
        return None

    reply_message_id = getattr(message.reply_to_message, 'message_id', None)
    if reply_message_id is not None:
        support_message = await SupportMessageRepository(session).get_by_admin_chat_message_id(reply_message_id)
        if support_message:
            return support_message.ticket_id

    return _extract_ticket_id_from_admin_message(message)


def _extract_user_id_from_admin_message(message: Message) -> int | None:
    if not message.reply_to_message:
        return None
    src = message.reply_to_message.text or message.reply_to_message.caption or ''
    match = USER_ID_TAG_RE.search(src)
    if match:
        return int(match.group(1))
    return None


def _is_support_staff(tg_id: int | None, runtime_settings: SupportRuntimeSettings) -> bool:
    if not tg_id:
        return False
    return tg_id in runtime_settings.admin_ids or tg_id in runtime_settings.support_ids


def _support_staff_recipient_ids(runtime_settings: SupportRuntimeSettings, *, exclude_tg_id: int | None = None) -> list[int]:
    recipients = sorted({*runtime_settings.support_ids, *runtime_settings.admin_ids})
    if exclude_tg_id is None:
        return recipients
    return [tg_id for tg_id in recipients if tg_id != exclude_tg_id]


def _is_support_operator_chat(
    *,
    chat_id: int | None,
    chat_type: object | None,
    actor_tg_id: int | None,
    runtime_settings: SupportRuntimeSettings,
) -> bool:
    if not _is_support_staff(actor_tg_id, runtime_settings):
        return False

    if runtime_settings.support_chat_id and chat_id == runtime_settings.support_chat_id:
        return True

    normalized_chat_type = getattr(chat_type, 'value', chat_type)
    return normalized_chat_type == 'private' and actor_tg_id is not None and chat_id == actor_tg_id


async def _send_to_support_staff_fallback(
    *,
    bot,
    recipient_ids: list[int],
    ticket_id: int,
    user_tg_id: int,
    text: str | None,
) -> int | None:
    fallback_text = _support_fallback_summary(ticket_id=ticket_id, user_tg_id=user_tg_id, text=text)
    reply_markup = support_admin_ticket_keyboard(ticket_id, is_open=True)
    first_sent_message_id: int | None = None

    for recipient_id in recipient_ids:
        try:
            sent = await bot.send_message(recipient_id, fallback_text, reply_markup=reply_markup)
            if first_sent_message_id is None:
                first_sent_message_id = sent.message_id
        except TelegramAPIError:
            logger.exception(
                'Failed to deliver fallback support summary to operator: ticket_id=%s user_tg_id=%s recipient_tg_id=%s',
                ticket_id,
                user_tg_id,
                recipient_id,
            )

    if first_sent_message_id is not None:
        logger.warning(
            'Support message delivered to operators via fallback direct messages only: ticket_id=%s user_tg_id=%s recipient_count=%s',
            ticket_id,
            user_tg_id,
            len(recipient_ids),
        )
    return first_sent_message_id


async def _deliver_to_support_destinations(
    *,
    bot,
    runtime_settings: SupportRuntimeSettings,
    ticket_id: int,
    user_tg_id: int,
    from_chat_id: int,
    message_id: int,
    text: str | None,
) -> int | None:
    if runtime_settings.support_chat_id:
        delivered = await _send_to_support_chat(
            bot=bot,
            chat_id=runtime_settings.support_chat_id,
            ticket_id=ticket_id,
            user_tg_id=user_tg_id,
            from_chat_id=from_chat_id,
            message_id=message_id,
            text=text,
        )
        if delivered is not None:
            return delivered

    staff_recipient_ids = _support_staff_recipient_ids(runtime_settings, exclude_tg_id=user_tg_id)
    if not staff_recipient_ids:
        return None

    return await _send_to_support_staff_fallback(
        bot=bot,
        recipient_ids=staff_recipient_ids,
        ticket_id=ticket_id,
        user_tg_id=user_tg_id,
        text=text,
    )


def _media_allowed(settings: Settings, media_type: str | None, size: int | None) -> tuple[bool, str | None]:
    if media_type is None:
        return True, None
    if media_type not in settings.support_allowed_media_types:
        return False, '❌ Допустимы текст, фото, видео, документы, голосовые и другие разрешённые вложения.'
    if size and size > settings.support_max_media_bytes:
        return False, '❌ Файл слишком большой. Пожалуйста, отправьте файл меньшего размера.'
    return True, None


async def _mark_user_bot_blocked(session: AsyncSession, user_id: int, reason: str) -> None:
    user_repo = UserRepository(session)
    user = await user_repo.get_by_id_for_update(user_id)
    if user is not None:
        await user_repo.set_bot_blocked(user, True, reason)


async def _notify_ticket_closed(
    *,
    bot,
    support_chat_id: int | None,
    support_recipient_ids: list[int] | None,
    ticket_id: int,
    user_tg_id: int | None,
    reason: str,
    notify_user: bool = True,
) -> None:
    user_text = '🔒 Ваша заявка закрыта.\n\nЕсли потребуется помощь, создайте новое обращение через раздел поддержки.'
    support_actor = {
        'user_closed': 'пользователем',
        'admin_closed': 'оператором',
        'web_admin_closed': 'администратором',
    }.get(reason, 'инициатором')
    support_text = f'🔒 Заявка #{ticket_id} #ticket{ticket_id} закрыта ({support_actor}).'

    if notify_user and user_tg_id:
        try:
            await bot.send_message(user_tg_id, user_text)
        except TelegramAPIError:
            logger.exception(
                'Failed to notify user about ticket close: ticket_id=%s user_tg_id=%s',
                ticket_id,
                user_tg_id,
            )

    delivered_to_support = False
    if support_chat_id:
        try:
            await bot.send_message(support_chat_id, support_text)
            delivered_to_support = True
        except TelegramAPIError:
            logger.exception('Failed to notify support chat about ticket close: ticket_id=%s', ticket_id)

    if not delivered_to_support:
        for recipient_id in support_recipient_ids or []:
            try:
                await bot.send_message(recipient_id, support_text)
                delivered_to_support = True
            except TelegramAPIError:
                logger.exception(
                    'Failed to notify operator about ticket close: ticket_id=%s recipient_tg_id=%s',
                    ticket_id,
                    recipient_id,
                )


@router.callback_query(SupportTicketCallback.filter(F.action == 'noop'))
async def support_noop(callback: CallbackQuery) -> None:
    await safe_callback_answer(callback)


@router.message(F.text.in_(all_translations('📞 Поддержка')))
async def support_home(
    message: Message,
    session: AsyncSession,
    _: Callable[[str], str] = lambda s: s,
) -> None:
    user = await get_or_create_user(message, session)
    tickets = await SupportTicketRepository(session).list_by_user(user.id)
    ids = [t.id for t in tickets]
    await message.answer(_('📬 <b>Ваши обращения в поддержку:</b>'), reply_markup=support_list_keyboard(ids, page=0))


@router.callback_query(SupportTicketCallback.filter(F.action == 'page'))
async def support_page(callback: CallbackQuery, callback_data: SupportTicketCallback, session: AsyncSession) -> None:
    user = await get_or_create_user(callback, session)
    tickets = await SupportTicketRepository(session).list_by_user(user.id)
    ids = [t.id for t in tickets]
    await safe_edit_reply_markup(callback.message, reply_markup=support_list_keyboard(ids, page=max(0, callback_data.page)))
    await safe_callback_answer(callback)


@router.callback_query(SupportTicketCallback.filter(F.action == 'new'))
async def support_new(
    callback: CallbackQuery,
    state: FSMContext,
    _: Callable[[str], str],
) -> None:
    await state.set_state(SupportState.waiting_new_message)
    await state.update_data(ticket_id=0)
    await callback.message.answer(_('📩 Введите ваше обращение. Можно отправить текст, фото, видео, документ или голосовое сообщение.'))
    await safe_callback_answer(callback)


@router.callback_query(SupportTicketCallback.filter(F.action == 'open'))
async def support_open(callback: CallbackQuery, callback_data: SupportTicketCallback, session: AsyncSession) -> None:
    user = await get_or_create_user(callback, session)
    ticket_repo = SupportTicketRepository(session)
    ticket = await ticket_repo.get_by_id(callback_data.ticket_id)
    if not ticket or ticket.user_id != user.id:
        await safe_callback_answer(callback, 'Заявка не найдена', show_alert=True)
        return

    await safe_edit_message_text(
        callback.message,
        _ticket_user_text(ticket),
        reply_markup=support_ticket_keyboard(ticket.id, is_open=_is_ticket_active(ticket)),
    )
    await safe_callback_answer(callback)


@router.callback_query(SupportTicketCallback.filter(F.action == 'history'))
async def support_history(callback: CallbackQuery, callback_data: SupportTicketCallback, session: AsyncSession) -> None:
    user = await get_or_create_user(callback, session)
    ticket = await SupportTicketRepository(session).get_by_id(callback_data.ticket_id)
    if not ticket or ticket.user_id != user.id:
        await safe_callback_answer(callback, 'Заявка не найдена', show_alert=True)
        return

    messages = await SupportMessageRepository(session).list_by_ticket(ticket.id)
    text, has_prev, has_next = _render_history(messages, page=callback_data.msg_page)
    await safe_edit_message_text(
        callback.message,
        f'📜 <b>История заявки #{ticket.id}</b>\n\n{text}',
        reply_markup=support_history_keyboard(
            ticket.id,
            page=callback_data.msg_page,
            has_prev=has_prev,
            has_next=has_next,
            is_open=_is_ticket_active(ticket),
        ),
    )
    await safe_callback_answer(callback)


@router.callback_query(SupportTicketCallback.filter(F.action == 'history_page'))
async def support_history_page(callback: CallbackQuery, callback_data: SupportTicketCallback, session: AsyncSession) -> None:
    await support_history(callback, callback_data, session)


@router.callback_query(SupportTicketCallback.filter(F.action == 'back'))
async def support_back_to_list(
    callback: CallbackQuery,
    session: AsyncSession,
    _: Callable[[str], str],
) -> None:
    user = await get_or_create_user(callback, session)
    tickets = await SupportTicketRepository(session).list_by_user(user.id)
    ids = [t.id for t in tickets]
    await safe_edit_message_text(
        callback.message,
        _('📬 <b>Ваши обращения в поддержку:</b>'),
        reply_markup=support_list_keyboard(ids, page=0),
    )
    await safe_callback_answer(callback)


@router.callback_query(SupportTicketCallback.filter(F.action == 'close'))
async def support_close(
    callback: CallbackQuery,
    callback_data: SupportTicketCallback,
    session: AsyncSession,
    state: FSMContext,
    settings: Settings,
) -> None:
    user = await get_or_create_user(callback, session)
    repo = SupportTicketRepository(session)
    ticket = await repo.get_by_id_for_update(callback_data.ticket_id)

    if not ticket or ticket.user_id != user.id:
        await safe_callback_answer(callback, 'Заявка не найдена', show_alert=True)
        return

    closed_now = await repo.close(
        ticket,
        None,
        actor_tg_id=user.tg_id,
        actor_type=SupportSenderType.user,
    )
    if not closed_now:
        await safe_edit_reply_markup(callback.message, reply_markup=support_ticket_keyboard(ticket.id, is_open=False))
        await safe_callback_answer(callback, 'Заявка уже закрыта')
        return

    await AuditLogRepository(session).create(
        action=AuditAction.ticket_closed,
        actor_type=AuditActorType.user,
        actor_tg_id=user.tg_id,
        entity_type='support_ticket',
        entity_id=str(ticket.id),
        details={'reason': 'user_closed'},
    )
    await session.commit()
    await state.clear()

    runtime_settings = await _load_support_runtime_settings(session, settings)
    await _notify_ticket_closed(
        bot=callback.bot,
        support_chat_id=runtime_settings.support_chat_id,
        support_recipient_ids=_support_staff_recipient_ids(runtime_settings, exclude_tg_id=user.tg_id),
        ticket_id=ticket.id,
        user_tg_id=user.tg_id,
        reason='user_closed',
        notify_user=False,
    )
    SUPPORT_TICKETS_CLOSED.labels(reason='user_closed').inc()

    await safe_edit_reply_markup(callback.message, reply_markup=support_ticket_keyboard(ticket.id, is_open=False))
    await callback.message.answer('Спасибо за обращение! Ваша заявка закрыта.')
    await safe_callback_answer(callback, 'Закрыто')


@router.callback_query(SupportTicketCallback.filter(F.action == 'reply'))
async def support_reply(
    callback: CallbackQuery,
    callback_data: SupportTicketCallback,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    user = await get_or_create_user(callback, session)
    ticket = await SupportTicketRepository(session).get_by_id(callback_data.ticket_id)
    if not ticket or ticket.user_id != user.id:
        await safe_callback_answer(callback, 'Заявка не найдена', show_alert=True)
        return
    if not _is_ticket_active(ticket):
        await safe_callback_answer(callback, 'Заявка уже закрыта', show_alert=True)
        return

    await state.set_state(SupportState.waiting_reply_message)
    await state.update_data(ticket_id=callback_data.ticket_id)
    await callback.message.answer('✍️ Введите ваше сообщение по заявке.')
    await safe_callback_answer(callback)


@router.message(SupportState.waiting_new_message, ~F.text.startswith('/'))
async def support_send_new(message: Message, session: AsyncSession, settings: Settings, state: FSMContext) -> None:
    user = await get_or_create_user(message, session)
    runtime_settings = await _load_support_runtime_settings(session, settings)

    payload = _extract_media(message)
    ok_media, media_error = _media_allowed(settings, payload.media_type, payload.media_size_bytes)
    if not ok_media:
        await message.answer(media_error or '❌ Неверный формат вложения.')
        return

    ticket_repo = SupportTicketRepository(session)
    ticket = await ticket_repo.get_open_by_user(user.id)
    created_new = False

    if not ticket:
        try:
            ticket = await ticket_repo.create(user.id)
            created_new = True
        except IntegrityError:
            await session.rollback()
            ticket = await ticket_repo.get_open_by_user(user.id)
            if not ticket:
                await message.answer('❌ Не удалось создать обращение. Попробуйте ещё раз.')
                await state.clear()
                return

    admin_msg_id = await _deliver_to_support_destinations(
        bot=message.bot,
        runtime_settings=runtime_settings,
        ticket_id=ticket.id,
        user_tg_id=user.tg_id,
        from_chat_id=message.chat.id,
        message_id=message.message_id,
        text=payload.text,
    )
    if not admin_msg_id:
        if created_new:
            await session.delete(ticket)
            await session.flush()
        await message.answer(SUPPORT_UNAVAILABLE_TEXT)
        await state.clear()
        return

    await SupportMessageRepository(session).create(
        ticket_id=ticket.id,
        sender_type=SupportSenderType.user,
        sender_tg_id=user.tg_id,
        text=payload.text,
        media_type=payload.media_type,
        media_file_id=payload.media_file_id,
        media_file_unique_id=payload.media_file_unique_id,
        media_file_name=payload.media_file_name,
        media_mime_type=payload.media_mime_type,
        media_size_bytes=payload.media_size_bytes,
        admin_chat_message_id=admin_msg_id,
    )
    await ticket_repo.touch_user_reply(ticket, sender_tg_id=user.tg_id)
    await session.commit()

    if created_new:
        SUPPORT_TICKETS_OPENED.inc()

    await message.answer('📤 Сообщение успешно отправлено.')
    await state.clear()


@router.message(SupportState.waiting_reply_message, ~F.text.startswith('/'))
async def support_send_reply(message: Message, session: AsyncSession, settings: Settings, state: FSMContext) -> None:
    data = await state.get_data()
    ticket_id = int(data.get('ticket_id') or 0)
    user = await get_or_create_user(message, session)
    runtime_settings = await _load_support_runtime_settings(session, settings)

    payload = _extract_media(message)
    ok_media, media_error = _media_allowed(settings, payload.media_type, payload.media_size_bytes)
    if not ok_media:
        await message.answer(media_error or '❌ Неверный формат вложения.')
        return

    ticket_repo = SupportTicketRepository(session)
    ticket = await ticket_repo.get_by_id_for_update(ticket_id)
    if not ticket or ticket.user_id != user.id:
        await message.answer('❌ Заявка не найдена.')
        await state.clear()
        return
    if not _is_ticket_active(ticket):
        await message.answer('❌ Заявка уже закрыта. Ответить нельзя.')
        await state.clear()
        return

    admin_msg_id = await _deliver_to_support_destinations(
        bot=message.bot,
        runtime_settings=runtime_settings,
        ticket_id=ticket.id,
        user_tg_id=user.tg_id,
        from_chat_id=message.chat.id,
        message_id=message.message_id,
        text=payload.text,
    )
    if not admin_msg_id:
        await message.answer(SUPPORT_UNAVAILABLE_TEXT)
        await state.clear()
        return

    await SupportMessageRepository(session).create(
        ticket_id=ticket.id,
        sender_type=SupportSenderType.user,
        sender_tg_id=user.tg_id,
        text=payload.text,
        media_type=payload.media_type,
        media_file_id=payload.media_file_id,
        media_file_unique_id=payload.media_file_unique_id,
        media_file_name=payload.media_file_name,
        media_mime_type=payload.media_mime_type,
        media_size_bytes=payload.media_size_bytes,
        admin_chat_message_id=admin_msg_id,
    )
    await ticket_repo.touch_user_reply(ticket, sender_tg_id=user.tg_id)
    await session.commit()

    await message.answer('📤 Ответ успешно отправлен.')
    await state.clear()


@router.callback_query(SupportTicketCallback.filter(F.action == 'admin_reply_help'))
async def support_admin_reply_help(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    runtime_settings = await _load_support_runtime_settings(session, settings)
    if not _is_support_operator_chat(
        chat_id=getattr(callback.message.chat, 'id', None) if callback.message else None,
        chat_type=getattr(callback.message.chat, 'type', None) if callback.message else None,
        actor_tg_id=callback.from_user.id if callback.from_user else None,
        runtime_settings=runtime_settings,
    ):
        await safe_callback_answer(callback, 'Недостаточно прав', show_alert=True)
        return

    await safe_callback_answer(
        callback,
        'Ответьте реплаем на копию сообщения заявки или на служебное сообщение заявки — ответ уйдёт пользователю.',
        show_alert=True,
    )


@router.callback_query(SupportTicketCallback.filter(F.action == 'admin_close'))
async def support_admin_close(
    callback: CallbackQuery,
    callback_data: SupportTicketCallback,
    session: AsyncSession,
    settings: Settings,
) -> None:
    runtime_settings = await _load_support_runtime_settings(session, settings)
    if not _is_support_operator_chat(
        chat_id=getattr(callback.message.chat, 'id', None) if callback.message else None,
        chat_type=getattr(callback.message.chat, 'type', None) if callback.message else None,
        actor_tg_id=callback.from_user.id if callback.from_user else None,
        runtime_settings=runtime_settings,
    ):
        await safe_callback_answer(callback, 'Недостаточно прав', show_alert=True)
        return

    ticket_repo = SupportTicketRepository(session)
    ticket = await ticket_repo.get_by_id_for_update(callback_data.ticket_id)
    if not ticket:
        await safe_callback_answer(callback, 'Заявка не найдена', show_alert=True)
        return

    if ticket.status == SupportTicketStatus.closed:
        await safe_edit_reply_markup(callback.message, reply_markup=support_admin_ticket_keyboard(ticket.id, is_open=False))
        await safe_callback_answer(callback, 'Заявка уже закрыта')
        return

    closed_now = await ticket_repo.close(
        ticket,
        None,
        closed_by_admin_tg_id=callback.from_user.id if callback.from_user else None,
        actor_tg_id=callback.from_user.id if callback.from_user else None,
        actor_type=SupportSenderType.admin,
    )
    if not closed_now:
        await safe_edit_reply_markup(callback.message, reply_markup=support_admin_ticket_keyboard(ticket.id, is_open=False))
        await safe_callback_answer(callback, 'Заявка уже закрыта')
        return

    await AuditLogRepository(session).create(
        action=AuditAction.ticket_closed,
        actor_type=AuditActorType.admin,
        actor_tg_id=callback.from_user.id,
        entity_type='support_ticket',
        entity_id=str(ticket.id),
        details={'reason': 'admin_closed'},
    )

    user = await UserRepository(session).get_by_id(ticket.user_id)
    await session.commit()

    if user:
        try:
            await _notify_ticket_closed(
                bot=callback.bot,
                support_chat_id=runtime_settings.support_chat_id,
                support_recipient_ids=_support_staff_recipient_ids(runtime_settings, exclude_tg_id=user.tg_id),
                ticket_id=ticket.id,
                user_tg_id=user.tg_id,
                reason='admin_closed',
            )
        except TelegramForbiddenError as exc:
            await _mark_user_bot_blocked(session, user.id, str(exc))
            await session.commit()
        except TelegramAPIError:
            logger.exception(
                'Failed to deliver admin close notifications: ticket_id=%s user_id=%s',
                ticket.id,
                user.id,
            )

    SUPPORT_TICKETS_CLOSED.labels(reason='admin_closed').inc()

    await safe_edit_reply_markup(callback.message, reply_markup=support_admin_ticket_keyboard(ticket.id, is_open=False))
    await safe_callback_answer(callback, 'Заявка закрыта')


@router.callback_query(SupportTicketCallback.filter(F.action == 'admin_history'))
async def support_admin_history(
    callback: CallbackQuery,
    callback_data: SupportTicketCallback,
    session: AsyncSession,
    settings: Settings,
) -> None:
    runtime_settings = await _load_support_runtime_settings(session, settings)
    if not _is_support_operator_chat(
        chat_id=getattr(callback.message.chat, 'id', None) if callback.message else None,
        chat_type=getattr(callback.message.chat, 'type', None) if callback.message else None,
        actor_tg_id=callback.from_user.id if callback.from_user else None,
        runtime_settings=runtime_settings,
    ):
        await safe_callback_answer(callback, 'Недостаточно прав', show_alert=True)
        return

    ticket = await SupportTicketRepository(session).get_by_id(callback_data.ticket_id)
    if not ticket:
        await safe_callback_answer(callback, 'Заявка не найдена', show_alert=True)
        return

    messages = await SupportMessageRepository(session).list_by_ticket(ticket.id)
    text, has_prev, has_next = _render_history(messages, page=callback_data.msg_page)
    await callback.message.answer(
        f'📜 История заявки #{ticket.id} #ticket{ticket.id}\n\n{text}',
        reply_markup=support_admin_history_keyboard(
            ticket.id,
            page=callback_data.msg_page,
            has_prev=has_prev,
            has_next=has_next,
            is_open=_is_ticket_active(ticket),
        ),
    )
    await safe_callback_answer(callback)


@router.message(F.reply_to_message)
async def support_admin_reply_router(message: Message, session: AsyncSession, settings: Settings) -> None:
    runtime_settings = await _load_support_runtime_settings(session, settings)
    if not _is_support_operator_chat(
        chat_id=getattr(message.chat, 'id', None),
        chat_type=getattr(message.chat, 'type', None),
        actor_tg_id=message.from_user.id if message.from_user else None,
        runtime_settings=runtime_settings,
    ):
        return

    ticket_id = await _resolve_ticket_id_from_admin_reply(message, session)
    if not ticket_id:
        return

    if not _is_support_staff(message.from_user.id if message.from_user else None, runtime_settings):
        await message.reply('⛔ У вас недостаточно прав.')
        return

    ticket_repo = SupportTicketRepository(session)
    ticket = await ticket_repo.get_by_id_for_update(ticket_id)
    if not ticket:
        return

    if not _is_ticket_active(ticket):
        await message.answer(f'❌ Заявка #{ticket.id} уже закрыта. Ответ пользователю не отправлен.')
        return

    user_repo = UserRepository(session)
    user = await user_repo.get_by_id(ticket.user_id)
    if not user:
        fallback_user_id = _extract_user_id_from_admin_message(message)
        if fallback_user_id:
            user = await user_repo.get_by_tg_id(fallback_user_id)
    if not user:
        await message.answer('❌ Не удалось определить пользователя для ответа.')
        return

    payload = _extract_media(message)

    try:
        delivered_message_id = await _send_admin_reply_to_user(
            bot=message.bot,
            user_tg_id=user.tg_id,
            ticket_id=ticket.id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
            text=payload.text,
        )
    except TelegramForbiddenError as exc:
        await _mark_user_bot_blocked(session, user.id, str(exc))
        await session.commit()
        await message.answer('❌ Пользователь заблокировал бота. Сообщение не доставлено.')
        return

    if delivered_message_id is None:
        await message.answer('❌ Ошибка отправки пользователю.')
        return

    await SupportMessageRepository(session).create(
        ticket_id=ticket.id,
        sender_type=SupportSenderType.admin,
        sender_tg_id=message.from_user.id,
        text=payload.text,
        media_type=payload.media_type,
        media_file_id=payload.media_file_id,
        media_file_unique_id=payload.media_file_unique_id,
        media_file_name=payload.media_file_name,
        media_mime_type=payload.media_mime_type,
        media_size_bytes=payload.media_size_bytes,
        admin_chat_message_id=message.message_id,
    )
    await ticket_repo.touch_admin_reply(ticket, sender_tg_id=message.from_user.id if message.from_user else None)
    await session.commit()

    await message.answer('✅ Ответ отправлен пользователю.')
