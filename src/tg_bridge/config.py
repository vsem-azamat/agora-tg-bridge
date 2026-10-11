"""Command line, the optional config file and the hub token."""

from __future__ import annotations

import argparse
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tg_bridge.text import normalize, utf16_len

ROBOT = "\U0001f916"
DEFAULT_PREFIX = f"{ROBOT} "
MAX_PREFIX_UTF16 = 64  # the split limit leaves this much room below Telegram's 4096
DEFAULT_CONFIG_FILE = "~/.config/agora-tg-bridge/config.toml"
DEFAULT_HUB = "http://127.0.0.1:8790"
DEFAULT_TOKEN_FILE = "~/.config/mcp-hub/client.token"
DEFAULT_TOKEN_VAR = "MCP_HUB_TOKEN"
ACCOUNT_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
CUSTOM_EMOJI_ID = re.compile(r"[0-9]{1,20}")

# Keys the config file may set, with their types; the chat is per bridge and
# stays on the command line.
FILE_KEYS: dict[str, tuple[type, ...]] = {
    "account": (str,),
    "hub_url": (str,),
    "token_file": (str,),
    "token_var": (str,),
    "reconcile": (int, float),
    "wait": (int, float),
    "prefix": (str,),
    "custom_emoji_id": (str, int),
}


class ConfigError(Exception):
    """A configuration error the bridge cannot recover from by retrying."""


@dataclass(frozen=True)
class Config:
    chat: int
    account: str
    hub_url: str
    token_file: Path
    token_var: str
    reconcile_seconds: float
    wait_seconds: float
    prefix: str = DEFAULT_PREFIX
    custom_emoji_id: str | None = None

    @property
    def mcp_url(self) -> str:
        return f"{self.hub_url.rstrip('/')}/telegram-{self.account}/mcp"


def parse_chat(value: str) -> int:
    """A numeric chat id only: a username can move to another chat."""
    try:
        return int(value.strip())
    except ValueError:
        raise ConfigError(
            "--chat must be a numeric chat id (find it with get_chat or list_chats)"
        ) from None


def parse_prefix(value: str) -> str:
    if not normalize(value):
        raise ConfigError("--prefix must have visible characters")
    if utf16_len(value) > MAX_PREFIX_UTF16:
        raise ConfigError(f"--prefix is longer than {MAX_PREFIX_UTF16} UTF-16 units")
    return value


def parse_custom_emoji_id(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if not CUSTOM_EMOJI_ID.fullmatch(value):
        raise ConfigError("--custom-emoji-id must be a numeric custom emoji document id")
    return value


def load_config_file(path: Path, *, required: bool) -> dict[str, Any]:
    """Defaults from a TOML file; a missing default file is no error."""
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        if required:
            raise ConfigError(f"the config file {path} does not exist") from None
        return {}
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"the config file {path} is not valid TOML ({exc})") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read the config file {path} ({type(exc).__name__})") from exc
    values: dict[str, Any] = {}
    for key, value in data.items():
        types = FILE_KEYS.get(key)
        if types is None:
            raise ConfigError(f"unknown key in the config file: {key}")
        if isinstance(value, bool) or not isinstance(value, types):
            raise ConfigError(f"wrong type for {key} in the config file")
        values[key] = str(value) if key == "custom_emoji_id" else value
    return values


def parse_args(argv: list[str] | None = None) -> Config:
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config")
    known, rest = pre.parse_known_args(argv)
    config_path = known.config if known.config is not None else DEFAULT_CONFIG_FILE
    try:
        file_values = load_config_file(
            Path(os.path.expanduser(config_path)), required=known.config is not None
        )
    except ConfigError:
        if "-h" not in rest and "--help" not in rest:
            raise
        file_values = {}  # help works with a broken config file

    parser = argparse.ArgumentParser(
        prog="tg-bridge",
        description="Agora bridge for one Telegram chat (JSON lines on stdin/stdout).",
    )
    parser.add_argument("--chat", required=True, help="numeric chat id (e.g. -1001234567890)")
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_FILE,
        help="TOML file with defaults for the other options (read when it exists)",
    )
    parser.add_argument(
        "--account",
        default="personal",
        help="account name; the endpoint is <hub-url>/telegram-<account>/mcp",
    )
    parser.add_argument("--hub-url", default=DEFAULT_HUB, help="MCP hub base URL (loopback)")
    parser.add_argument(
        "--token-file",
        default=DEFAULT_TOKEN_FILE,
        help="file holding the hub token: the bare token, or VAR=value lines",
    )
    parser.add_argument(
        "--token-var",
        default=DEFAULT_TOKEN_VAR,
        help="variable to read when --token-file has VAR=value lines",
    )
    parser.add_argument(
        "--reconcile",
        type=float,
        default=60.0,
        help="seconds between safety reads of the chat history",
    )
    parser.add_argument(
        "--wait",
        type=float,
        default=50.0,
        help="seconds one wait_for_events call may block (under the hub timeout)",
    )
    parser.add_argument(
        "--prefix",
        default=DEFAULT_PREFIX,
        help="text in front of every message the bridge sends (default: the robot emoji)",
    )
    parser.add_argument(
        "--custom-emoji-id",
        default=None,
        help="send the prefix's first character as this custom emoji (a document id)",
    )
    parser.set_defaults(**file_values)
    args = parser.parse_args(argv)
    if not ACCOUNT_NAME.fullmatch(args.account):
        raise ConfigError(
            "--account takes up to 32 lowercase letters, digits, - and _, "
            "starting with a letter or digit"
        )
    return Config(
        chat=parse_chat(args.chat),
        account=args.account,
        hub_url=args.hub_url,
        token_file=Path(os.path.expanduser(args.token_file)),
        token_var=args.token_var,
        reconcile_seconds=max(5.0, float(args.reconcile)),
        wait_seconds=min(max(1.0, float(args.wait)), 100.0),
        prefix=parse_prefix(args.prefix),
        custom_emoji_id=parse_custom_emoji_id(args.custom_emoji_id),
    )


def read_token(path: Path, var: str = DEFAULT_TOKEN_VAR) -> str:
    """Read the hub token: a file holding only the token (the client token file),
    or `VAR=value` lines. The value never leaves memory."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read the token file ({type(exc).__name__})") from exc
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) == 1 and "=" not in lines[0]:
        return lines[0]
    for line in lines:
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if sep and key.strip() == var:
            value = value.strip().strip("'\"")
            if value:
                return value
    raise ConfigError(f"no token in the token file (bare token or {var}=…)")
