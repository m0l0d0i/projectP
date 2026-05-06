from __future__ import annotations

from aiogram.exceptions import TelegramBadRequest


def _is_message_not_modified(exc: TelegramBadRequest) -> bool:
    text = str(exc).lower()
    return 'message is not modified' in text


def _is_query_too_old(exc: TelegramBadRequest) -> bool:
    text = str(exc).lower()
    return 'query is too old' in text or 'query id is invalid' in text


def _is_message_cant_be_edited(exc: TelegramBadRequest) -> bool:
    text = str(exc).lower()
    return "message can't be edited" in text or 'message to edit not found' in text


async def safe_callback_answer(
    callback,
    text: str | None = None,
    *,
    show_alert: bool = False,
    cache_time: int | None = None,
) -> bool:
    try:
        kwargs: dict[str, object] = {'show_alert': show_alert}
        if text is not None:
            kwargs['text'] = text
        if cache_time is not None:
            kwargs['cache_time'] = cache_time
        await callback.answer(**kwargs)
        return True
    except TelegramBadRequest as exc:
        if _is_query_too_old(exc):
            return False
        raise


async def safe_edit_message_text(
    target,
    text: str,
    *,
    reply_markup=None,
    disable_web_page_preview: bool = False,
) -> bool:
    try:
        await target.edit_text(
            text,
            reply_markup=reply_markup,
            disable_web_page_preview=disable_web_page_preview,
        )
        return True
    except TelegramBadRequest as exc:
        if _is_message_not_modified(exc) or _is_message_cant_be_edited(exc):
            return False
        raise


async def safe_edit_reply_markup(target, *, reply_markup=None) -> bool:
    try:
        await target.edit_reply_markup(reply_markup=reply_markup)
        return True
    except TelegramBadRequest as exc:
        if _is_message_not_modified(exc) or _is_message_cant_be_edited(exc):
            return False
        raise