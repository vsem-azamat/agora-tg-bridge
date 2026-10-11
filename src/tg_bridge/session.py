"""The Telethon session file: the API id and hash and the session string.

The file is a full login to the account. It is written with mode 0600 in a
directory with mode 0700 and never logged.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from tg_bridge.config import ConfigError

log = logging.getLogger("tg_bridge")

FILE_MODE = 0o600
DIR_MODE = 0o700
FORMAT_VERSION = 1


@dataclass(frozen=True)
class SessionData:
    api_id: int
    api_hash: str
    session: str  # a Telethon StringSession

    def __repr__(self) -> str:  # keep secrets out of logs and tracebacks
        return f"SessionData(api_id={self.api_id}, api_hash=***, session=***)"


def load_session(path: Path) -> SessionData:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError("no session file; run `tg-bridge login` (or pass --session)") from None
    except OSError as exc:
        raise ConfigError(f"cannot read the session file ({type(exc).__name__})") from exc
    try:
        mode = path.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            log.warning("the session file is readable by others; chmod 600 it")
    except OSError:
        pass
    try:
        data = json.loads(raw)
        session = SessionData(
            api_id=int(data["api_id"]), api_hash=str(data["api_hash"]), session=str(data["session"])
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        raise ConfigError("the session file is not one `tg-bridge login` wrote") from None
    if not session.api_hash or not session.session:
        raise ConfigError("the session file is not one `tg-bridge login` wrote")
    return session


def save_session(path: Path, data: SessionData) -> None:
    """Write the file atomically, readable by its owner only."""
    path.parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    body = json.dumps(
        {
            "version": FORMAT_VERSION,
            "api_id": data.api_id,
            "api_hash": data.api_hash,
            "session": data.session,
        }
    )
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body + "\n")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
