"""The MCP backend: telegram-mcp's event tools behind an MCP hub."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from typing import Any, Protocol

from tg_bridge.backend import History
from tg_bridge.config import ConfigError
from tg_bridge.errors import ChatAccessError, ToolError

log = logging.getLogger("tg_bridge")

REQUIRED_TOOLS = ("wait_for_events", "get_messages_after", "send_message")
PHOTO_TOOL = "get_profile_photo"  # optional: without it the bridge sends no avatars
READ_TIMEOUT = 60.0
WAIT_SLACK = 30.0  # the HTTP read may last this much longer than the wait itself
SEND_READ_TIMEOUT = 115.0  # Telethon may sleep through a 60 s flood wait; the hub allows 120 s
# What get_messages_after's description says when its records name forum topics
# (`topic` per message, one `topics` map of titles per result).
TOPICS_MARK = "`topics`"
GENERAL_TOPIC = 1  # a forum's General topic: its messages are the room's
SENT_OK = "Message sent successfully"
_SENT_ID = re.compile(r"message_id=(\d+)")


class McpClient(Protocol):
    async def list_tools(self) -> dict[str, str]: ...

    async def call_tool(
        self, name: str, arguments: dict[str, Any], *, read_timeout: float = 60.0
    ) -> str: ...

    async def aclose(self) -> None: ...


def _short(text: str, limit: int = 200) -> str:
    return " ".join(text.split())[:limit] or "unknown error"


class McpBackend:
    def __init__(self, client: McpClient) -> None:
        self._client = client
        self._boot: str | None = None
        self._after_seq = 0
        self._photos = False  # the endpoint offers PHOTO_TOOL
        self.topics = False  # get_messages_after names forum topics

    async def start(self) -> None:
        tools = await self._client.list_tools()
        missing = [name for name in REQUIRED_TOOLS if name not in tools]
        if missing:
            raise ConfigError(
                "the Telegram MCP endpoint does not offer "
                + ", ".join(missing)
                + " (telegram-mcp with the event log, and the hub's tool filter)"
            )
        self._photos = PHOTO_TOOL in tools
        if not self._photos:
            log.info("the endpoint does not offer %s: no avatars", PHOTO_TOOL)
        self.topics = TOPICS_MARK in tools["get_messages_after"]
        if not self.topics:
            log.info("get_messages_after does not name forum topics: a forum is one room")

    async def aclose(self) -> None:
        await self._client.aclose()

    async def messages_after(self, chat: int, after_id: int | None, limit: int) -> History:
        args: dict[str, Any] = {"chat_id": chat, "limit": limit}
        if after_id is not None:
            args["after_id"] = after_id
        try:
            text = await self._client.call_tool(
                "get_messages_after", args, read_timeout=READ_TIMEOUT
            )
        except ToolError as exc:
            if "restricted" in str(exc).lower():
                raise ChatAccessError(str(exc)) from exc
            raise
        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            if "restricted" in text.lower():
                raise ChatAccessError(_short(text)) from None
            raise ToolError(_short(text)) from None
        if not isinstance(result, dict) or not isinstance(result.get("messages"), list):
            raise ToolError("unexpected get_messages_after result")
        messages = [r for r in result["messages"] if isinstance(r, dict)]
        if not self._photos:
            # A photo id the bridge cannot fetch would only drop stored avatars.
            for rec in messages:
                rec.pop("sender_photo", None)
        self._name_topics(messages, result.get("topics"))
        try:
            return History(
                chat_id=int(result["chat_id"]),
                chat_type=str(result.get("chat_type") or ""),
                messages=messages,
                has_more=bool(result.get("has_more")),
            )
        except (KeyError, TypeError, ValueError):
            raise ToolError("unexpected get_messages_after result") from None

    def _name_topics(self, messages: list[Any], titles: object) -> None:
        """Give each record in a topic its title from the result's `topics` map
        (topic id to title, sent once per result). The General topic is the
        room's, so its records carry no topic. Records of a server that does not
        name topics are left as they are."""
        if not self.topics:
            for rec in messages:
                rec.pop("topic", None)
            return
        names = (
            {str(k): v for k, v in titles.items() if isinstance(v, str)}
            if isinstance(titles, dict)
            else {}
        )
        for rec in messages:
            topic = rec.get("topic")
            if not isinstance(topic, int) or isinstance(topic, bool) or topic == GENERAL_TOPIC:
                rec.pop("topic", None)
            else:
                rec["topic_title"] = names.get(str(topic), "")

    async def wait_for_events(self, chat_id: int, timeout: float) -> bool:
        args: dict[str, Any] = {
            "chat_id": chat_id,
            "after_seq": self._after_seq,
            "timeout": timeout,
        }
        if self._boot:
            args["boot"] = self._boot
        text = await self._client.call_tool(
            "wait_for_events", args, read_timeout=timeout + WAIT_SLACK
        )
        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            raise ToolError(_short(text)) from None
        if not isinstance(result, dict):
            raise ToolError("unexpected wait_for_events result")
        if result.get("reset") and self._boot is not None:
            log.info("telegram-mcp restarted; reading the chat again")
        self._boot = result.get("boot") or self._boot
        self._after_seq = int(result.get("last_seq") or 0)
        return bool(result.get("events") or result.get("gap") or result.get("reset"))

    async def send(
        self, chat_id: int, text: str, *, reply_to: int | None, custom_emoji_id: str | None
    ) -> int | None:
        args: dict[str, Any] = {"chat_id": chat_id, "message": text}
        if custom_emoji_id is not None:
            args["custom_emoji_id"] = custom_emoji_id
        if reply_to is not None:
            args["reply_to"] = reply_to
        result = await self._client.call_tool("send_message", args, read_timeout=SEND_READ_TIMEOUT)
        if not result.startswith(SENT_OK):
            raise ToolError(_short(result))
        match = _SENT_ID.search(result)
        return int(match.group(1)) if match else None

    async def profile_photo(self, sender_id: int) -> bytes | None:
        text = await self._client.call_tool(
            PHOTO_TOOL, {"peer_id": sender_id}, read_timeout=READ_TIMEOUT
        )
        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            raise ToolError(_short(text)) from None
        if not isinstance(result, dict) or result.get("peer_id") != sender_id:
            raise ToolError(f"unexpected {PHOTO_TOOL} result")
        if result.get("photo_id") == "":
            return None
        if not result.get("data"):
            raise ToolError(f"{PHOTO_TOOL} gave no picture")
        try:
            return base64.b64decode(str(result["data"]), validate=True)
        except (binascii.Error, ValueError):
            raise ToolError(f"unexpected {PHOTO_TOOL} result") from None
