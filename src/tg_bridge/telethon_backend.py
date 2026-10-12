"""The Telethon backend: the bridge's own connection to Telegram.

It logs in with the session file `tg-bridge login` wrote, reads the chat with
get_messages, is woken by Telethon's NewMessage updates and sends with
send_message. Its records are the same as the MCP backend's, so the bridge
treats both alike.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import OrderedDict
from collections.abc import Callable, Iterator
from datetime import UTC
from typing import Any

from telethon import TelegramClient, errors, events, utils
from telethon.errors.common import AuthKeyNotFound
from telethon.sessions import StringSession
from telethon.tl import types

from tg_bridge.backend import Forwarded, History, Record
from tg_bridge.config import ConfigError
from tg_bridge.errors import AuthError, ChatAccessError, ToolError, TransportError
from tg_bridge.session import SessionData
from tg_bridge.text import normalize, utf16_len

log = logging.getLogger("tg_bridge")

FLOOD_SLEEP_SECONDS = 60  # Telethon sleeps through shorter flood waits by itself
SEND_TIMEOUT = 115.0  # longer than a flood wait Telethon sleeps through
REQUEST_TIMEOUT = 60.0
DIALOGS_TIMEOUT = 300.0  # the whole dialog list of a big account takes a while
DEVICE_MODEL = "agora-tg-bridge"
SENDERS_KEPT = 2000  # senders whose profile photo may be asked for

# Errors that mean the account may not read or write the chat.
ACCESS_ERRORS: tuple[type[Exception], ...] = (
    errors.ChannelPrivateError,
    errors.ChatForbiddenError,
    errors.ChatAdminRequiredError,
    errors.ChatWriteForbiddenError,
    errors.UserBannedInChannelError,
    errors.ForbiddenError,
)


def make_client(session: SessionData) -> TelegramClient:
    return TelegramClient(
        StringSession(session.session),
        session.api_id,
        session.api_hash,
        flood_sleep_threshold=FLOOD_SLEEP_SECONDS,
        device_model=DEVICE_MODEL,
        # Raise Telegram's own error after the retries (ServerError and kin),
        # not a bare ValueError the bridge could not sort.
        raise_last_call_error=True,
    )


@contextlib.contextmanager
def telegram_errors(what: str) -> Iterator[None]:
    """Sort Telethon's errors into the bridge's."""
    try:
        yield
    except (errors.UnauthorizedError, errors.AuthKeyDuplicatedError, AuthKeyNotFound) as exc:
        raise AuthError(
            f"Telegram refused the session ({type(exc).__name__}); run tg-bridge login"
        ) from exc
    except ACCESS_ERRORS as exc:
        raise ChatAccessError(f"{what}: chat restricted ({type(exc).__name__})") from exc
    except errors.FloodWaitError as exc:
        raise ToolError(f"{what}: flood wait of {exc.seconds} s") from exc
    except (errors.ServerError, errors.TimedOutError) as exc:
        # Telegram may have done it: a send whose retry was refused as
        # RANDOM_ID_DUPLICATE did go out. Check, do not report a refusal.
        raise TransportError(f"{what}: {type(exc).__name__}") from exc
    except errors.RPCError as exc:  # other 406 errors are refusals too, not a bad session
        raise ToolError(f"{what}: {type(exc).__name__}") from exc
    except (ConnectionError, OSError, TimeoutError) as exc:
        raise TransportError(f"{what}: {type(exc).__name__}") from exc


def chat_type(entity: Any) -> str:
    if isinstance(entity, types.User):
        return "private"
    if isinstance(entity, (types.Chat, types.ChatForbidden)):
        return "group"
    if isinstance(entity, (types.Channel, types.ChannelForbidden)):
        return "supergroup" if getattr(entity, "megagroup", False) else "channel"
    return ""


def _display_name(entity: Any) -> str:
    return utils.get_display_name(entity) if entity is not None else ""


def media_label(msg: Any) -> str:
    """The media kind as telegram-mcp names it, or ""."""
    if msg.media is None or isinstance(msg.media, types.MessageMediaWebPage):
        return ""  # a link preview is not an attachment
    if msg.sticker is not None:
        alt = next(
            (a.alt for a in msg.sticker.attributes if getattr(a, "alt", None)),
            "",
        )
        return f"sticker {alt}".strip()
    if msg.photo is not None:
        return "photo"
    for kind in ("voice", "video_note", "gif", "video", "audio"):  # a GIF is a video too
        if getattr(msg, kind) is not None:
            return kind
    if msg.document is not None:
        name = msg.file.name if msg.file is not None else None
        return f"document: {name}" if name else "document"
    if msg.contact is not None:
        return "contact"
    if msg.geo is not None:
        return "geo"
    if msg.poll is not None:
        return "poll"
    return "media"


def _poll_question(msg: Any) -> str:
    question = msg.poll.poll.question
    return getattr(question, "text", question) or ""


def photo_rev(entity: Any) -> str:
    """The id of a user's or chat's current profile photo, "" when it has none
    (or hides it)."""
    photo = getattr(entity, "photo", None)
    if isinstance(photo, (types.UserProfilePhoto, types.ChatPhoto)):
        return str(photo.photo_id)
    return ""


def _iso(value: Any) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _forwarded(msg: Any) -> Forwarded:
    info: dict[str, str] = {}
    if msg.fwd_from.date is not None:
        info["date"] = _iso(msg.fwd_from.date)  # keeps the record non-empty
    fwd = msg.forward
    if fwd is None:
        return info
    if fwd.sender is not None:
        info["from_user"] = _display_name(fwd.sender)
    elif fwd.chat is not None:
        info["from_chat"] = _display_name(fwd.chat)
    elif msg.fwd_from.from_name:
        info["from_name"] = msg.fwd_from.from_name
    return Forwarded(**{k: v for k, v in info.items() if v})


def to_record(msg: Any, kind: str) -> Record:
    """A Telethon message as a backend record (see backend.Record)."""
    rec: Record = {
        "id": msg.id,
        "sender": _display_name(msg.sender) or "Unknown",
        "out": bool(msg.out),
    }
    if msg.sender_id is not None:
        rec["sender_id"] = msg.sender_id
    if msg.date is not None:
        rec["date"] = _iso(msg.date)
    if isinstance(msg, types.MessageService):
        rec["action"] = type(msg.action).__name__
        return rec  # a service message only moves the cursor
    if msg.sender is not None:
        rec["sender_photo"] = photo_rev(msg.sender)
    text = normalize(msg.message or "")  # as telegram-mcp cleans it
    if text:
        rec["text"] = text
    media = media_label(msg)
    if media:
        rec["media"] = media
        duration = getattr(msg.file, "duration", None) if msg.file is not None else None
        if duration is not None and media in ("voice", "video_note"):
            rec["duration"] = int(duration)
        if media == "poll":
            rec["poll"] = {"question": _poll_question(msg)}
    if msg.fwd_from is not None:
        rec["forwarded"] = _forwarded(msg)
    if msg.reply_to is not None and getattr(msg.reply_to, "reply_to_msg_id", None):
        rec["reply_to"] = msg.reply_to.reply_to_msg_id
    emojis = [
        {"id": str(e.document_id)}
        for e in msg.entities or []
        if isinstance(e, types.MessageEntityCustomEmoji)
    ]
    if emojis:
        rec["custom_emojis"] = emojis
    if kind == "private":
        rec["addressed"] = not msg.out
    elif kind in ("group", "supergroup"):
        rec["addressed"] = bool(msg.mentioned) and not msg.out
    else:
        rec["addressed"] = False
    return rec


class TelethonBackend:
    def __init__(
        self,
        session: SessionData,
        *,
        client_factory: Callable[[SessionData], Any] = make_client,
        send_timeout: float = SEND_TIMEOUT,
    ) -> None:
        self._client = client_factory(session)
        self._send_timeout = send_timeout
        self._entity: Any = None
        self._chat_id: int | None = None
        self._chat_type = ""
        self._woken = asyncio.Event()
        # The senders read in the chat, newest last: their photos are
        # downloaded through these, which also works for min users.
        self._senders: OrderedDict[int, Any] = OrderedDict()

    async def start(self) -> None:
        with telegram_errors("connect"):
            await asyncio.wait_for(self._client.connect(), REQUEST_TIMEOUT)
            authorized = await asyncio.wait_for(self._client.is_user_authorized(), REQUEST_TIMEOUT)
        if not authorized:
            raise ConfigError("the session is not logged in; run `tg-bridge login`")
        self._client.add_event_handler(self._on_new_message, events.NewMessage())

    async def aclose(self) -> None:
        with contextlib.suppress(Exception):
            await self._client.disconnect()

    async def _on_new_message(self, event: Any) -> None:
        if self._chat_id is None or event.chat_id == self._chat_id:
            self._woken.set()

    async def _resolve(self, chat: int) -> Any:
        if self._entity is not None:
            return self._entity
        with telegram_errors("resolve the chat"):
            try:
                entity = await asyncio.wait_for(self._client.get_entity(chat), REQUEST_TIMEOUT)
            except ValueError:
                # Not in this session's cache yet: the dialogs list fills it.
                await asyncio.wait_for(self._client.get_dialogs(), DIALOGS_TIMEOUT)
                try:
                    entity = await asyncio.wait_for(self._client.get_entity(chat), REQUEST_TIMEOUT)
                except ValueError:
                    raise ConfigError(
                        "this account has no chat with that id (see `tg-bridge chats`)"
                    ) from None
        self._entity = entity
        self._chat_id = utils.get_peer_id(entity)
        self._chat_type = chat_type(entity)
        return entity

    def _check_connected(self) -> None:
        if not self._client.is_connected():
            raise TransportError("disconnected from Telegram")

    async def messages_after(self, chat: int, after_id: int | None, limit: int) -> History:
        self._check_connected()
        entity = await self._resolve(chat)
        with telegram_errors("read the chat"):
            if after_id is not None and after_id >= 0:
                found = await asyncio.wait_for(
                    self._client.get_messages(entity, limit=limit, min_id=after_id, reverse=True),
                    REQUEST_TIMEOUT,
                )
                found = [m for m in found if m.id > after_id]
            else:
                found = list(
                    reversed(
                        await asyncio.wait_for(
                            self._client.get_messages(entity, limit=limit), REQUEST_TIMEOUT
                        )
                    )
                )
        messages = [m for m in found if isinstance(m, types.Message | types.MessageService)]
        for m in messages:
            if m.sender is not None and m.sender_id is not None:
                self._remember_sender(m.sender_id, m.sender)
        return History(
            chat_id=self._chat_id,
            chat_type=self._chat_type,
            messages=[to_record(m, self._chat_type) for m in messages],
            has_more=len(found) >= limit,
        )

    def _remember_sender(self, sender_id: int, entity: Any) -> None:
        self._senders[sender_id] = entity
        self._senders.move_to_end(sender_id)
        if len(self._senders) > SENDERS_KEPT:
            self._senders.popitem(last=False)

    async def profile_photo(self, sender_id: int) -> bytes | None:
        self._check_connected()
        entity = self._senders.get(sender_id, sender_id)
        if not isinstance(entity, int) and not photo_rev(entity):
            return None
        with telegram_errors("download a profile photo"):
            try:
                data = await asyncio.wait_for(
                    self._client.download_profile_photo(entity, file=bytes, download_big=False),
                    REQUEST_TIMEOUT,
                )
            except ValueError:  # not in the session's cache any more
                raise ToolError("download a profile photo: unknown sender") from None
        if not data:
            # Telethon answers some refused downloads, and a peer without a
            # photo asked for by id, with nothing: not a known "no photo".
            raise ToolError("download a profile photo: nothing downloaded")
        return data

    async def wait_for_events(self, chat_id: int, timeout: float) -> bool:
        self._check_connected()
        if timeout > 0 and not self._woken.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._woken.wait(), timeout)
        self._check_connected()
        woken = self._woken.is_set()
        self._woken.clear()
        return woken

    async def send(
        self, chat_id: int, text: str, *, reply_to: int | None, custom_emoji_id: str | None
    ) -> int | None:
        self._check_connected()
        entity = await self._resolve(chat_id)
        entities = None
        if custom_emoji_id is not None:
            entities = [
                types.MessageEntityCustomEmoji(
                    offset=0, length=utf16_len(text[0]), document_id=int(custom_emoji_id)
                )
            ]
        try:
            with telegram_errors("send"):
                sent = await asyncio.wait_for(
                    self._client.send_message(
                        entity,
                        text,
                        reply_to=reply_to,
                        parse_mode=None,
                        formatting_entities=entities,
                    ),
                    self._send_timeout,
                )
        except ChatAccessError as exc:
            raise ToolError(str(exc)) from exc  # a refused send, not a fatal error
        return getattr(sent, "id", None)
