from __future__ import annotations

import logging

from aiogram import Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ErrorEvent

from app.observability.metrics import HANDLER_ERRORS

logger = logging.getLogger(__name__)

router = Router(name='errors')


@router.errors()
async def on_error(event: ErrorEvent) -> bool:
    HANDLER_ERRORS.inc()
    logger.exception('Unhandled bot error', exc_info=event.exception)
    update = event.update
    try:
        if update.message:
            await update.message.answer('⚠️ Произошла техническая ошибка. Попробуйте еще раз чуть позже.')
            return True
        if update.callback_query:
            await update.callback_query.answer('⚠️ Произошла техническая ошибка. Попробуйте еще раз.', show_alert=True)
            return True
    except TelegramAPIError:
        logger.warning('Failed to deliver error message to user')
    return True
