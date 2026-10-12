"""The MCP backend: telegram-mcp's event tools behind an MCP hub."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Protocol

from tg_bridge.backend import History
from tg_bridge.config import ConfigError
from tg_bridge.errors import ChatAccessError, ToolError

log = logging.getLogger("tg_bridge")

REQUIRED_TOOLS = ("wait_for_events", "get_messages_after", "send_message")
READ_TIMEOUT = 60.0
WAIT_SLACK = 30.0  # the HTTP read may last this much longer than the wait itself
SEND_READ_TIMEOUT = 115.0  # Telethon may sleep through a 60 s flood wait; the hub allows 120 s
SENT_OK = "Message sent successfully"
_SENT_ID = re.compile(r"message_id=(\d+)")


class McpClient(Protocol):
    async def list_tools(self) -> list[str]: ...

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

    async def start(self) -> None:
        tools = await self._client.list_tools()
        missing = [name for name in REQUIRED_TOOLS if name not in tools]
        if missing:
            raise ConfigError(
                "the Telegram MCP endpoint does not offer "
                + ", ".join(missing)
                + " (telegram-mcp with the event log, and the hub's tool filter)"
            )

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
        try:
            return History(
                chat_id=int(result["chat_id"]),
                chat_type=str(result.get("chat_type") or ""),
                messages=[r for r in result["messages"] if isinstance(r, dict)],
                has_more=bool(result.get("has_more")),
            )
        except (KeyError, TypeError, ValueError):
            raise ToolError("unexpected get_messages_after result") from None

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
        # telegram-mcp's records carry no sender_photo, so the bridge never asks.
        raise ToolError("telegram-mcp offers no profile photos")
