"""What the bridge needs from Telegram, whichever way it is reached."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, TypedDict

CHAT_TYPES = ("private", "group", "supergroup", "channel")


class Forwarded(TypedDict, total=False):
    date: str
    from_user: str
    from_chat: str
    from_name: str


class Record(TypedDict, total=False):
    """One message as the backends report it (the record of telegram-mcp's
    get_messages_after; `render.in_line` turns it into an `in` line)."""

    id: int  # always present
    sender: str
    sender_id: int
    date: str  # ISO 8601, UTC
    text: str
    out: bool  # the account's own message
    addressed: bool  # to the account owner
    reply_to: int
    media: str  # photo, voice, video_note, video, audio, gif, document[: name], sticker [alt], …
    duration: int  # seconds, for voice and video messages
    poll: dict[str, Any]  # {"question": …}
    forwarded: Forwarded
    transcript: str  # a machine transcript of a voice message
    action: str  # set on service messages
    custom_emojis: list[dict[str, str]]  # [{"id": document id, …}]
    sender_photo: str  # the sender's profile photo by an opaque id, "" for none


@dataclass(frozen=True)
class History:
    chat_id: int  # the chat's marked id (-100… for supergroups and channels)
    chat_type: str  # one of CHAT_TYPES
    messages: list[Record]  # oldest first
    has_more: bool


class Backend(Protocol):
    """One connection to Telegram; the bridge makes a new one after a TransportError.

    Errors: TransportError (connection; from `send`, the outcome is unknown),
    ToolError (Telegram refused; from `send`, nothing was sent), ChatAccessError
    (the chat may not be read), ConfigError and AuthError (fatal)."""

    async def start(self) -> None:
        """Connect and check that the backend can work at all."""

    async def messages_after(self, chat: int, after_id: int | None, limit: int) -> History:
        """Messages with an id above `after_id`, oldest first; without it the
        newest `limit` messages."""

    async def wait_for_events(self, chat_id: int, timeout: float) -> bool:
        """Wait up to `timeout` seconds for something new in the chat; True when
        the chat should be read. A zero timeout only marks the current position."""

    async def send(
        self, chat_id: int, text: str, *, reply_to: int | None, custom_emoji_id: str | None
    ) -> int | None:
        """Send plain text, the first character as the custom emoji when given;
        returns the new message id when known."""

    async def profile_photo(self, sender_id: int) -> bytes | None:
        """The small profile photo of a sender seen in the chat, None when it has
        none. A photo that exists but cannot be downloaded is a ToolError, so
        the bridge does not record the person as having none. Asked only for
        records that carry `sender_photo`."""

    async def aclose(self) -> None: ...
