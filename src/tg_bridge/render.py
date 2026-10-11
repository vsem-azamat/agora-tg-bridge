"""Telegram message records (get_messages_after) to Agora `in` lines."""

from __future__ import annotations

from typing import Any


def _duration(seconds: Any) -> str:
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return ""
    return f"{total // 60}:{total % 60:02d}"


def placeholder(rec: dict[str, Any]) -> str:
    """A short placeholder for content that is not text, or ""."""
    media = rec.get("media") or ""
    dur = _duration(rec.get("duration"))
    if media == "photo":
        return "[photo]"
    if media == "voice":
        return f"[voice {dur}]" if dur else "[voice]"
    if media == "video_note":
        return f"[video message {dur}]" if dur else "[video message]"
    if media == "video":
        return "[video]"
    if media == "audio":
        return "[audio]"
    if media == "gif":
        return "[GIF]"
    if media.startswith("document: "):
        return f"[file {media[len('document: ') :]}]"
    if media == "document":
        return "[file]"
    if media.startswith("sticker"):
        alt = media[len("sticker") :].strip()
        return f"[sticker {alt}]" if alt else "[sticker]"
    if media == "contact":
        return "[contact]"
    if media == "geo":
        return "[location]"
    if media == "poll":
        question = (rec.get("poll") or {}).get("question")
        return f"[poll: {question}]" if question else "[poll]"
    if media:
        return "[media]"
    return ""


def message_text(rec: dict[str, Any]) -> str:
    parts = []
    fwd = rec.get("forwarded")
    if fwd:
        origin = ""
        if isinstance(fwd, dict):
            origin = fwd.get("from_user") or fwd.get("from_chat") or fwd.get("from_name") or ""
        parts.append(f"[forwarded from {origin}]" if origin else "[forwarded]")
    ph = placeholder(rec)
    if ph:
        parts.append(ph)
    text = rec.get("text") or ""
    if text:
        parts.append(text)
    elif rec.get("transcript"):
        parts.append(f"(transcript, not verbatim) {rec['transcript']}")
    return " ".join(parts)


def is_service(rec: dict[str, Any]) -> bool:
    return bool(rec.get("action"))


def in_line(rec: dict[str, Any], chat_type: str) -> dict[str, Any]:
    """The `in` line for one message record; service messages only move the cursor."""
    msg_id = str(rec["id"])
    if is_service(rec):
        return {"type": "in", "cursor": msg_id}
    out = bool(rec.get("out"))
    line: dict[str, Any] = {
        "type": "in",
        "id": msg_id,
        "author": {
            "id": str(rec["sender_id"]) if rec.get("sender_id") is not None else "",
            "name": rec.get("sender") or "Unknown",
            "self": out,
        },
        "text": message_text(rec),
        "cursor": msg_id,
        "addressed": bool(rec.get("addressed"))
        if "addressed" in rec
        else (chat_type == "private" and not out),
    }
    if rec.get("reply_to") is not None:
        line["reply_to"] = str(rec["reply_to"])
    if rec.get("date"):
        line["at"] = rec["date"]
    return line
