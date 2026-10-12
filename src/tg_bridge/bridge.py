"""The bridge: Agora's JSON lines on one side, a Telegram backend on the other.

Inbound: after `hello`, read the chat from the hub's cursor, then wait for
events of this chat and read again whenever something arrives, plus a safety
read every `reconcile_seconds`.

Outbound: each `out` line becomes a send job of one or more sends with the
marker prefix in front. Before a job sends anything it looks in
the chat for its text already delivered, but only in two cases: the job's own
send got no answer (the connection broke or timed out), or the line came from
the hub before `hello` or within 30 s of it (a message an earlier process sent
and died before answering). Normal sends in a running process never claim, so an
identical assistant message from another client cannot swallow them. An
uncertain send is never answered blindly: the chat is checked after the
reconnect and again 65 s later (a flood wait may delay the message) before the
job answers `failed`. `sent` is written before any read of the chat can report
the new message, because reads and sends hold the same lock.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from tg_bridge.backend import Backend, History
from tg_bridge.config import DEFAULT_PREFIX, ConfigError
from tg_bridge.errors import ChatAccessError, ToolError, TransportError
from tg_bridge.render import in_line
from tg_bridge.text import normalize, normalize_start, utf16_len

log = logging.getLogger("tg_bridge")

ASSISTANT_ID = "assistant"
MAX_MESSAGE_UTF16 = 4000  # Telegram allows 4096; leave room for the prefix
HISTORY_PAGE = 100
DELIVERY_CHECK_PAGES = 10
FRESH_GRACE_SECONDS = 1.5  # the hub hands unanswered messages within a second of `hello`
CLAIM_WINDOW_SECONDS = 30.0  # lines handed this soon after `hello` may be an earlier process's
RECHECK_SECONDS = 65.0  # telegram-mcp may still be sleeping through a 60 s flood wait
STARTUP_ATTEMPTS = 6
AVATAR_LINES = 30  # the hub takes at most 30 avatar lines a minute from a bridge
AVATAR_WINDOW_SECONDS = 61.0  # a minute, with a margin
MAX_AVATAR_BYTES = 256 * 1024  # the hub refuses larger PNG and JPEG images
NO_AVATAR_REV = "none"  # the rev of "this person has no avatar"
IMAGE_SIGNATURES = ((b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "image/png"))


def split_message(text: str, limit: int = MAX_MESSAGE_UTF16) -> list[str]:
    """Split text into pieces of at most `limit` UTF-16 units, preferring line
    breaks, then spaces."""
    chunks: list[str] = []
    rest = text
    while utf16_len(rest) > limit:
        cut, units = 0, 0
        for i, ch in enumerate(rest):
            units += utf16_len(ch)
            if units > limit:
                break
            cut = i + 1
        window = rest[:cut]
        brk = max(window.rfind("\n"), window.rfind(" "))
        if brk > cut // 2:
            cut = brk + 1
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n")
    if rest.strip() or not chunks:
        chunks.append(rest)
    return chunks


@dataclass(frozen=True)
class Marker:
    """How the bridge marks what it sends: a text prefix, its first character
    optionally sent as a custom emoji. Own messages carrying the mark are an
    assistant's, not the operator's."""

    prefix: str = DEFAULT_PREFIX
    custom_emoji_id: str | None = None

    def text(self, chunk: str) -> str:
        return self.prefix + chunk

    @property
    def assistant_author(self) -> dict[str, Any]:
        """The author of marked messages this bridge did not send."""
        return {"id": ASSISTANT_ID, "name": f"assistant ({normalize(self.prefix)})", "self": False}

    def matches(self, rec: dict[str, Any]) -> bool:
        if not rec.get("out"):
            return False
        # telegram-mcp strips invisible characters and leading whitespace,
        # but keeps the space after the prefix.
        text = normalize(str(rec.get("text") or ""))
        if self.custom_emoji_id is None:
            return text.startswith(normalize_start(self.prefix))
        # The custom emoji is the mark: other assistants may follow it with
        # other text than this bridge's prefix.
        if not text.startswith(normalize_start(self.prefix)[:1]):
            return False
        return any(
            isinstance(e, dict) and str(e.get("id")) == self.custom_emoji_id
            for e in rec.get("custom_emojis") or []
        )


def short_reason(text: Any, limit: int = 200) -> str:
    """A one-line error reason for a `failed` line (never the message text)."""
    reason = " ".join(str(text).split())
    return reason[:limit] or "unknown error"


def image_mime(data: bytes) -> str | None:
    """The media type of a JPEG or PNG image, None for anything else."""
    return next((mime for sig, mime in IMAGE_SIGNATURES if data.startswith(sig)), None)


def _parse_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


@dataclass
class SendJob:
    out_id: int
    chunks: list[str]
    reply_to: int | None
    cursor_before: int
    done: int = 0  # parts known to be in the chat
    ids: list[int] = field(default_factory=list)
    claim_from: int | None = None  # look in the chat after this id before sending
    uncertain: bool = False  # part `done` may have gone out; its call got no answer


class Bridge:
    def __init__(
        self,
        chat: int | str,
        write: Callable[[dict[str, Any]], None],
        *,
        reconcile_seconds: float = 60.0,
        wait_seconds: float = 50.0,
        fresh_grace: float = FRESH_GRACE_SECONDS,
        recheck_seconds: float = RECHECK_SECONDS,
        marker: Marker | None = None,
        avatar_window: float = AVATAR_WINDOW_SECONDS,
    ) -> None:
        self.chat = chat
        self.marker = marker or Marker()
        self._write = write
        self.reconcile_seconds = reconcile_seconds
        self.wait_seconds = wait_seconds
        self.fresh_grace = fresh_grace
        self.recheck_seconds = recheck_seconds
        self.avatar_window = avatar_window
        self.hello_cursor: int | None = None  # where this process started reading
        self._hello_at: float | None = None
        self.cursor: int | None = None
        self.hello: dict[str, Any] | None = None
        self.hello_seen = asyncio.Event()
        self.closed = asyncio.Event()
        self.outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.chat_id: int | None = None
        self.chat_type: str | None = None
        self._own_order: deque[int] = deque(maxlen=5000)
        self._own: set[int] = set()
        self._accepted: set[int] = set()  # out ids taken in this process
        self._job: SendJob | None = None
        self._lock = asyncio.Lock()
        self._backend: Backend | None = None
        self._in_flight: asyncio.Future | None = None
        self._started_once = False
        self._startup_failures = 0
        self.connected_at: float | None = None
        # Avatars: the rev answered per author in this process, the ones still
        # to answer (oldest first) and when the recent avatar lines went out.
        self._avatar_revs: dict[str, str] = {}
        self._avatar_wanted: dict[str, str] = {}
        self._avatar_wake = asyncio.Event()
        self._avatar_times: deque[float] = deque()

    # --- hub side ---------------------------------------------------------

    def emit(self, line: dict[str, Any]) -> None:
        self._write(line)

    def handle_hub_line(self, raw: str) -> None:
        raw = raw.strip()
        if not raw:
            return
        try:
            line = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("hub line is not JSON; skipped")
            return
        if not isinstance(line, dict):
            return
        kind = line.get("type")
        if kind == "hello":
            if self.hello is None:
                self.hello = line
                self.cursor = _parse_id(line.get("cursor"))
                self.hello_cursor = self.cursor
                self._hello_at = time.monotonic()
                log.info("hello: bridge %s, cursor %s", line.get("bridge"), self.cursor)
            self.hello_seen.set()
        elif kind == "out":
            out_id = _parse_id(line.get("id"))
            if out_id is None:
                log.warning("out line without a numeric id; skipped")
                return
            if out_id in self._accepted:
                log.info("message %s handed again; already taken", out_id)
                return
            self._accepted.add(out_id)
            # The hub re-hands unanswered messages within about a second of
            # `hello`; only those may have been sent by an earlier process.
            if self._hello_at is None or (time.monotonic() - self._hello_at < CLAIM_WINDOW_SECONDS):
                line["_maybe_earlier"] = True
            self.outbox.put_nowait(line)

    def _remember_own(self, msg_id: int) -> None:
        if msg_id in self._own:
            return
        if len(self._own_order) == self._own_order.maxlen:
            self._own.discard(self._own_order[0])
        self._own_order.append(msg_id)
        self._own.add(msg_id)

    # --- Telegram side: reading -------------------------------------------

    async def _messages_after(self, after_id: int | None, limit: int) -> History:
        chat = self.chat_id if self.chat_id is not None else self.chat
        return await self._backend.messages_after(chat, after_id, limit)

    def _line_for(self, rec: dict[str, Any]) -> dict[str, Any]:
        if rec["id"] in self._own:
            return {"type": "in", "cursor": str(rec["id"])}
        line = in_line(rec, self.chat_type or "")
        if "id" in line and self.marker.matches(rec):
            # An assistant (another bridge or MCP client) wrote it through this
            # account: it is not the operator's own message.
            line["author"] = self.marker.assistant_author
            line["addressed"] = False
        author = line.get("author")
        if author and "sender_photo" in rec and author["id"] not in ("", ASSISTANT_ID):
            if not author["self"]:  # the operator's messages show the operator's avatar
                self._want_avatar(author, rec["sender_photo"])
        return line

    # --- avatars ----------------------------------------------------------

    def _want_avatar(self, author: dict[str, Any], photo: str) -> None:
        rev = photo or NO_AVATAR_REV
        author["avatar_rev"] = rev
        if self._avatar_revs.get(author["id"]) != rev:
            self._avatar_wanted[author["id"]] = rev
            self._avatar_wake.set()

    async def _avatar_turn(self) -> None:
        """Wait until another avatar line keeps within the hub's rate."""
        while len(self._avatar_times) >= AVATAR_LINES:
            wait = self._avatar_times[0] + self.avatar_window - time.monotonic()
            if wait <= 0:
                self._avatar_times.popleft()
            else:
                await asyncio.sleep(wait)

    async def _avatar_line(self, author: str, rev: str) -> dict[str, Any] | None:
        line: dict[str, Any] = {"type": "avatar", "author": author, "data": "", "rev": rev}
        if rev == NO_AVATAR_REV:
            return line
        try:
            data = await self._backend.profile_photo(int(author))
        except ToolError as exc:
            log.warning("avatar of %s not read: %s", author, short_reason(exc, 120))
            return None
        if data is None:
            return line
        mime = image_mime(data)
        if mime is None or len(data) > MAX_AVATAR_BYTES:
            log.warning(
                "avatar of %s skipped: %s, %d bytes", author, mime or "not JPEG or PNG", len(data)
            )
            return None
        line["mime"] = mime
        line["data"] = base64.b64encode(data).decode("ascii")
        return line

    async def _avatars_loop(self) -> None:
        while True:
            await self._avatar_wake.wait()
            self._avatar_wake.clear()
            while self._avatar_wanted:
                await self._avatar_turn()
                author, rev = next(iter(self._avatar_wanted.items()))
                line = await self._avatar_line(author, rev)
                if self._avatar_wanted.get(author) == rev:
                    del self._avatar_wanted[author]
                # A photo that could not be read is not asked for again until it changes.
                self._avatar_revs[author] = rev
                if line is not None:
                    self.emit(line)
                    self._avatar_times.append(time.monotonic())

    async def sync(self) -> int:
        """Read the chat after the cursor and emit what is new; returns the count."""
        async with self._lock:
            return await self._sync_locked()

    async def _sync_locked(self) -> int:
        emitted = 0
        while True:
            result = await self._messages_after(self.cursor or 0, HISTORY_PAGE)
            records = [r for r in result.messages if isinstance(r.get("id"), int)]
            for rec in records:
                if self.cursor is not None and rec["id"] <= self.cursor:
                    continue
                line = self._line_for(rec)
                self.emit(line)
                emitted += "id" in line
                self.cursor = rec["id"]
            if not result.has_more or not records:
                break
        if emitted:
            log.info("read %d new message(s), cursor %s", emitted, self.cursor)
        return emitted

    async def _poll_events(self, timeout: float) -> bool:
        return await self._backend.wait_for_events(self.chat_id, timeout)

    # --- Telegram side: sending -------------------------------------------

    async def _claim_delivered(self, job: SendJob) -> int:
        """Find parts of the job already in the chat (own robot messages after
        the job's cursor, not claimed before, with the part's exact text) and
        claim them in order; returns how many were found."""
        found = 0
        after = job.claim_from or 0
        for _ in range(DELIVERY_CHECK_PAGES):
            if job.done >= len(job.chunks):
                break
            result = await self._messages_after(after, HISTORY_PAGE)
            records = [r for r in result.messages if isinstance(r.get("id"), int)]
            for rec in records:
                after = max(after, rec["id"])
                if job.done >= len(job.chunks):
                    break
                if rec["id"] in self._own or not self.marker.matches(rec):
                    continue
                if normalize(str(rec.get("text") or "")) != normalize(
                    self.marker.text(job.chunks[job.done])
                ):
                    continue
                expected_reply = job.reply_to if job.done == 0 else None
                if _parse_id(rec.get("reply_to")) != expected_reply:
                    continue
                job.ids.append(rec["id"])
                self._remember_own(rec["id"])
                job.done += 1
                found += 1
            if not result.has_more or not records:
                break
        if found:
            log.info("message %s: %d part(s) already in the chat", job.out_id, found)
        return found

    async def _process(self, line: dict[str, Any]) -> None:
        """Run one `out` line to an answer (the caller holds the lock)."""
        out_id = _parse_id(line.get("id"))
        text = line.get("text") if isinstance(line.get("text"), str) else ""
        if not normalize(text):
            self.emit({"type": "failed", "id": out_id, "error": "empty message"})
            return
        job = SendJob(
            out_id=out_id,
            chunks=split_message(text),
            reply_to=_parse_id(line.get("reply_to")),
            cursor_before=self.cursor or 0,
        )
        if line.get("_maybe_earlier"):
            # Possibly sent by an earlier process: its copy may already be
            # behind the cursor, so look from where this process started.
            job.claim_from = self.hello_cursor or 0
        self._job = job
        await self._run_job(job)

    async def _run_job(self, job: SendJob) -> None:
        """Send what is left of a job and answer it. A connection failure leaves
        the job in place, to be checked against the chat after the reconnect."""
        if job.claim_from is not None:
            found = await self._claim_delivered(job)
            if job.uncertain and not found:
                log.info(
                    "message %s: not in the chat yet; checking again in %.0f s",
                    job.out_id,
                    self.recheck_seconds,
                )
                await asyncio.sleep(self.recheck_seconds)
                found = await self._claim_delivered(job)
                if not found:
                    job.claim_from = None
                    self._finish_failed(
                        job,
                        "the Telegram connector did not answer and the message "
                        f"was not found in the chat after {self.recheck_seconds:.0f} s",
                    )
                    return
            job.claim_from = None
            job.uncertain = False
        while job.done < len(job.chunks):
            try:
                msg_id = await self._backend.send(
                    self.chat_id,
                    self.marker.text(job.chunks[job.done]),
                    reply_to=job.reply_to if job.done == 0 else None,
                    custom_emoji_id=self.marker.custom_emoji_id,
                )
            except (TransportError, asyncio.CancelledError):
                job.uncertain = True
                job.claim_from = job.cursor_before
                log.warning("message %s: no answer to a send; will check the chat", job.out_id)
                raise
            except ToolError as exc:
                self._finish_failed(job, short_reason(exc))
                return
            if msg_id is not None:
                job.ids.append(msg_id)
                self._remember_own(msg_id)
            job.done += 1
        self._finish_sent(job)

    def _finish_sent(self, job: SendJob) -> None:
        line: dict[str, Any] = {"type": "sent", "id": job.out_id}
        if job.ids:
            line["ext_id"] = str(job.ids[0])
        self.emit(line)
        self._job = None
        log.info("message %s sent as %s", job.out_id, job.ids[0] if job.ids else "?")

    def _finish_failed(self, job: SendJob, reason: str) -> None:
        if job.done:
            reason = f"sent in part: {job.done} of {len(job.chunks)} ({reason})"
        self.emit({"type": "failed", "id": job.out_id, "error": reason[:480]})
        self._job = None
        log.warning("message %s not sent (%d of %d parts)", job.out_id, job.done, len(job.chunks))

    async def _process_locked(self, line: dict[str, Any] | None) -> None:
        async with self._lock:
            if line is None:
                if self._job is not None:
                    await self._run_job(self._job)
            else:
                await self._process(line)
            # Report own messages and anything that came in meanwhile now,
            # rather than at the next event or safety read.
            try:
                await self._sync_locked()
            except ToolError as exc:
                log.warning("read after a send failed: %s", short_reason(exc, 120))

    # --- loops ------------------------------------------------------------

    async def _events_loop(self) -> None:
        while True:
            try:
                if await self._poll_events(self.wait_seconds):
                    await self.sync()
            except ToolError as exc:
                log.warning("event wait failed: %s", short_reason(exc, 120))
                await asyncio.sleep(5)

    async def _reconcile_loop(self) -> None:
        while True:
            await asyncio.sleep(self.reconcile_seconds)
            try:
                await self.sync()
            except ToolError as exc:
                log.warning("safety read failed: %s", short_reason(exc, 120))

    async def _outbound_loop(self) -> None:
        while True:
            line = await self.outbox.get()
            # Shielded: stopping must not cut a send between Telegram and `sent`.
            self._in_flight = asyncio.ensure_future(self._process_locked(line))
            await asyncio.shield(self._in_flight)

    # --- lifecycle --------------------------------------------------------

    async def _resolve_chat(self) -> None:
        try:
            first = await self._messages_after(None, 1)
        except ChatAccessError as exc:
            raise ConfigError(f"cannot read the chat: {exc}") from exc
        except ToolError as exc:
            self._startup_failures += 1
            if self._startup_failures >= STARTUP_ATTEMPTS:
                raise ConfigError(f"cannot read the chat: {exc}") from exc
            raise
        self._startup_failures = 0
        self.chat_id = first.chat_id
        self.chat_type = first.chat_type
        log.info("chat %s (%s)", self.chat_id, self.chat_type)
        if self.cursor is None:
            newest = first.messages[-1]["id"] if first.messages else 0
            self.cursor = int(newest)
            self.hello_cursor = self.cursor
            if newest:
                self.emit({"type": "in", "cursor": str(newest)})
            log.info("no cursor: starting after message %s", newest)

    async def _startup(self) -> None:
        await self._backend.start()
        if self.chat_id is None:
            await self._resolve_chat()
        # Learn the event log's position before reading, so nothing that
        # arrives during the read is missed.
        await self._poll_events(timeout=0)
        if not self._started_once:
            # Let the hub hand messages left unanswered by an earlier process,
            # so they are matched against the chat before it is read.
            await asyncio.sleep(self.fresh_grace)
        async with self._lock:
            if self._job is not None:
                await self._run_job(self._job)
            while not self.outbox.empty():
                await self._process(self.outbox.get_nowait())
            await self._sync_locked()
        self._started_once = True

    async def _settle_in_flight(self, timeout: float | None) -> None:
        task, self._in_flight = self._in_flight, None
        if task is None:
            return
        if not task.done():
            await asyncio.wait({task}, timeout=timeout)
        if not task.done():
            task.cancel()
        results = await asyncio.gather(task, return_exceptions=True)
        if isinstance(results[0], BaseException) and not isinstance(
            results[0], asyncio.CancelledError
        ):
            log.info("previous send ended with %s", type(results[0]).__name__)

    async def serve(self, backend: Backend) -> None:
        """Run with one backend connection until stdin closes; transport errors
        propagate so the caller reconnects with backoff."""
        await self._settle_in_flight(None)
        self._backend = backend
        hello = asyncio.ensure_future(self.hello_seen.wait())
        closed = asyncio.ensure_future(self.closed.wait())
        await asyncio.wait({hello, closed}, return_when=asyncio.FIRST_COMPLETED)
        hello.cancel()
        if self.closed.is_set():
            closed.cancel()
            return
        try:
            await self._startup()
        except BaseException:
            closed.cancel()
            raise
        self.connected_at = time.monotonic()
        tasks = {
            asyncio.ensure_future(self._events_loop()),
            asyncio.ensure_future(self._reconcile_loop()),
            asyncio.ensure_future(self._outbound_loop()),
            asyncio.ensure_future(self._avatars_loop()),
        }
        try:
            done, _ = await asyncio.wait(tasks | {closed}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task is not closed and task.exception() is not None:
                    raise task.exception()
        finally:
            for task in tasks | {closed}:
                task.cancel()
            await asyncio.gather(*tasks, closed, return_exceptions=True)
            if self.closed.is_set():
                # The hub kills the bridge 5 s after SIGTERM; finish the send first.
                await self._settle_in_flight(4.0)
