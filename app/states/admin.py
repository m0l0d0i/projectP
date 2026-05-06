from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class AdminState(StatesGroup):
    # Users
    waiting_user_query = State()
    waiting_balance_amount = State()

    # Promocodes
    waiting_promo_create = State()
    waiting_promo_edit = State()

    # Broadcasts
    waiting_broadcast_text = State()
    waiting_broadcast_custom_time = State()
    waiting_broadcast_edit_text = State()
    waiting_broadcast_edit_custom_time = State()

    # Pricing
    waiting_price_edit = State()