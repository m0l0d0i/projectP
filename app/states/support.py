from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class SupportState(StatesGroup):
    waiting_new_message = State()
    waiting_reply_message = State()