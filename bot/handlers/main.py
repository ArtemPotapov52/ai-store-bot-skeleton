from aiogram import Dispatcher

from bot.handlers.admin import router as admin_router
from bot.handlers.other import router as other_router
from bot.handlers.user import router as user_router


def register_all_handlers(dp: Dispatcher) -> None:
    dp.include_router(admin_router)
    dp.include_router(other_router)
    dp.include_router(user_router)
