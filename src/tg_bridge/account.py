"""`tg-bridge login` and `tg-bridge chats`: the Telethon session, interactively."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from collections.abc import Callable
from typing import Any

from telethon import TelegramClient, errors, utils
from telethon.sessions import StringSession

from tg_bridge.config import (
    ConfigError,
    add_common_options,
    file_defaults,
    session_path,
)
from tg_bridge.session import SessionData, load_session, save_session
from tg_bridge.telethon_backend import DEVICE_MODEL, chat_type, make_client

API_ID_VAR = "TG_API_ID"
API_HASH_VAR = "TG_API_HASH"
API_HELP = "https://my.telegram.org/apps"
DEFAULT_CHATS_LIMIT = 50
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2


def _parser(command: str, file_values: dict[str, Any]) -> argparse.ArgumentParser:
    if command == "login":
        parser = argparse.ArgumentParser(
            prog="tg-bridge login",
            description="Log in to Telegram and write the session file the Telethon "
            f"backend uses. The API id and hash come from ${API_ID_VAR} and "
            f"${API_HASH_VAR}, the config file (api_id, api_hash) or a prompt; "
            f"get them at {API_HELP}.",
        )
        parser.add_argument("--force", action="store_true", help="replace an existing session file")
    else:
        parser = argparse.ArgumentParser(
            prog="tg-bridge chats",
            description="List your recent chats with the numeric ids --chat takes.",
        )
        parser.add_argument("--limit", type=int, default=DEFAULT_CHATS_LIMIT)
    add_common_options(parser)
    parser.set_defaults(**{k: v for k, v in file_values.items() if k == "session"})
    return parser


def _api_credentials(file_values: dict[str, Any], ask: Callable[[str], str]) -> tuple[int, str]:
    raw_id = (
        os.environ.get(API_ID_VAR)
        or file_values.get("api_id")
        or ask(f"API id (from {API_HELP}): ")
    )
    api_hash = (
        os.environ.get(API_HASH_VAR) or file_values.get("api_hash") or getpass.getpass("API hash: ")
    )
    try:
        api_id = int(str(raw_id).strip())
    except ValueError:
        raise ConfigError("the API id is a number") from None
    if not str(api_hash).strip():
        raise ConfigError("the API hash is empty")
    return api_id, str(api_hash).strip()


async def login(
    path: Any,
    api_id: int,
    api_hash: str,
    *,
    ask: Callable[[str], str] = input,
    client_factory: Callable[..., Any] = TelegramClient,
) -> str:
    """Log in interactively and save the session; returns the account's name."""
    client = client_factory(StringSession(), api_id, api_hash, device_model=DEVICE_MODEL)
    try:
        await client.start(phone=lambda: ask("Phone number (international format): "))
        me = await client.get_me()
        save_session(path, SessionData(api_id, api_hash, client.session.save()))
        return utils.get_display_name(me)
    finally:
        await client.disconnect()


async def list_chats(session: SessionData, limit: int) -> list[tuple[int, str, str]]:
    client = make_client(session)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise ConfigError("the session is not logged in; run `tg-bridge login`")
        rows = []
        async for dialog in client.iter_dialogs(limit=limit):
            rows.append((dialog.id, chat_type(dialog.entity), dialog.name or ""))
        return rows
    finally:
        await client.disconnect()


def main(command: str, argv: list[str]) -> int:
    try:
        file_values = file_defaults(argv)
        args = _parser(command, file_values).parse_args(argv)
        path = session_path(args.session)
        if command == "login":
            if path.exists() and not args.force:
                print(f"{path} exists; pass --force to replace it", file=sys.stderr)
                return EXIT_CONFIG
            api_id, api_hash = _api_credentials(file_values, input)
            name = asyncio.run(login(path, api_id, api_hash))
            print(f"Logged in as {name}. Session saved to {path} (keep it secret).")
            return EXIT_OK
        rows = asyncio.run(list_chats(load_session(path), max(1, args.limit)))
    except ConfigError as exc:
        print(f"tg-bridge: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except (errors.RPCError, ConnectionError, OSError) as exc:
        print(f"tg-bridge: Telegram error: {type(exc).__name__}", file=sys.stderr)
        return EXIT_FAILED
    except (KeyboardInterrupt, EOFError):
        print(file=sys.stderr)
        return EXIT_FAILED
    for chat_id, kind, name in rows:
        print(f"{chat_id:>16}  {kind or '?':<10}  {name}")
    return EXIT_OK
