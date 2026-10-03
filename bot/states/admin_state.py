from aiogram.filters.state import State, StatesGroup


class BotAdminFSM(StatesGroup):
    """Short-lived state used while the owner enters the Telegram admin password."""

    waiting_password = State()
