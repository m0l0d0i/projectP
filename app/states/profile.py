from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class ProfileState(StatesGroup):
    waiting_promo_code = State()
    waiting_referral_code = State()