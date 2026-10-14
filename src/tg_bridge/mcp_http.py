"""A small MCP client over streamable HTTP: initialize, then tools/call.

Every call is an independent POST, so a long-poll and a send can run at the
same time. Responses come as JSON or as a server-sent-event stream; both are
read until the JSON-RPC response with the request's id.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from typing import Any

import httpx

from tg_bridge.errors import AuthError, ToolError, TransportError

PROTOCOL_VERSION = "2025-06-18"


class McpHttpClient:
    def __init__(
        self,
        url: str,
        token: str,
        *,
        connect_timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = url
        self._token = token
        self._session_id: str | None = None
        self._ids = itertools.count(1)
        self._init_lock = asyncio.Lock()
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=connect_timeout), transport=transport
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self._token}",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
            headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
        return headers

    async def _post(
        self, body: dict[str, Any], read_timeout: float
    ) -> tuple[int, dict | None, str | None]:
        timeout = httpx.Timeout(read_timeout, connect=10.0)
        try:
            async with self._http.stream(
                "POST", self._url, json=body, headers=self._headers(), timeout=timeout
            ) as resp:
                session = resp.headers.get("mcp-session-id")
                if resp.status_code in (401, 403):
                    raise AuthError(f"hub refused the token (HTTP {resp.status_code})")
                if resp.status_code >= 400:
                    await resp.aread()
                    return resp.status_code, None, session
                if "id" not in body:
                    await resp.aread()
                    return resp.status_code, None, session
                ctype = resp.headers.get("content-type", "")
                if ctype.startswith("text/event-stream"):
                    message = await _read_sse_response(resp, body["id"])
                else:
                    raw = await resp.aread()
                    message = json.loads(raw) if raw.strip() else None
                return resp.status_code, message, session
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise TransportError(type(exc).__name__) from exc

    async def _initialize(self) -> None:
        self._session_id = None
        status, message, session = await self._post(
            {
                "jsonrpc": "2.0",
                "id": next(self._ids),
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "agora-tg-bridge", "version": "0.1.0"},
                },
            },
            read_timeout=30.0,
        )
        if status >= 400 or not message or "result" not in message:
            raise TransportError(f"initialize failed (HTTP {status})")
        self._session_id = session
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, 30.0)

    async def ensure_session(self) -> None:
        async with self._init_lock:
            if self._session_id is None:
                await self._initialize()

    async def _request(self, method: str, params: dict[str, Any], read_timeout: float) -> dict:
        """One JSON-RPC request; re-initializes once when the session expired."""
        for attempt in (1, 2):
            await self.ensure_session()
            stale = self._session_id
            status, message, _ = await self._post(
                {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params},
                read_timeout,
            )
            if status in (400, 404) and attempt == 1:
                # The session expired (the hub or the server restarted). The
                # request was refused before anything ran, so retrying is safe.
                async with self._init_lock:
                    if self._session_id == stale:
                        self._session_id = None
                continue
            if status >= 400 or message is None:
                raise TransportError(f"{method} failed (HTTP {status})")
            if "error" in message:
                raise ToolError(str(message["error"].get("message", "error"))[:200])
            return message.get("result") or {}
        raise TransportError(f"{method} failed")

    async def list_tools(self) -> dict[str, str]:
        """The tools the endpoint offers: name to description."""
        tools: dict[str, str] = {}
        cursor = None
        while True:
            result = await self._request("tools/list", {"cursor": cursor} if cursor else {}, 30.0)
            for tool in result.get("tools", []):
                tools[tool.get("name", "")] = str(tool.get("description") or "")
            cursor = result.get("nextCursor")
            if not cursor:
                return tools

    async def call_tool(
        self, name: str, arguments: dict[str, Any], *, read_timeout: float = 60.0
    ) -> str:
        """Call a tool and return its text content; raises ToolError when the
        tool reports an error and TransportError when it could not be called."""
        result = await self._request(
            "tools/call", {"name": name, "arguments": arguments}, read_timeout
        )
        text = "\n".join(
            part.get("text", "") for part in result.get("content", []) if part.get("type") == "text"
        )
        if result.get("isError"):
            raise ToolError(text[:300])
        return text


async def _read_sse_response(resp: httpx.Response, request_id: int) -> dict | None:
    data: list[str] = []
    async for line in resp.aiter_lines():
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
            continue
        if line == "" and data:
            payload, data = "\n".join(data), []
            message = json.loads(payload)
            if isinstance(message, dict) and message.get("id") == request_id:
                return message
    if data:
        message = json.loads("\n".join(data))
        if isinstance(message, dict) and message.get("id") == request_id:
            return message
    return None
