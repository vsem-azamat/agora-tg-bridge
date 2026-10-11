"""Text helpers shared by the configuration and the bridge."""

from __future__ import annotations

import re
import unicodedata

# Characters telegram-mcp's sanitize_user_content removes from message text.
_INVISIBLE = re.compile("[\u200b-\u200f\u2028\u2029\u202a-\u202e\u2060-\u2064\ufeff\ufff9-\ufffb]")
_NEWLINES = re.compile(r"\n{3,}")


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def visible(text: str) -> str:
    """Text without control, format and invisible characters (line breaks and
    tabs stay)."""
    kept = "".join(
        ch for ch in text if ch in "\n\t" or unicodedata.category(ch) not in ("Cc", "Cf")
    )
    return _INVISIBLE.sub("", kept)


def normalize_start(text: str) -> str:
    """The start of a text as telegram-mcp keeps it: `normalize` without the
    trailing strip, so a prefix keeps its space."""
    return _NEWLINES.sub("\n\n", visible(text)).lstrip()


def normalize(text: str) -> str:
    """Text as telegram-mcp returns it (sanitize_user_content), for comparing."""
    return _NEWLINES.sub("\n\n", visible(text)).strip()
