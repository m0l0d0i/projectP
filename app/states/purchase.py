from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class PurchaseState(StatesGroup):
    # Tariff flow
    choosing_tariff = State()
    choosing_months = State()
    waiting_payment = State()

    # Topup flow
    choosing_topup = State()
    waiting_balance_topup_amount = State()