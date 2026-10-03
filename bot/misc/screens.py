from __future__ import annotations

import asyncio
import json
import os
import re
from html.parser import HTMLParser
from pathlib import Path

from aiogram.types import (
    FSInputFile,
    InputMediaPhoto,
    InputRichBlockParagraph,
    InputRichBlockPhoto,
    InputRichMessage,
    RichTextBold,
    RichTextCode,
    RichTextItalic,
    RichTextMarked,
    RichTextSpoiler,
    RichTextStrikethrough,
    RichTextUnderline,
    RichTextUrl,
)
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from bot.misc.env import EnvKeys
from bot.logger_mesh import logger
from bot.keyboards.styles import colorize_markup


_SAFE_SCREEN = re.compile(r"^[a-z0-9_-]{1,64}$")
_PHOTO_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")
_TELEGRAM_TEXT_LIMIT = 4096
_TELEGRAM_CAPTION_LIMIT = 1024

# Telegram callbacks give us the message being acted on, but a new /start or
# text message does not. Keep the bot's latest rendered screen per private
# chat so a fresh screen can replace it instead of stacking below it.
_SCREEN_MESSAGES: dict[int, list[object]] = {}
# The current artwork key lets a photo screen update only its caption and
# buttons when the banner did not change. That is faster than re-uploading the
# same image for every button press.
_SCREEN_MEDIA_KEYS: dict[int, str | None] = {}
# One lock per chat serializes concurrent renders (double-tapped /start,
# retried Telegram updates). Without it two handlers interleave
# pop-then-send: both miss each other's message and the menu is sent twice.
_SCREEN_LOCKS: dict[int, asyncio.Lock] = {}
_GLOBAL_SCREEN_LOCK: asyncio.Lock | None = None
# Message ids restored from disk after a restart. The in-memory tracking above
# is lost when the process exits, so the first render after a restart would
# otherwise leave the pre-restart menu on screen next to the new one.
_RESTORED_IDS: dict[int, list[int]] = {}
_PERSIST_LOADED = False
_LAST_PERSISTED_BLOB: str | None = None
_PERSIST_FILE = Path("data/screen_messages.json")
_PERSIST_MAX_CHATS = 5000


def _assets_root() -> Path:
    return Path(EnvKeys.UI_ASSETS_DIR).resolve()


def _local_photo(ref: str) -> FSInputFile | None:
    """Resolve a media ref strictly inside UI_ASSETS_DIR."""
    root = _assets_root()
    candidate = (root / ref).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    return FSInputFile(candidate)


def resolve_photo(screen: str, image_ref: str | None = None):
    """Return a URL/file_id/local upload accepted by Telegram, or None."""
    if image_ref:
        ref = image_ref.strip()
        if ref.startswith("https://"):
            return ref
        if "/" not in ref and "\\" not in ref and "." not in ref and len(ref) >= 20:
            return ref  # Telegram file_id
        local = _local_photo(ref)
        if local:
            return local
        # An explicitly supplied but invalid reference must not silently fall
        # back to a same-named screen file. Besides being clearer for admins,
        # this prevents a traversal-like value from selecting an unrelated
        # local image by accident.
        return None

    if not _SAFE_SCREEN.fullmatch(screen):
        return None
    for suffix in _PHOTO_SUFFIXES:
        local = _local_photo(screen + suffix)
        if local:
            return local
    return None


def _message_has_photo(message) -> bool:
    """Return True for Telegram photo messages, without treating mocks as photos."""
    photos = getattr(message, "photo", None)
    return isinstance(photos, (list, tuple)) and bool(photos)


def _chat_id(source) -> int | None:
    """Resolve a private chat id from a Message or CallbackQuery-like object."""
    chat = getattr(source, "chat", None)
    chat_id = getattr(chat, "id", None)
    if isinstance(chat_id, int):
        return chat_id
    user = getattr(source, "from_user", None)
    user_id = getattr(user, "id", None)
    return user_id if isinstance(user_id, int) else None


def _media_key(screen: str, image_ref: str | None, photo) -> str | None:
    """Return a stable local identifier for the artwork shown on a screen."""
    if photo is None:
        return None
    return image_ref.strip() if image_ref else f"screen:{screen}"


