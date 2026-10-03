"""Owner-only password gate for the Telegram admin panel.

The panel itself is intentionally not duplicated here.  After authentication
we render the existing admin console, so catalog, stock upload, users, sales,
statistics, roles, promos, broadcasts and settings keep their established
permission checks and confirmation flows.
"""

import hmac
import time
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.database.methods import check_role_cached
from bot.database.methods.audit import log_audit
from bot.database.models import Permission
from bot.handlers.admin.main import _render_admin_menu
from bot.i18n import localize
from bot.misc import EnvKeys
from bot.states import BotAdminFSM

router = Router()

_MAX_PASSWORD_ATTEMPTS = 5
_ATTEMPT_WINDOW_SECONDS = 60.0
_LOCKOUT_SECONDS = 300.0
_failed_attempts: dict[int, list[float]] = {}
_locked_until: dict[int, float] = {}


def _configured_password() -> str:
    """Return the Telegram-panel password without exposing it to logs/UI."""
    separate = str(getattr(EnvKeys, "BOT_ADMIN_PASSWORD", "") or "").strip()
    return separate or str(getattr(EnvKeys, "ADMIN_PASSWORD", "") or "").strip()


def _lockout_remaining(user_id: int, now: float | None = None) -> int:
    now = time.monotonic() if now is None else now
    until = _locked_until.get(user_id, 0.0)
    if until <= now:
        _locked_until.pop(user_id, None)
        return 0
    return max(1, int(until - now))


def _record_failed_attempt(user_id: int, now: float | None = None) -> int:
    now = time.monotonic() if now is None else now
    attempts = [
        stamp for stamp in _failed_attempts.get(user_id, [])
        if now - stamp <= _ATTEMPT_WINDOW_SECONDS
    ]
    attempts.append(now)
    _failed_attempts[user_id] = attempts
    if len(attempts) >= _MAX_PASSWORD_ATTEMPTS:
        _locked_until[user_id] = now + _LOCKOUT_SECONDS
    return _lockout_remaining(user_id, now)


async def _delete_secret_message(message: Message) -> None:
    """Best-effort deletion so a password is not left in the chat history."""
    try:
        await message.delete()
    except Exception:
        # Telegram may reject deletion when the bot lacks the relevant chat
        # rights.  Authentication still works; the secret is never logged.
        return


async def _open_admin_panel(message: Message, state: FSMContext) -> None:
    role = await check_role_cached(message.from_user.id) or 0
    if not Permission.has_any_admin_perm(role):
        await state.clear()
        await message.answer(localize("admin.bot_auth.owner_role_missing"))
        return

    await state.clear()
    await _render_admin_menu(message, role=role)
    await log_audit(
        "bot_admin_login",
        user_id=message.from_user.id,
        details="telegram_panel",
    )


@router.message(Command("xyz67"))
async def bot_admin_command(message: Message, state: FSMContext) -> None:
    """Start the owner-only password flow for the in-bot admin panel."""
    user_id = message.from_user.id
    if user_id != EnvKeys.OWNER_ID:
        await message.answer(localize("admin.bot_auth.owner_only"))
        return

    remaining = _lockout_remaining(user_id)
    if remaining:
        await message.answer(localize("admin.bot_auth.locked", seconds=remaining))
        return

    await state.clear()
    await state.set_state(BotAdminFSM.waiting_password)
    await message.answer(localize("admin.bot_auth.prompt"))


@router.message(BotAdminFSM.waiting_password, F.text)
async def process_bot_admin_password(message: Message, state: FSMContext) -> None:
    """Validate the password and open the existing button-based admin menu."""
    user_id = message.from_user.id
    await _delete_secret_message(message)

    if user_id != EnvKeys.OWNER_ID:
        await state.clear()
        await message.answer(localize("admin.bot_auth.owner_only"))
        return

    remaining = _lockout_remaining(user_id)
    if remaining:
        await state.clear()
        await message.answer(localize("admin.bot_auth.locked", seconds=remaining))
        return

    expected = _configured_password()
    supplied = (message.text or "")[:4096]
    if not expected:
        await state.clear()
        await message.answer(localize("admin.bot_auth.unavailable"))
        return

    if not hmac.compare_digest(supplied, expected):
        remaining = _record_failed_attempt(user_id)
        if remaining:
            await state.clear()
            await message.answer(localize("admin.bot_auth.locked", seconds=remaining))
        else:
            await message.answer(localize("admin.bot_auth.invalid"))
        return

    _failed_attempts.pop(user_id, None)
    _locked_until.pop(user_id, None)
    await _open_admin_panel(message, state)
