"""Entry point: wire stdin/stdout to the bridge and keep a backend connected."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import time
from collections.abc import Awaitable, Callable
from typing import Any

from tg_bridge import account
from tg_bridge.backend import Backend
from tg_bridge.bridge import Bridge, Marker, short_reason
from tg_bridge.config import Config, ConfigError, parse_args, read_token
from tg_bridge.errors import AuthError, ToolError, TransportError
from tg_bridge.mcp_backend import McpBackend
from tg_bridge.mcp_http import McpHttpClient
from tg_bridge.session import load_session
from tg_bridge.telethon_backend import TelethonBackend

log = logging.getLogger("tg_bridge")

EXIT_CONFIG = 2
BACKOFF_MAX = 60.0
STEADY_SECONDS = 60.0


def write_stdout(line: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(line, ensure_ascii=False) + "\n")
    sys.stdout.flush()


async def read_stdin(bridge: Bridge) -> None:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=2 * 1024 * 1024)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    try:
        while True:
            try:
                raw = await reader.readline()
            except ValueError:
                log.warning("hub line too long; skipped")
                continue
            if not raw:
                break
            bridge.handle_hub_line(raw.decode("utf-8", errors="replace"))
    finally:
        log.info("stdin closed; stopping")
        bridge.closed.set()


def backend_factory(config: Config) -> Callable[[], Backend]:
    """Check the backend's configuration once and return a factory that makes
    a fresh connection for every attempt."""
    if config.backend == "mcp":
        token = read_token(config.token_file, config.token_var)
        return lambda: McpBackend(McpHttpClient(config.mcp_url, token))
    session = load_session(config.session_file)
    return lambda: TelethonBackend(session)


async def run(
    config: Config,
    make_backend: Callable[[], Backend],
    *,
    stdin_reader: Callable[[Bridge], Awaitable[None]] = read_stdin,
    write: Callable[[dict[str, Any]], None] = write_stdout,
    backoff_start: float = 1.0,
    fresh_grace: float | None = None,
) -> int:
    kwargs: dict[str, Any] = {}
    if fresh_grace is not None:
        kwargs["fresh_grace"] = fresh_grace
    bridge = Bridge(
        config.chat,
        write,
        reconcile_seconds=config.reconcile_seconds,
        wait_seconds=config.wait_seconds,
        marker=Marker(config.prefix, config.custom_emoji_id),
        threads=config.backend != "mcp",  # the MCP backend declares it when its server can
        **kwargs,
    )
    loop = asyncio.get_running_loop()
    signals = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, bridge.closed.set)
            signals.append(sig)
        except (NotImplementedError, RuntimeError):
            pass
    stdin_task = asyncio.ensure_future(stdin_reader(bridge))
    backoff = backoff_start
    try:
        while not bridge.closed.is_set():
            backend = make_backend()
            started = time.monotonic()
            try:
                await bridge.serve(backend)
                if bridge.closed.is_set():
                    return 0
            except (ConfigError, AuthError) as exc:
                log.error("configuration error: %s", exc)
                return EXIT_CONFIG
            except (TransportError, ToolError) as exc:
                log.warning(
                    "Telegram connection failed (%s); retrying in %.0f s",
                    short_reason(exc, 120),
                    backoff,
                )
            finally:
                await backend.aclose()
            if time.monotonic() - started >= STEADY_SECONDS:
                backoff = backoff_start
            try:
                await asyncio.wait_for(bridge.closed.wait(), timeout=backoff)
            except TimeoutError:
                pass
            backoff = min(backoff * 2, BACKOFF_MAX)
        return 0
    finally:
        for sig in signals:
            loop.remove_signal_handler(sig)
        stdin_task.cancel()
        await asyncio.gather(stdin_task, return_exceptions=True)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO, format="tg-bridge %(levelname)s %(message)s"
    )
    # httpx logs every request at INFO; a long-poll every 50 s would flood the hub's log.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telethon").setLevel(logging.WARNING)
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] in ("login", "chats"):
        return account.main(args[0], args[1:])
    try:
        config = parse_args(args)
        make_backend = backend_factory(config)
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        return EXIT_CONFIG
    return asyncio.run(run(config, make_backend))


if __name__ == "__main__":
    sys.exit(main())
