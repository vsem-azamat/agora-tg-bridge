"""Bridge protocol tests against a fake telegram-mcp; no network, no Telegram."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from tg_bridge.bridge import (
    Bridge,
    Marker,
    split_message,
)
from tg_bridge.config import ROBOT, ConfigError
from tg_bridge.errors import ToolError, TransportError
from tg_bridge.mcp_backend import SEND_READ_TIMEOUT, McpBackend
from tg_bridge.text import normalize, utf16_len

CHAT = -1001234567890
EMOJI_ID = "5000000000000000001"
MARKER = Marker(f"{ROBOT} ", EMOJI_ID)


def msg(msg_id: int, text: str = "hi", **extra: Any) -> dict[str, Any]:
    rec = {
        "id": msg_id,
        "sender": "Ada",
        "sender_id": 42,
        "date": "2026-10-10T18:02:11Z",
        "text": text,
        "out": False,
        "addressed": False,
    }
    rec.update(extra)
    return rec


class FakeTelegram:
    """Plays telegram-mcp: a chat history, an event log, send_message."""

    def __init__(self, chat_type: str = "supergroup", history: list[dict] | None = None) -> None:
        self.chat_type = chat_type
        self.history: list[dict] = list(history or [])
        self.calls: list[tuple[str, dict]] = []
        self.tools = ["wait_for_events", "get_messages_after", "send_message", "get_me"]
        self.seq = 0
        self.boot = "boot-1"
        self.event = asyncio.Event()
        self.next_id = 1000
        self.send_result: str | None = None
        self.send_error: Exception | None = None
        self.deliver_despite_error = False  # Telegram took it, but the answer was lost
        self.fail_send_number: int | None = None  # 1-based send call that is refused
        self.history_error: Exception | None = None
        self.history_errors: list[Exception] = []
        self.sends = 0
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True

    async def list_tools(self) -> list[str]:
        return self.tools

    def arrive(self, rec: dict) -> None:
        self.history.append(rec)
        self.seq += 1
        self.event.set()

    async def call_tool(self, name: str, args: dict, *, read_timeout: float = 60.0) -> str:
        self.calls.append((name, dict(args)))
        if name == "get_messages_after":
            if self.history_errors:
                raise self.history_errors.pop(0)
            if self.history_error:
                raise self.history_error
            limit = args["limit"]
            if "after_id" in args:
                found = [m for m in self.history if m["id"] > args["after_id"]][:limit]
            else:
                found = self.history[-limit:]
            return json.dumps(
                {
                    "chat_id": CHAT,
                    "chat_type": self.chat_type,
                    "has_more": len(found) >= limit,
                    "messages": found,
                }
            )
        if name == "wait_for_events":
            if args.get("after_seq", 0) >= self.seq and args["timeout"] > 0:
                self.event.clear()
                try:
                    await asyncio.wait_for(self.event.wait(), args["timeout"])
                except TimeoutError:
                    pass
            events = [{"seq": self.seq}] if self.seq > args.get("after_seq", 0) else []
            return json.dumps(
                {"boot": self.boot, "gap": False, "last_seq": self.seq, "events": events}
            )
        if name == "send_message":
            self.sends += 1
            if self.fail_send_number == self.sends:
                return "An error occurred (code: MSG-ERR-123)."
            if self.send_error and not self.deliver_despite_error:
                raise self.send_error
            if self.send_result is not None:
                return self.send_result
            self.next_id += 1
            # A sent message is in the history at once, as with Telegram.
            sent = robot(self.next_id, args["message"])
            if "custom_emoji_id" in args:
                sent["custom_emojis"] = [{"emoji": ROBOT, "id": args["custom_emoji_id"]}]
            else:
                del sent["custom_emojis"]
            self.history.append(sent)
            if self.send_error:
                raise self.send_error
            return f"Message sent successfully. message_id={self.next_id}"
        raise AssertionError(name)


def robot(msg_id: int, text: str) -> dict[str, Any]:
    """An own message starting with the robot custom emoji, as get_messages_after shows it."""
    return msg(
        msg_id,
        normalize(text),
        out=True,
        sender="Operator",
        sender_id=1,
        custom_emojis=[{"emoji": ROBOT, "id": EMOJI_ID}],
    )


class Out:
    def __init__(self) -> None:
        self.lines: list[dict] = []

    def __call__(self, line: dict) -> None:
        json.dumps(line)  # must be serialisable
        self.lines.append(line)

    def of(self, kind: str) -> list[dict]:
        return [line for line in self.lines if line["type"] == kind]


def hello(cursor: str = "") -> str:
    return json.dumps(
        {"type": "hello", "version": 1, "bridge": "ex", "room": "ex", "cursor": cursor}
    )


async def started(
    tg: FakeTelegram, cursor: str = "", **kw: Any
) -> tuple[Bridge, Out, asyncio.Task]:
    out = Out()
    bridge = kw.pop("bridge", None) or Bridge(
        CHAT,
        out,
        reconcile_seconds=kw.pop("reconcile", 3600),
        wait_seconds=kw.pop("wait", 5),
        fresh_grace=0,
        recheck_seconds=kw.pop("recheck", 0.01),
        marker=kw.pop("marker", MARKER),
    )
    out = bridge._write if isinstance(bridge._write, Out) else out
    bridge.connected_at = None
    if bridge.hello is None:
        bridge.handle_hub_line(hello(cursor))
    task = asyncio.create_task(bridge.serve(McpBackend(tg)))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if bridge.connected_at is not None:
            break
    return bridge, out, task


async def stop(bridge: Bridge, task: asyncio.Task) -> None:
    bridge.closed.set()
    await asyncio.wait_for(task, 2)


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0.01)


async def test_first_start_begins_after_the_newest_message():
    tg = FakeTelegram(history=[msg(10), msg(11)])
    bridge, out, task = await started(tg)

    assert out.lines == [{"type": "in", "cursor": "11"}]
    assert bridge.chat_id == CHAT
    await stop(bridge, task)


async def test_resume_reads_missed_messages_from_the_cursor():
    tg = FakeTelegram(
        history=[msg(10), msg(11, "missed", mentioned=True, addressed=True, reply_to=10), msg(12)]
    )
    bridge, out, task = await started(tg, cursor="10")

    ins = out.of("in")
    assert [line["id"] for line in ins] == ["11", "12"]
    first = ins[0]
    assert first["text"] == "missed"
    assert first["author"] == {"id": "42", "name": "Ada", "self": False}
    assert first["reply_to"] == "10"
    assert first["at"] == "2026-10-10T18:02:11Z"
    assert first["cursor"] == "11"
    assert first["addressed"] is True
    assert ins[1]["addressed"] is False
    await stop(bridge, task)


async def test_pages_through_a_long_gap():
    tg = FakeTelegram(history=[msg(i) for i in range(1, 251)])
    bridge, out, task = await started(tg, cursor="0")

    assert len(out.of("in")) == 250
    assert out.lines[-1]["cursor"] == "250"
    await stop(bridge, task)


async def test_push_event_reads_the_new_message_at_once():
    tg = FakeTelegram(history=[msg(10)])
    bridge, out, task = await started(tg, cursor="10")

    tg.arrive(msg(11, "fresh"))
    await settle()

    assert [line.get("id") for line in out.of("in")] == ["11"]
    await stop(bridge, task)


async def test_event_wait_passes_chat_boot_and_position():
    tg = FakeTelegram(history=[msg(10)])
    bridge, out, task = await started(tg, cursor="10")
    tg.arrive(msg(11))
    await settle()

    waits = [args for name, args in tg.calls if name == "wait_for_events"]
    assert waits[0]["timeout"] == 0  # position learnt before the first read
    assert all(args["chat_id"] == CHAT for args in waits)
    assert waits[-1]["after_seq"] == 1 and waits[-1]["boot"] == "boot-1"
    await stop(bridge, task)


async def test_reconciliation_catches_what_no_event_announced():
    tg = FakeTelegram(history=[msg(10)])
    bridge, out, task = await started(tg, cursor="10", reconcile=0.05)

    tg.history.append(msg(11, "quiet"))  # no event, e.g. sent by another MCP client
    await asyncio.sleep(0.2)

    assert [line.get("id") for line in out.of("in")] == ["11"]
    await stop(bridge, task)


async def test_private_chat_messages_are_addressed_and_own_are_self():
    tg = FakeTelegram(chat_type="private", history=[msg(10)])
    bridge, out, task = await started(tg, cursor="10")

    tg.arrive(msg(11, "from them", addressed=True))
    tg.arrive(msg(12, "from my phone", out=True, addressed=False))
    await settle()

    them, me = out.of("in")
    assert them["addressed"] is True and them["author"]["self"] is False
    assert me["addressed"] is False and me["author"]["self"] is True
    await stop(bridge, task)


async def test_media_and_service_messages():
    tg = FakeTelegram(history=[msg(10)])
    bridge, out, task = await started(tg, cursor="10")

    tg.arrive(msg(11, "", media="photo"))
    tg.arrive(msg(12, "", media="voice", duration=12))
    tg.arrive(msg(13, "see attached", media="document: name.pdf"))
    tg.arrive(msg(14, "", media="sticker 😀"))
    tg.arrive(msg(15, "", action="MessageActionChatAddUser"))
    await settle()

    texts = [line.get("text") for line in out.of("in")]
    assert texts == [
        "[photo]",
        "[voice 0:12]",
        "[file name.pdf] see attached",
        "[sticker 😀]",
        None,
    ]
    assert out.lines[-1] == {"type": "in", "cursor": "15"}
    await stop(bridge, task)


async def test_out_is_sent_with_the_robot_and_answered_before_its_echo():
    tg = FakeTelegram(history=[msg(5513)])
    tg.next_id = 5514
    bridge, out, task = await started(tg, cursor="5513")

    bridge.handle_hub_line(
        json.dumps(
            {
                "type": "out",
                "id": 318,
                "author": "secretary",
                "text": "Hello there",
                "reply_to": "5513",
            }
        )
    )
    await settle()
    await bridge.sync()  # the echo is in the history now

    name, args = [c for c in tg.calls if c[0] == "send_message"][0]
    assert args["message"] == f"{ROBOT} Hello there"
    assert args["custom_emoji_id"] == EMOJI_ID
    assert args["reply_to"] == 5513 and args["chat_id"] == CHAT
    sent_at = out.lines.index({"type": "sent", "id": 318, "ext_id": "5515"})
    echo = [i for i, line in enumerate(out.lines) if line.get("cursor") == "5515"]
    assert echo and echo[0] > sent_at
    assert "id" not in out.lines[echo[0]]  # own message: cursor only, never an `in`
    await stop(bridge, task)


async def test_send_failure_answers_failed_with_a_short_reason():
    tg = FakeTelegram(history=[msg(10)])
    tg.send_result = "Access to chat '@x' is restricted by privacy policy.\nmore"
    bridge, out, task = await started(tg, cursor="10")

    bridge.handle_hub_line(json.dumps({"type": "out", "id": 7, "author": "a", "text": "x"}))
    await settle()

    (failed,) = out.of("failed")
    assert failed["id"] == 7
    assert "\n" not in failed["error"] and "restricted" in failed["error"]
    await stop(bridge, task)


async def test_empty_out_fails_without_sending():
    tg = FakeTelegram(history=[msg(10)])
    bridge, out, task = await started(tg, cursor="10")

    bridge.handle_hub_line(json.dumps({"type": "out", "id": 8, "author": "a", "text": "  "}))
    bridge.handle_hub_line(json.dumps({"type": "out", "id": 9, "author": "a", "text": "\u200b"}))
    await settle()

    assert out.of("failed") == [
        {"type": "failed", "id": 8, "error": "empty message"},
        {"type": "failed", "id": 9, "error": "empty message"},
    ]
    assert not [c for c in tg.calls if c[0] == "send_message"]
    await stop(bridge, task)


def out_line(out_id: int, text: str, **extra: Any) -> str:
    return json.dumps({"type": "out", "id": out_id, "author": "secretary", "text": text, **extra})


async def test_lost_answer_is_checked_in_the_chat_after_reconnect():
    tg = FakeTelegram(history=[msg(10)])
    tg.send_error = TransportError("ReadTimeout")
    tg.deliver_despite_error = True
    bridge, out, task = await started(tg, cursor="10")

    bridge.handle_hub_line(out_line(9, "x"))
    with pytest.raises(TransportError):
        await asyncio.wait_for(task, 2)
    assert not out.of("sent") and not out.of("failed")  # no blind answer

    tg.send_error = None
    bridge2, out2, task2 = await started(tg, bridge=bridge)
    assert out.of("sent") == [{"type": "sent", "id": 9, "ext_id": "1001"}]
    assert not [line for line in out.of("in") if line.get("id") == "1001"]  # not an echo
    assert [c for c in tg.calls if c[0] == "send_message"].__len__() == 1  # not sent twice
    assert bridge._in_flight is None  # the broken send was collected
    await stop(bridge, task2)


async def test_lost_send_not_in_the_chat_fails():
    tg = FakeTelegram(history=[msg(10)])
    tg.send_error = TransportError("ReadTimeout")
    bridge, out, task = await started(tg, cursor="10")

    bridge.handle_hub_line(out_line(9, "x"))
    with pytest.raises(TransportError):
        await asyncio.wait_for(task, 2)
    tg.send_error = None
    _, _, task2 = await started(tg, bridge=bridge)

    (failed,) = out.of("failed")
    assert failed["id"] == 9 and "not found in the chat after" in failed["error"]
    assert tg.sends == 1
    await stop(bridge, task2)


async def test_running_process_never_claims_an_identical_assistant_message():
    # A: another client sent the same 🤖 text through the account (no event); a normal
    # send in a running process must still go out.
    tg = FakeTelegram(history=[msg(10)])
    bridge, out, task = await started(tg, cursor="10")
    bridge.handle_hub_line(out_line(1, "first"))
    await settle()
    bridge._hello_at -= 60  # long after hello, and this process has sent before

    tg.history.append(robot(2000, f"{ROBOT} same"))  # not yet read
    bridge.handle_hub_line(out_line(2, "same"))
    await settle()

    assert tg.sends == 2
    assert out.of("sent")[-1] == {"type": "sent", "id": 2, "ext_id": "1002"}
    await stop(bridge, task)


async def test_out_after_the_window_is_sent_even_before_any_own_send():
    # A process that has sent nothing yet (the usual state after a hub restart)
    # must not claim an identical assistant message once the window is over.
    tg = FakeTelegram(history=[msg(10)])
    bridge, out, task = await started(tg, cursor="10")
    bridge._hello_at -= 60

    tg.history.append(robot(2000, f"{ROBOT} same"))  # another client, after the hello cursor
    bridge.handle_hub_line(out_line(2, "same"))
    await settle()

    assert tg.sends == 1
    assert out.of("sent") == [{"type": "sent", "id": 2, "ext_id": "1001"}]
    await stop(bridge, task)


async def test_claim_requires_the_same_reply_to():
    tg = FakeTelegram(history=[msg(10), robot(11, f"{ROBOT} hello")])
    out = Out()
    bridge = Bridge(CHAT, out, reconcile_seconds=3600, wait_seconds=5, fresh_grace=0, marker=MARKER)
    bridge.handle_hub_line(hello("10"))
    bridge.handle_hub_line(out_line(318, "hello", reply_to="10"))  # 11 replies to nothing
    task = asyncio.create_task(bridge.serve(McpBackend(tg)))
    await settle()

    assert tg.sends == 1
    assert out.of("sent") == [{"type": "sent", "id": 318, "ext_id": "1001"}]
    await stop(bridge, task)


async def test_message_handed_after_the_first_read_is_still_claimed():
    # B: the previous process's copy (11) was already read as an assistant
    # message when the hub hands the unanswered message again.
    tg = FakeTelegram(history=[msg(10), robot(11, f"{ROBOT} hello")])
    out = Out()
    bridge = Bridge(CHAT, out, reconcile_seconds=3600, wait_seconds=5, fresh_grace=0, marker=MARKER)
    bridge.handle_hub_line(hello("10"))
    task = asyncio.create_task(bridge.serve(McpBackend(tg)))
    await settle()
    assert [line["id"] for line in out.of("in") if "id" in line] == ["11"]  # imported

    bridge.handle_hub_line(out_line(318, "hello"))
    await settle()

    assert tg.sends == 0
    assert out.of("sent") == [{"type": "sent", "id": 318, "ext_id": "11"}]  # the hub merges
    await stop(bridge, task)


async def test_uncertain_send_is_checked_again_after_a_flood_wait():
    # C: the message shows up only after the first check (Telegram was still
    # sleeping through a flood wait when the bridge reconnected).
    tg = FakeTelegram(history=[msg(10)])
    tg.send_error = TransportError("ReadTimeout")
    bridge, out, task = await started(tg, cursor="10", recheck=0.3)

    bridge.handle_hub_line(out_line(9, "late"))
    with pytest.raises(TransportError):
        await asyncio.wait_for(task, 2)
    tg.send_error = None
    task2 = asyncio.create_task(bridge.serve(McpBackend(tg)))
    await asyncio.sleep(0.1)
    assert not out.of("failed") and not out.of("sent")  # still waiting
    tg.history.append(robot(1500, f"{ROBOT} late"))
    await asyncio.sleep(0.4)

    assert out.of("sent") == [{"type": "sent", "id": 9, "ext_id": "1500"}]
    assert not out.of("failed") and tg.sends == 1
    await stop(bridge, task2)


async def test_send_waits_longer_than_a_flood_wait():
    tg = FakeTelegram(history=[msg(10)])
    timeouts = []
    original = tg.call_tool

    async def spy(name, args, *, read_timeout=60.0):
        if name == "send_message":
            timeouts.append(read_timeout)
        return await original(name, args, read_timeout=read_timeout)

    tg.call_tool = spy
    bridge, out, task = await started(tg, cursor="10")
    bridge.handle_hub_line(out_line(1, "x"))
    await settle()

    assert timeouts == [SEND_READ_TIMEOUT] and 60 < SEND_READ_TIMEOUT < 120
    await stop(bridge, task)


async def test_fresh_process_claims_a_message_delivered_before_a_crash():
    # The previous process sent message 11 and died before answering `sent`.
    tg = FakeTelegram(history=[msg(10), robot(11, f"{ROBOT} hello"), msg(12, "after")])
    out = Out()
    bridge = Bridge(CHAT, out, reconcile_seconds=3600, wait_seconds=5, fresh_grace=0.05)
    bridge.handle_hub_line(hello("10"))
    task = asyncio.create_task(bridge.serve(McpBackend(tg)))
    await asyncio.sleep(0.01)
    bridge.handle_hub_line(out_line(318, "hello"))  # handed again after the restart
    await settle()

    assert out.of("sent") == [{"type": "sent", "id": 318, "ext_id": "11"}]
    assert tg.sends == 0
    ins = [line for line in out.of("in") if "id" in line]
    assert [line["id"] for line in ins] == ["12"]  # 11 only moved the cursor
    assert out.lines.index(out.of("sent")[0]) < out.lines.index({"type": "in", "cursor": "11"})
    await stop(bridge, task)


async def test_fresh_process_claims_every_part_of_a_split_message():
    text = "word " * 2000
    parts = split_message(text)
    history = [msg(10)] + [robot(11 + i, f"{ROBOT} {p}") for i, p in enumerate(parts)]
    tg = FakeTelegram(history=history)
    out = Out()
    bridge = Bridge(CHAT, out, reconcile_seconds=3600, wait_seconds=5, fresh_grace=0.05)
    bridge.handle_hub_line(hello("10"))
    bridge.handle_hub_line(out_line(2, text))
    task = asyncio.create_task(bridge.serve(McpBackend(tg)))
    await settle()

    assert out.of("sent") == [{"type": "sent", "id": 2, "ext_id": "11"}]
    assert tg.sends == 0
    assert not [line for line in out.of("in") if "id" in line]
    await stop(bridge, task)


async def test_assistant_messages_through_the_account_are_not_the_operators():
    tg = FakeTelegram(history=[msg(10)])
    bridge, out, task = await started(tg, cursor="10")

    tg.arrive(robot(11, f"{ROBOT} from another client"))
    tg.arrive(msg(12, "from my phone", out=True, custom_emojis=[]))
    await settle()

    other, me = [line for line in out.of("in") if "id" in line]
    assert other["author"] == {"id": "assistant", "name": f"assistant ({ROBOT})", "self": False}
    assert other["addressed"] is False
    assert me["author"]["self"] is True
    await stop(bridge, task)


async def test_with_a_custom_emoji_any_text_after_it_marks_an_assistant():
    tg = FakeTelegram(history=[msg(10)])
    bridge, out, task = await started(tg, cursor="10")

    tg.arrive(robot(11, f"{ROBOT}\nanother assistant"))
    await settle()

    (line,) = [line for line in out.of("in") if "id" in line]
    assert line["author"]["id"] == "assistant"
    await stop(bridge, task)


async def test_a_robot_text_without_the_custom_emoji_is_the_operators():
    tg = FakeTelegram(history=[msg(10)])
    bridge, out, task = await started(tg, cursor="10")

    tg.arrive(msg(11, f"{ROBOT} typed by hand", out=True))
    await settle()

    (line,) = [line for line in out.of("in") if "id" in line]
    assert line["author"]["self"] is True
    await stop(bridge, task)


async def test_a_plain_prefix_is_sent_without_a_custom_emoji():
    tg = FakeTelegram(history=[msg(10)])
    bridge, out, task = await started(tg, cursor="10", marker=Marker("[bot] "))

    bridge.handle_hub_line(out_line(5, "Hello"))
    await settle()

    (args,) = [a for name, a in tg.calls if name == "send_message"]
    assert args["message"] == "[bot] Hello"
    assert "custom_emoji_id" not in args
    assert out.of("sent") == [{"type": "sent", "id": 5, "ext_id": "1001"}]
    await stop(bridge, task)


async def test_with_a_plain_prefix_any_own_message_starting_with_it_is_an_assistants():
    tg = FakeTelegram(history=[msg(10)])
    bridge, out, task = await started(tg, cursor="10", marker=Marker(f"{ROBOT} "))

    tg.arrive(msg(11, f"{ROBOT} from another client", out=True))
    tg.arrive(msg(12, f"{ROBOT} from someone else", out=False))
    tg.arrive(msg(13, "plain", out=True))
    await settle()

    other, someone, me = [line for line in out.of("in") if "id" in line]
    assert other["author"] == {"id": "assistant", "name": f"assistant ({ROBOT})", "self": False}
    assert someone["author"]["name"] == "Ada"
    assert me["author"]["self"] is True
    await stop(bridge, task)


async def test_an_early_message_is_not_claimed_from_an_own_message_without_the_custom_emoji():
    tg = FakeTelegram(history=[msg(10)])
    # Typed by hand after the hello cursor: same text, but no custom emoji.
    tg.history.append(msg(11, f"{ROBOT} hello", out=True))
    out = Out()
    bridge = Bridge(CHAT, out, reconcile_seconds=3600, wait_seconds=5, fresh_grace=0, marker=MARKER)
    bridge.handle_hub_line(hello("10"))
    bridge.handle_hub_line(out_line(318, "hello"))
    task = asyncio.create_task(bridge.serve(McpBackend(tg)))
    await settle()

    assert tg.sends == 1
    assert out.of("sent") == [{"type": "sent", "id": 318, "ext_id": "1001"}]
    await stop(bridge, task)


async def test_the_prefix_must_be_followed_by_its_space():
    tg = FakeTelegram(history=[msg(10)])
    bridge, out, task = await started(tg, cursor="10", marker=Marker("[bot] "))

    tg.arrive(msg(11, "[bot]note to self", out=True))
    tg.arrive(msg(12, "\u200b[bot] from another client", out=True))
    await settle()

    me, other = [line for line in out.of("in") if "id" in line]
    assert me["author"]["self"] is True
    assert other["author"] == {"id": "assistant", "name": "assistant ([bot])", "self": False}
    await stop(bridge, task)


async def test_a_prefix_with_many_line_breaks_still_matches():
    marker = Marker("[bot]\n\n\n")
    assert marker.matches({"out": True, "text": normalize(marker.text("hello"))})
    assert not marker.matches({"out": True, "text": "[bot] hello"})


async def test_sent_without_an_id_omits_ext_id():
    tg = FakeTelegram(history=[msg(10)])
    tg.send_result = "Message sent successfully."
    bridge, out, task = await started(tg, cursor="10")

    bridge.handle_hub_line(out_line(4, "x"))
    await settle()

    assert out.of("sent") == [{"type": "sent", "id": 4}]
    await stop(bridge, task)


async def test_an_out_handed_twice_is_sent_once():
    tg = FakeTelegram(history=[msg(10)])
    bridge, out, task = await started(tg, cursor="10")

    bridge.handle_hub_line(out_line(5, "x"))
    bridge.handle_hub_line(out_line(5, "x"))
    await settle()
    bridge.handle_hub_line(out_line(5, "x"))
    await settle()

    assert tg.sends == 1
    assert len(out.of("sent")) == 1
    await stop(bridge, task)


async def test_a_failed_later_part_is_reported_as_sent_in_part():
    tg = FakeTelegram(history=[msg(10)])
    tg.fail_send_number = 2
    bridge, out, task = await started(tg, cursor="10")

    bridge.handle_hub_line(out_line(6, "word " * 2000))
    await settle()

    (failed,) = out.of("failed")
    assert failed["id"] == 6 and failed["error"].startswith("sent in part: 1 of 3")
    assert not out.of("sent")
    assert not [line for line in out.of("in") if "id" in line]  # part 1 is own
    await stop(bridge, task)


async def test_temporary_failure_at_start_is_retried_not_fatal():
    tg = FakeTelegram(history=[msg(10)])
    tg.history_errors = [ToolError("An error occurred (code: GEN-ERR-001).")]
    bridge = Bridge(CHAT, Out(), fresh_grace=0)
    bridge.handle_hub_line(hello("10"))

    with pytest.raises(ToolError) as info:
        await bridge.serve(McpBackend(tg))
    assert not isinstance(info.value, ConfigError)

    _, _, task = await started(tg, bridge=bridge)  # the reconnect succeeds
    assert bridge.chat_id == CHAT
    await stop(bridge, task)


async def test_persistent_failure_at_start_becomes_a_configuration_error():
    tg = FakeTelegram()
    tg.history_error = ToolError("An error occurred (code: GEN-ERR-001).")
    bridge = Bridge(CHAT, Out(), fresh_grace=0)
    bridge.handle_hub_line(hello())

    for _ in range(5):
        with pytest.raises(ToolError):
            await bridge.serve(McpBackend(tg))
    with pytest.raises(ConfigError):
        await bridge.serve(McpBackend(tg))


async def test_out_lines_wait_for_the_connection():
    tg = FakeTelegram(history=[msg(10)])
    out = Out()
    bridge = Bridge(CHAT, out, reconcile_seconds=3600, wait_seconds=5, fresh_grace=0, marker=MARKER)
    bridge.handle_hub_line(json.dumps({"type": "out", "id": 1, "author": "a", "text": "early"}))
    bridge.handle_hub_line(hello("10"))
    task = asyncio.create_task(bridge.serve(McpBackend(tg)))
    await settle()

    assert out.of("sent") == [{"type": "sent", "id": 1, "ext_id": "1001"}]
    await stop(bridge, task)


async def test_long_out_is_split_and_every_part_carries_the_robot():
    tg = FakeTelegram(history=[msg(10)])
    bridge, out, task = await started(tg, cursor="10")

    bridge.handle_hub_line(
        json.dumps(
            {"type": "out", "id": 2, "author": "a", "text": "word " * 2000, "reply_to": "10"}
        )
    )
    await settle()
    await bridge.sync()

    sends = [args for name, args in tg.calls if name == "send_message"]
    assert len(sends) == 3
    assert all(a["message"].startswith(ROBOT + " ") for a in sends)
    assert "reply_to" in sends[0] and "reply_to" not in sends[1]
    assert out.of("sent") == [{"type": "sent", "id": 2, "ext_id": "1001"}]
    assert not [line for line in out.of("in") if "id" in line]  # no part echoes back
    await stop(bridge, task)


async def test_missing_event_tools_is_a_configuration_error():
    tg = FakeTelegram(history=[msg(10)])
    tg.tools = ["get_messages", "send_message"]
    out = Out()
    bridge = Bridge(CHAT, out)
    bridge.handle_hub_line(hello())

    with pytest.raises(ConfigError):
        await bridge.serve(McpBackend(tg))


async def test_restricted_chat_is_a_configuration_error():
    tg = FakeTelegram()
    tg.history_error = ToolError("Access to chat '-1' is restricted by privacy policy.")
    bridge = Bridge(CHAT, Out(), fresh_grace=0)
    bridge.handle_hub_line(hello())

    with pytest.raises(ConfigError):
        await bridge.serve(McpBackend(tg))


async def test_unknown_lines_are_ignored():
    bridge = Bridge(CHAT, Out())
    bridge.handle_hub_line("not json")
    bridge.handle_hub_line(json.dumps({"type": "typing"}))
    bridge.handle_hub_line(json.dumps({"type": "out", "text": "no id"}))
    assert bridge.outbox.empty() and bridge.hello is None


async def test_stdin_closing_before_hello_returns():
    bridge = Bridge(CHAT, Out())
    bridge.closed.set()
    await asyncio.wait_for(bridge.serve(McpBackend(FakeTelegram())), 1)


def test_split_message_respects_utf16_limit():
    text = ("😀" * 3000) + " tail"
    parts = split_message(text, 4000)
    assert all(utf16_len(p) <= 4000 for p in parts)
    assert "".join(parts).replace(" ", "") == text.replace(" ", "")
    assert split_message("short") == ["short"]
