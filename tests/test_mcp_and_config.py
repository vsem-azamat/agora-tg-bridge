"""The HTTP MCP client (against httpx.MockTransport) and configuration."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tg_bridge.cli import main
from tg_bridge.config import ConfigError, parse_args, parse_chat, read_token
from tg_bridge.mcp_http import AuthError, McpHttpClient, ToolError, TransportError

URL = "http://127.0.0.1:8790/telegram-personal/mcp"


def server(handler_for_call, *, sse: bool = False, expire_once: bool = False):
    state = {"sessions": 0, "expired": False, "seen_auth": set()}

    def handler(request: httpx.Request) -> httpx.Response:
        state["seen_auth"].add(request.headers.get("authorization"))
        body = json.loads(request.content)
        method = body.get("method")
        if method == "initialize":
            state["sessions"] += 1
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"protocolVersion": "2025-06-18"},
                },
                headers={"mcp-session-id": f"s{state['sessions']}"},
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        assert request.headers["mcp-session-id"] == f"s{state['sessions']}"
        if expire_once and not state["expired"]:
            state["expired"] = True
            return httpx.Response(404)
        result = handler_for_call(body)
        payload = {"jsonrpc": "2.0", "id": body["id"], **result}
        if sse:
            text = (
                'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
                f"event: message\ndata: {json.dumps(payload)}\n\n"
            )
            return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=payload)

    return handler, state


def text_result(text: str, is_error: bool = False) -> dict:
    return {"result": {"content": [{"type": "text", "text": text}], "isError": is_error}}


@pytest.mark.parametrize("sse", [False, True])
async def test_call_tool_returns_text(sse):
    handler, state = server(lambda body: text_result("ok " + body["params"]["name"]), sse=sse)
    client = McpHttpClient(URL, "tok", transport=httpx.MockTransport(handler))

    assert await client.call_tool("get_me", {}) == "ok get_me"
    assert state["seen_auth"] == {"Bearer tok"}
    await client.aclose()


async def test_expired_session_is_renewed_once():
    handler, state = server(lambda body: text_result("ok"), expire_once=True)
    client = McpHttpClient(URL, "tok", transport=httpx.MockTransport(handler))

    assert await client.call_tool("get_me", {}) == "ok"
    assert state["sessions"] == 2
    await client.aclose()


async def test_tool_error_and_auth_error():
    handler, _ = server(lambda body: text_result("boom", is_error=True))
    client = McpHttpClient(URL, "tok", transport=httpx.MockTransport(handler))
    with pytest.raises(ToolError):
        await client.call_tool("x", {})
    await client.aclose()

    refused = McpHttpClient(
        URL, "bad", transport=httpx.MockTransport(lambda r: httpx.Response(401))
    )
    with pytest.raises(AuthError):
        await refused.call_tool("x", {})
    await refused.aclose()


async def test_unreachable_hub_is_a_transport_error():
    def fail(request):
        raise httpx.ConnectError("refused")

    client = McpHttpClient(URL, "tok", transport=httpx.MockTransport(fail))
    with pytest.raises(TransportError):
        await client.call_tool("x", {})
    await client.aclose()


async def test_list_tools_follows_pages():
    pages = iter(
        [
            {"result": {"tools": [{"name": "a"}], "nextCursor": "p2"}},
            {"result": {"tools": [{"name": "b"}]}},
        ]
    )
    handler, _ = server(lambda body: next(pages))
    client = McpHttpClient(URL, "tok", transport=httpx.MockTransport(handler))

    assert await client.list_tools() == ["a", "b"]
    await client.aclose()


def test_parse_chat_accepts_numeric_ids_only():
    assert parse_chat("-1001234567890") == -1001234567890
    assert parse_chat(" 42 ") == 42
    for bad in ("", "@someone", "someone", "@"):
        with pytest.raises(ConfigError):
            parse_chat(bad)


def test_config_urls_and_defaults():
    config = parse_args(["--chat", "42"])
    assert config.mcp_url == URL
    assert parse_args(["--chat", "42", "--account", "work"]).mcp_url.endswith("/telegram-work/mcp")
    assert parse_args(["--chat", "42", "--wait", "500"]).wait_seconds == 100.0
    for bad in ("Work", "a/b", "../x", ""):
        with pytest.raises(ConfigError):
            parse_args(["--chat", "42", "--account", bad])


def test_prefix_and_custom_emoji_options(tmp_path):
    absent = ["--config", str(tmp_path / "none.toml")]
    with pytest.raises(ConfigError):
        parse_args(["--chat", "1", *absent])  # an explicit config file must exist
    config = parse_args(["--chat", "1"])
    assert config.prefix == "\U0001f916 " and config.custom_emoji_id is None
    config = parse_args(["--chat", "1", "--prefix", "[bot] ", "--custom-emoji-id", "123"])
    assert (config.prefix, config.custom_emoji_id) == ("[bot] ", "123")
    assert parse_args(["--chat", "1", "--prefix", "x" * 64]).prefix == "x" * 64
    bad_options = (
        ["--prefix", "  "],
        ["--prefix", "\u2060\u200b"],
        ["--prefix", "x" * 65],
        ["--custom-emoji-id", "abc"],
    )
    for bad in bad_options:
        with pytest.raises(ConfigError):
            parse_args(["--chat", "1", *bad])


def test_config_file_sets_defaults_and_the_command_line_wins(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        'account = "work"\ncustom_emoji_id = 123\nprefix = "[bot] "\nreconcile = 30\n',
        encoding="utf-8",
    )
    config = parse_args(["--chat", "1", "--config", str(path)])
    assert config.account == "work" and config.custom_emoji_id == "123"
    assert config.prefix == "[bot] " and config.reconcile_seconds == 30.0
    config = parse_args(["--chat", "1", "--config", str(path), "--account", "personal"])
    assert config.account == "personal"


@pytest.mark.parametrize(
    "body", ['chat = "1"\n', "reconcile = true\n", 'wait = "5"\n', "not toml ["]
)
def test_bad_config_file_is_a_configuration_error(tmp_path, body):
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigError):
        parse_args(["--chat", "1", "--config", str(path)])


def test_help_works_with_a_broken_config_file(tmp_path, capsys):
    path = tmp_path / "config.toml"
    path.write_text("not toml [", encoding="utf-8")
    with pytest.raises(SystemExit) as caught:
        parse_args(["--config", str(path), "--help"])
    assert caught.value.code == 0
    assert "--prefix" in capsys.readouterr().out


def test_read_bare_client_token(tmp_path):
    path = tmp_path / "client.token"
    path.write_text("abc123\n")
    assert read_token(path) == "abc123"
    assert str(parse_args(["--chat", "1"]).token_file).endswith(".config/mcp-hub/client.token")


def test_read_token(tmp_path):
    path = tmp_path / "tokens.env"
    path.write_text("MCP_HUB_TOKEN_OTHER=a\nexport MCP_HUB_TOKEN='b'\n")
    assert read_token(path, "MCP_HUB_TOKEN") == "b"
    with pytest.raises(ConfigError):
        read_token(path, "MISSING")
    with pytest.raises(ConfigError):
        read_token(tmp_path / "absent", "MCP_HUB_TOKEN")


def test_bad_config_exits_2_without_printing_the_token(tmp_path, capsys):
    path = tmp_path / "tokens.env"
    path.write_text("MCP_HUB_TOKEN=supersecret\n")
    assert main(["--chat", "nonsense", "--token-file", str(path)]) == 2
    assert main(["--chat", "1", "--token-file", str(tmp_path / "absent")]) == 2
    captured = capsys.readouterr()
    assert "supersecret" not in captured.out + captured.err
    assert captured.out == ""


# --- the reconnect loop ----------------------------------------------------

from tests.test_bridge import CHAT, FakeTelegram, Out, hello, msg  # noqa: E402
from tg_bridge.cli import run  # noqa: E402


async def _drive(bridge, until):
    bridge.handle_hub_line(hello("10"))
    for _ in range(300):
        await asyncio.sleep(0.01)
        if until(bridge):
            break
    bridge.closed.set()


async def test_run_reconnects_after_a_transport_error():
    broken = FakeTelegram(history=[msg(10)])

    async def refuse():
        raise TransportError("ConnectError")

    broken.list_tools = refuse
    working = FakeTelegram(history=[msg(10)])
    clients = iter([broken, working])
    made = []

    def factory(url, token):
        made.append(url)
        return next(clients)

    async def reader(bridge):
        await _drive(bridge, lambda b: b.connected_at is not None)

    config = parse_args(["--chat", str(CHAT)])
    code = await asyncio.wait_for(
        run(
            config,
            "tok",
            client_factory=factory,
            stdin_reader=reader,
            write=Out(),
            backoff_start=0.01,
            fresh_grace=0,
        ),
        5,
    )

    assert code == 0
    assert len(made) == 2 and made[0].endswith("/telegram-personal/mcp")
    assert broken.closed and working.closed


async def test_run_sends_with_the_configured_prefix_and_custom_emoji():
    tg = FakeTelegram(history=[msg(10)])

    async def reader(bridge):
        bridge.handle_hub_line(hello("10"))
        bridge.handle_hub_line(json.dumps({"type": "out", "id": 7, "text": "hi"}))
        for _ in range(300):
            await asyncio.sleep(0.01)
            if tg.sends:
                break
        bridge.closed.set()

    config = parse_args(["--chat", str(CHAT), "--prefix", "[bot] ", "--custom-emoji-id", "123"])
    code = await asyncio.wait_for(
        run(
            config,
            "tok",
            client_factory=lambda u, t: tg,
            stdin_reader=reader,
            write=Out(),
            fresh_grace=0,
        ),
        5,
    )

    assert code == 0
    (args,) = [a for name, a in tg.calls if name == "send_message"]
    assert args["message"] == "[bot] hi" and args["custom_emoji_id"] == "123"


async def test_run_exits_2_on_a_configuration_error():
    tg = FakeTelegram(history=[msg(10)])
    tg.tools = ["send_message"]

    async def reader(bridge):
        bridge.handle_hub_line(hello("10"))
        await asyncio.sleep(5)

    config = parse_args(["--chat", str(CHAT)])
    code = await asyncio.wait_for(
        run(
            config,
            "tok",
            client_factory=lambda u, t: tg,
            stdin_reader=reader,
            write=Out(),
            fresh_grace=0,
        ),
        5,
    )
    assert code == 2