def _remember_screen(chat_id: int | None, *messages, media_key: str | None = None) -> None:
    """Remember real Telegram messages; test doubles are intentionally ignored."""
    if chat_id is None:
        return
    valid = [message for message in messages
             if isinstance(getattr(message, "message_id", None), int)]
    if valid:
        _SCREEN_MESSAGES[chat_id] = valid
        _SCREEN_MEDIA_KEYS[chat_id] = media_key
        _persist_save()


def _lock_for(chat_id: int | None) -> asyncio.Lock:
    """Return the mutex serializing screen renders for one chat."""
    global _GLOBAL_SCREEN_LOCK
    if chat_id is None:
        if _GLOBAL_SCREEN_LOCK is None:
            _GLOBAL_SCREEN_LOCK = asyncio.Lock()
        return _GLOBAL_SCREEN_LOCK
    lock = _SCREEN_LOCKS.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _SCREEN_LOCKS[chat_id] = lock
        if len(_SCREEN_LOCKS) > _PERSIST_MAX_CHATS:
            # Locks are only contention guards; dropping the oldest is safe.
            oldest = next(iter(_SCREEN_LOCKS))
            if oldest != chat_id:
                _SCREEN_LOCKS.pop(oldest, None)
    return lock


def _persist_path() -> Path | None:
    """Where screen tracking is persisted, or None when it must stay in memory."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    return _PERSIST_FILE


def _ensure_persist_loaded() -> None:
    """Load pre-restart screen message ids once per process (best-effort)."""
    global _PERSIST_LOADED
    if _PERSIST_LOADED:
        return
    _PERSIST_LOADED = True
    path = _persist_path()
    if path is None:
        return
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return
    try:
        data = json.loads(raw)
    except ValueError:
        return
    if not isinstance(data, dict):
        return
    for chat_raw, ids in data.items():
        try:
            chat_id = int(chat_raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(ids, list):
            continue
        clean = [mid for mid in ids if isinstance(mid, int)][:5]
        if clean:
            _RESTORED_IDS[chat_id] = clean


def _persist_save() -> None:
    """Persist tracked screen ids so the next process can delete them.

    Only writes when the snapshot changed. Merges not-yet-consumed restored
    ids so a second restart before any new screen does not lose them.
    """
    global _LAST_PERSISTED_BLOB
    path = _persist_path()
    if path is None:
        return
    snapshot: dict[str, list[int]] = {}
    for chat, msgs in list(_SCREEN_MESSAGES.items())[:_PERSIST_MAX_CHATS]:
        ids = [mid for mid in (getattr(m, "message_id", None) for m in msgs) if isinstance(mid, int)]
        if ids:
            snapshot[str(chat)] = ids[:5]
    for chat, ids in _RESTORED_IDS.items():
        key = str(chat)
        if key in snapshot:
            merged = snapshot[key] + [mid for mid in ids if mid not in snapshot[key]]
            snapshot[key] = merged[:5]
        else:
            snapshot[key] = ids[:5]
        if len(snapshot) >= _PERSIST_MAX_CHATS:
            break
    blob = json.dumps(snapshot, separators=(",", ":"))
    if blob == _LAST_PERSISTED_BLOB:
        return
    _LAST_PERSISTED_BLOB = blob
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(blob, encoding="utf-8")
    except OSError as exc:
        logger.debug("Could not persist screen tracking: %s", exc)


def _take_restored_ids(chat_id: int | None) -> list[int]:
    """Consume pre-restart message ids for a chat (one-shot)."""
    if chat_id is None:
        return []
    return _RESTORED_IDS.pop(chat_id, [])


async def _delete_messages_by_id(bot, chat_id: int, message_ids: list[int]) -> None:
    """Delete messages by id, ignoring stale/network failures (best-effort)."""
    for mid in message_ids:
        try:
            await bot.delete_message(chat_id, mid)
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            logger.debug("Could not delete restored screen message %s:%s: %s", chat_id, mid, exc)
        except Exception as exc:
            logger.debug("Could not delete restored screen message %s:%s: %s", chat_id, mid, exc)


async def _edit_text_ignoring_not_modified(message, text: str, *, reply_markup) -> None:
    """Edit a text message, treating an identical re-render as success.

    Double-tapped menu buttons otherwise raise TelegramBadRequest and leave
    the callback hanging.
    """
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


def _text_parts(text: str | list[str]) -> list[str]:
    """Normalize one screen or a deliberately split long screen."""
    parts = [text] if isinstance(text, str) else list(text)
    if not parts or any(not isinstance(part, str) for part in parts):
        raise ValueError("A screen must contain at least one text part")
    if any(len(part) > _TELEGRAM_TEXT_LIMIT for part in parts):
        raise ValueError("A Telegram screen text part exceeds 4096 characters")
    return parts


class _RichTextHTMLParser(HTMLParser):
    """Convert the HTML used by screens into Telegram rich-text entities."""

    _BLOCK_TAGS = {
        "blockquote", "div", "h1", "h2", "h3", "h4", "h5", "h6",
        "li", "ol", "p", "pre", "ul",
    }
    _WRAPPERS = {
        "b": RichTextBold,
        "strong": RichTextBold,
        "code": RichTextCode,
        "del": RichTextStrikethrough,
        "em": RichTextItalic,
        "i": RichTextItalic,
        "mark": RichTextMarked,
        "s": RichTextStrikethrough,
        "strike": RichTextStrikethrough,
        "u": RichTextUnderline,
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._frames = [(None, {}, [])]

    @property
    def _children(self):
        return self._frames[-1][2]

    def _line_break(self):
        children = self._children
        if children and not (isinstance(children[-1], str) and children[-1].endswith("\n")):
            children.append("\n")

    @staticmethod
    def _value(children):
        if len(children) == 1:
            return children[0]
        return children

    def _close_frame(self):
        tag, attrs, children = self._frames.pop()
        value = self._value(children)
        wrapper = self._WRAPPERS.get(tag)
        if tag == "a" and attrs.get("href"):
            value = RichTextUrl(text=value, url=attrs["href"])
        elif tag == "tg-spoiler" or (
            tag == "span" and "tg-spoiler" in attrs.get("class", "").split()
        ):
            value = RichTextSpoiler(text=value)
        elif wrapper is not None:
            value = wrapper(text=value)

        if wrapper is None and tag not in {"a", "tg-spoiler"} and not (
            tag == "span" and "tg-spoiler" in attrs.get("class", "").split()
        ):
            self._children.extend(children)
        else:
            self._children.append(value)

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs = {key.lower(): value or "" for key, value in attrs}
        if tag in {"br", "hr"}:
            self._line_break()
            return
        if tag in self._BLOCK_TAGS:
            self._line_break()
            if tag == "li":
                self._children.append("• ")
        self._frames.append((tag, attrs, []))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in {"br", "hr"}:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        frame_index = next(
            (index for index in range(len(self._frames) - 1, 0, -1)
             if self._frames[index][0] == tag),
            None,
        )
        if frame_index is None:
            if tag in self._BLOCK_TAGS:
                self._line_break()
            return
        while len(self._frames) - 1 >= frame_index:
            self._close_frame()
        if tag in self._BLOCK_TAGS:
            self._line_break()

    def handle_data(self, data):
        self._children.append(data)

    def rich_text(self, source: str):
        self.feed(source)
        self.close()
        while len(self._frames) > 1:
            self._close_frame()
        return self._value(self._children) or source


def _rich_photo_content(photo, parts: list[str]) -> InputRichMessage:
    """Build a single rich Telegram message containing artwork and full text."""
    blocks = [InputRichBlockPhoto(photo=InputMediaPhoto(media=photo))]
    for part in parts:
        rich_text = _RichTextHTMLParser().rich_text(part)
        if rich_text:
            blocks.append(InputRichBlockParagraph(text=rich_text))
    return InputRichMessage(blocks=blocks)


async def _try_send_rich_photo_screen(source, photo, parts, *, reply_markup):
    """Send a photo and its full text as one rich message when supported."""
    bot = getattr(source, "bot", None)
    chat_id = _chat_id(source)
    send_rich_message = getattr(bot, "send_rich_message", None)
    if bot is None or chat_id is None or send_rich_message is None:
        return None
    try:
        return await send_rich_message(
            chat_id=chat_id,
            rich_message=_rich_photo_content(photo, parts),
            reply_markup=reply_markup,
        )
    except TelegramBadRequest as exc:
        logger.warning("Telegram rejected a combined rich screen; keeping the full text separately: %s", exc)
        return None


async def _try_edit_rich_photo_screen(message, photo, parts, *, reply_markup):
    """Replace a text or rich screen with one rich photo-and-text message."""
    bot = getattr(message, "bot", None)
    chat_id = _chat_id(message)
    message_id = getattr(message, "message_id", None)
    edit_message_text = getattr(bot, "edit_message_text", None)
    if bot is None or chat_id is None or not isinstance(message_id, int) or edit_message_text is None:
        return False
    try:
        await edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            rich_message=_rich_photo_content(photo, parts),
            reply_markup=reply_markup,
        )
        return True
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return True
        logger.warning("Could not edit a screen into a combined rich message: %s", exc)
        return False


async def _send_photo_text_fallback(source, photo, parts, *, reply_markup):
    """Legacy fallback used only when Telegram rejects rich-message delivery."""
    if len(parts) == 1 and len(parts[0]) <= _TELEGRAM_CAPTION_LIMIT:
        return [await source.answer_photo(
            photo,
            caption=parts[0],
            reply_markup=reply_markup,
            parse_mode="HTML",
        )]
    image_message = await source.answer_photo(photo)
    text_messages = await _send_text_parts(source, parts, reply_markup=reply_markup)
    return [image_message, *text_messages]


async def _send_text_parts(source, parts: list[str], *, reply_markup, edit_first: bool = False):
    """Send a split screen, putting its keyboard only on the final message."""
    rendered = []
    for index, part in enumerate(parts):
        markup = reply_markup if index == len(parts) - 1 else None
        if index == 0 and edit_first:
            await _edit_text_ignoring_not_modified(source, part, reply_markup=markup)
            rendered.append(source)
        else:
            rendered.append(await source.answer(
                part,
                reply_markup=markup,
                parse_mode="HTML",
            ))
    return rendered


async def _clear_or_replace_photo(message, photo, *, replace: bool) -> None:
    """Remove an over-limit caption and leave artwork without a stale keyboard."""
    try:
        if replace:
            await message.edit_media(
                media=InputMediaPhoto(media=photo),
                reply_markup=None,
            )
        else:
            await message.edit_caption(
                caption="",
                reply_markup=None,
                parse_mode="HTML",
            )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


def _tracked_photo(chat_id: int | None, current_message):
    for message in _SCREEN_MESSAGES.get(chat_id, []) if chat_id is not None else []:
        if _message_marker(message) != _message_marker(current_message) and _message_has_photo(message):
            return message
    return None


def _message_marker(message) -> tuple:
    """Identify Telegram message objects even when callbacks deserialize new instances."""
    message_id = getattr(message, "message_id", None)
    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None)
    if isinstance(message_id, int) and isinstance(chat_id, int):
        return ("telegram", chat_id, message_id)
    return ("object", id(message))


async def _delete_screen_messages(
        chat_id: int | None, *current_messages, keep=()
) -> None:
    """Delete tracked/current screens once each, optionally preserving a message."""
    messages = list(_SCREEN_MESSAGES.pop(chat_id, [])) if chat_id is not None else []
    if chat_id is not None:
        _SCREEN_MEDIA_KEYS.pop(chat_id, None)
    messages.extend(message for message in current_messages if message is not None)
    keep_markers = {_message_marker(message) for message in keep}
    seen: set[tuple] = set()
    for message in messages:
        marker = _message_marker(message)
        if marker in seen or marker in keep_markers:
            continue
        seen.add(marker)
        try:
            await message.delete()
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            # A stale message can be older than Telegram's deletion window.
            # Sending the fresh screen is still safe and leaves the old message
            # untouched rather than breaking navigation.
            logger.debug("Could not delete previous screen message: %s", exc)
    _persist_save()


async def _delete_previous_screen(call) -> None:
    """Delete a media screen before replacing it with another message type."""
    await _delete_screen_messages(_chat_id(call), call.message)


async def answer_screen(message, text: str | list[str], *, reply_markup=None, screen: str, image_ref: str | None = None):
    reply_markup = colorize_markup(reply_markup, uniform="success" if screen == "main-menu" else None)
    parts = _text_parts(text)
    chat_id = _chat_id(message)
    async with _lock_for(chat_id):
        _ensure_persist_loaded()
        restored = _take_restored_ids(chat_id)
        if restored and chat_id is not None:
            bot = getattr(message, "bot", None)
            if bot is not None:
                await _delete_messages_by_id(bot, chat_id, restored)
        await _delete_screen_messages(chat_id)
        photo = resolve_photo(screen, image_ref)
        media_key = _media_key(screen, image_ref, photo)
        if photo is None:
            rendered = await _send_text_parts(message, parts, reply_markup=reply_markup)
            _remember_screen(chat_id, *rendered, media_key=None)
            return rendered[-1]
        if len(parts) == 1 and len(parts[0]) <= _TELEGRAM_CAPTION_LIMIT:
            rendered = await message.answer_photo(
                photo, caption=parts[0], reply_markup=reply_markup, parse_mode="HTML"
            )
            _remember_screen(chat_id, rendered, media_key=media_key)
            return rendered
        rich_message = await _try_send_rich_photo_screen(
            message, photo, parts, reply_markup=reply_markup
        )
        if rich_message is not None:
            _remember_screen(chat_id, rich_message, media_key=media_key)
            return rich_message
        image_message = await message.answer_photo(photo)
        text_messages = await _send_text_parts(message, parts, reply_markup=reply_markup)
        _remember_screen(chat_id, image_message, *text_messages, media_key=media_key)
        return text_messages[-1]


async def edit_screen(call, text: str | list[str], *, reply_markup=None, screen: str, image_ref: str | None = None):
    """Change the active bot screen in place whenever Telegram allows it.

    A media message can edit its photo or caption, but Telegram cannot turn a
    text message into a photo (or remove a photo from a message). For that
    boundary we keep the existing message and update its text/caption instead
    of deleting it and sending a replacement.
    """
    reply_markup = colorize_markup(reply_markup, uniform="success" if screen == "main-menu" else None)
    parts = _text_parts(text)
    chat_id = _chat_id(call)
    async with _lock_for(chat_id):
        _ensure_persist_loaded()
        # Tapping a button on a pre-restart duplicate heals the chat: every
        # other restored message is removed, the tapped one is edited in place.
        restored = _take_restored_ids(chat_id)
        current_id = getattr(getattr(call, "message", None), "message_id", None)
        if restored:
            others = [mid for mid in restored if mid != current_id] if isinstance(current_id, int) else restored
            if others and chat_id is not None:
                bot = getattr(getattr(call, "message", None), "bot", None) or getattr(call, "bot", None)
                if bot is not None:
                    await _delete_messages_by_id(bot, chat_id, others)
        photo = resolve_photo(screen, image_ref)
        target_media_key = _media_key(screen, image_ref, photo)
        source_is_photo = _message_has_photo(call.message)
        source_is_rich = getattr(call.message, "rich_message", None) is not None

        if source_is_rich:
            if photo is None:
                text_messages = await _send_text_parts(
                    call.message, parts, reply_markup=reply_markup, edit_first=True
                )
                _remember_screen(chat_id, *text_messages, media_key=None)
                return text_messages[-1]

            if await _try_edit_rich_photo_screen(
                call.message, photo, parts, reply_markup=reply_markup
            ):
                await _delete_screen_messages(chat_id, call.message, keep=(call.message,))
                _remember_screen(chat_id, call.message, media_key=target_media_key)
                return call.message

            rendered = await _send_photo_text_fallback(
                call.message, photo, parts, reply_markup=reply_markup
            )
            await _delete_screen_messages(chat_id, call.message, keep=rendered)
            _remember_screen(chat_id, *rendered, media_key=target_media_key)
            return rendered[-1]

        if photo is not None and not source_is_photo:
            if len(parts) == 1 and len(parts[0]) <= _TELEGRAM_CAPTION_LIMIT:
                rendered = [await call.message.answer_photo(
                    photo,
                    caption=parts[0],
                    reply_markup=reply_markup,
                    parse_mode="HTML",
                )]
            else:
                rendered = []
                if await _try_edit_rich_photo_screen(
                    call.message, photo, parts, reply_markup=reply_markup
                ):
                    await _delete_screen_messages(chat_id, call.message, keep=(call.message,))
                    _remember_screen(chat_id, call.message, media_key=target_media_key)
                    return call.message
                rich_message = await _try_send_rich_photo_screen(
                    call.message, photo, parts, reply_markup=reply_markup
                )
                if rich_message is not None:
                    rendered = [rich_message]
                else:
                    rendered = await _send_photo_text_fallback(
                        call.message, photo, parts, reply_markup=reply_markup
                    )

            await _delete_screen_messages(chat_id, call.message, keep=rendered)
            _remember_screen(chat_id, *rendered, media_key=target_media_key)
            return rendered[-1]

        if photo is None:
            if source_is_photo and len(parts) == 1 and len(parts[0]) <= _TELEGRAM_CAPTION_LIMIT:
                try:
                    await call.message.edit_caption(
                        caption=parts[0], reply_markup=reply_markup, parse_mode="HTML"
                    )
                except TelegramBadRequest as exc:
                    if "message is not modified" not in str(exc).lower():
                        raise
                _remember_screen(
                    chat_id,
                    call.message,
                    media_key=_SCREEN_MEDIA_KEYS.get(chat_id),
                )
                return call.message

            if source_is_photo:
                current_media_key = _SCREEN_MEDIA_KEYS.get(chat_id)
                await _clear_or_replace_photo(call.message, None, replace=False)
                text_messages = await _send_text_parts(
                    call.message, parts, reply_markup=reply_markup
                )
                _remember_screen(
                    chat_id,
                    call.message,
                    *text_messages,
                    media_key=current_media_key,
                )
                return text_messages[-1]

            if _tracked_photo(chat_id, call.message) is not None:
                await _delete_screen_messages(chat_id, call.message, keep=(call.message,))
            text_messages = await _send_text_parts(
                call.message, parts, reply_markup=reply_markup, edit_first=True
            )
            _remember_screen(chat_id, *text_messages, media_key=None)
            return text_messages[-1]

        if source_is_photo:
            current_media_key = _SCREEN_MEDIA_KEYS.get(chat_id)
            if len(parts) == 1 and len(parts[0]) <= _TELEGRAM_CAPTION_LIMIT:
                try:
                    if current_media_key == target_media_key:
                        await call.message.edit_caption(
                            caption=parts[0], reply_markup=reply_markup, parse_mode="HTML"
                        )
                    else:
                        await call.message.edit_media(
                            media=InputMediaPhoto(media=photo, caption=parts[0], parse_mode="HTML"),
                            reply_markup=reply_markup,
                        )
                except TelegramBadRequest as exc:
                    if "message is not modified" not in str(exc).lower():
                        raise
                _remember_screen(chat_id, call.message, media_key=target_media_key)
                return call.message

            rich_message = await _try_send_rich_photo_screen(
                call.message, photo, parts, reply_markup=reply_markup
            )
            if rich_message is not None:
                await _delete_screen_messages(chat_id, call.message, keep=(rich_message,))
                _remember_screen(chat_id, rich_message, media_key=target_media_key)
                return rich_message

            await _clear_or_replace_photo(
                call.message,
                photo,
                replace=current_media_key != target_media_key,
            )
            text_messages = await _send_text_parts(
                call.message, parts, reply_markup=reply_markup
            )
            _remember_screen(chat_id, call.message, *text_messages, media_key=target_media_key)
            return text_messages[-1]

        # A long card is represented by a photo plus one or more text messages.
        # Keep that companion artwork when a callback is pressed on the text.
        photo_message = _tracked_photo(chat_id, call.message)
        current_media_key = _SCREEN_MEDIA_KEYS.get(chat_id)
        if photo_message is not None:
            # Remove stale continuation messages while keeping the active photo
            # and the text message carrying the callback keyboard.
            await _delete_screen_messages(
                chat_id,
                call.message,
                keep=(photo_message, call.message),
            )
            if current_media_key != target_media_key:
                try:
                    await photo_message.edit_media(
                        media=InputMediaPhoto(media=photo),
                        reply_markup=None,
                    )
                except TelegramBadRequest as exc:
                    if "message is not modified" not in str(exc).lower():
                        raise
        text_messages = await _send_text_parts(
            call.message, parts, reply_markup=reply_markup, edit_first=True
        )
        if photo_message is not None:
            _remember_screen(
                chat_id,
                photo_message,
                *text_messages,
                media_key=target_media_key,
            )
        else:
            # Telegram cannot turn an existing text message into a photo.
            _remember_screen(chat_id, *text_messages, media_key=None)
        return text_messages[-1]
