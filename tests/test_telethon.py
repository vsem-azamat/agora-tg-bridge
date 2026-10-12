"""The Telethon backend against a fake TelegramClient; no network, no Telegram.

Messages are real Telethon message objects, so the record conversion is
tested against Telethon's own properties (voice, sticker, file, forward…).
"""

from __future__ import annotations

import asyncio
import base64
import json
import stat
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from telethon import errors, utils
from telethon.errors import rpc_message_to_error
from telethon.errors.common import AuthKeyNotFound
from telethon.tl import patched, types

from tests.test_bridge import Out, hello, out_line, settle
from tg_bridge.bridge import AVATAR_LINES, MAX_AVATAR_BYTES, Bridge, Marker
from tg_bridge.cli import main, run
from tg_bridge.config import ROBOT, ConfigError, parse_args
from tg_bridge.errors import AuthError, ChatAccessError, ToolError, TransportError
from tg_bridge.session import SessionData, load_session, save_session
from tg_bridge.telethon_backend import TelethonBackend, to_record

CHANNEL_ID = 1234567890
CHAT = -1001234567890  # the marked id of CHANNEL_ID
SELF_ID = 1
ADA = types.User(id=42, first_name="Ada", last_name="Lovelace")
ME = types.User(id=SELF_ID, first_name="Operator", is_self=True)
GROUP = types.Channel(
    id=CHANNEL_ID, title="Example", photo=types.ChatPhotoEmpty(), date=None, megagroup=True
)
EMOJI_ID = "5000000000000000001"
SESSION = SessionData(api_id=12345, api_hash="0123456789abcdef", session="1AAAA")
_CLIENT = SimpleNamespace(_self_id=SELF_ID, _mb_entity_cache=None)


def tg_msg(
    msg_id: int,
    text: str = "hi",
    *,
    sender: types.User | None = ADA,
    out: bool = False,
    peer: Any = None,
    **fields: Any,
) -> patched.Message:
    msg = patched.Message(
        id=msg_id,
        peer_id=peer or types.PeerChannel(CHANNEL_ID),
        date=datetime(2026, 10, 10, 18, 2, 11, tzinfo=UTC),
        message=text,
        out=out,
        from_id=types.PeerUser(sender.id) if sender else None,
        **fields,
    )
    msg._finish_init(_CLIENT, {sender.id: sender} if sender else {}, None)
    return msg


def rpc_error(code: int, message: str) -> Exception:
    """The error Telethon raises for this answer from Telegram."""
    return rpc_message_to_error(types.RpcError(code, message), None)


def document(mime: str, *attributes: Any) -> types.MessageMediaDocument:
    doc = types.Document(
        id=1,
        access_hash=1,
        file_reference=b"",
        date=None,
        mime_type=mime,
        size=10,
        dc_id=1,
        attributes=list(attributes),
    )
    return types.MessageMediaDocument(document=doc)


# --- records -----------------------------------------------------------------


def test_text_message_record():
    msg = tg_msg(11, "hello", mentioned=True, reply_to=types.MessageReplyHeader(reply_to_msg_id=10))
    assert to_record(msg, "supergroup") == {
        "id": 11,
        "sender": "Ada Lovelace",
        "sender_id": 42,
        "out": False,
        "date": "2026-10-10T18:02:11Z",
        "text": "hello",
        "reply_to": 10,
        "addressed": True,
        "sender_photo": "",
    }


def test_text_is_cleaned_as_telegram_mcp_cleans_it():
    rec = to_record(tg_msg(11, "a\u200bb\u202e\n\n\n\nc "), "group")
    assert rec["text"] == "ab\n\nc"


async def test_the_client_raises_telegrams_own_errors():
    from tg_bridge.telethon_backend import make_client

    client = make_client(SessionData(api_id=12345, api_hash="0123456789abcdef", session=""))
    assert client._raise_last_call_error is True


def test_addressed_follows_the_chat_type():
    plain = tg_msg(11)
    own = tg_msg(12, sender=ME, out=True, mentioned=True)
    assert to_record(plain, "private")["addressed"] is True
    assert to_record(plain, "supergroup")["addressed"] is False
    assert to_record(own, "private")["addressed"] is False
    assert to_record(own, "group")["addressed"] is False
    assert to_record(tg_msg(13, mentioned=True), "channel")["addressed"] is False


@pytest.mark.parametrize(
    ("media", "label", "extra"),
    [
        (
            types.MessageMediaPhoto(
                photo=types.Photo(
                    id=1, access_hash=1, file_reference=b"", date=None, sizes=[], dc_id=1
                )
            ),
            "photo",
            {},
        ),
        (
            document("audio/ogg", types.DocumentAttributeAudio(duration=12, voice=True)),
            "voice",
            {"duration": 12},
        ),
        (
            document(
                "video/mp4",
                types.DocumentAttributeVideo(duration=9, w=1, h=1, round_message=True),
            ),
            "video_note",
            {"duration": 9},
        ),
        (
            document(
                "image/webp",
                types.DocumentAttributeSticker(alt="😀", stickerset=types.InputStickerSetEmpty()),
            ),
            "sticker 😀",
            {},
        ),
        (
            document("application/pdf", types.DocumentAttributeFilename(file_name="a.pdf")),
            "document: a.pdf",
            {},
        ),
        (
            document(
                "video/mp4",
                types.DocumentAttributeVideo(duration=3, w=1, h=1),
                types.DocumentAttributeAnimated(),
            ),
            "gif",
            {},
        ),
        (types.MessageMediaGeo(geo=types.GeoPointEmpty()), "geo", {}),
    ],
)
def test_media_records(media, label, extra):
    rec = to_record(tg_msg(11, "", media=media), "supergroup")
    assert rec["media"] == label
    assert {k: rec[k] for k in extra} == extra
    assert "text" not in rec


def test_a_link_preview_is_not_media():
    preview = types.MessageMediaWebPage(webpage=types.WebPageEmpty(id=1))
    assert "media" not in to_record(tg_msg(11, "see https://example.org", media=preview), "group")


def test_poll_forward_and_custom_emoji():
    poll = types.MessageMediaPoll(
        poll=types.Poll(
            id=1, hash=0, question=types.TextWithEntities(text="Lunch?", entities=[]), answers=[]
        ),
        results=types.PollResults(),
    )
    assert to_record(tg_msg(11, "", media=poll), "group")["poll"] == {"question": "Lunch?"}

    fwd = types.MessageFwdHeader(date=datetime(2026, 1, 1, tzinfo=UTC), from_name="Grace")
    assert to_record(tg_msg(12, "x", fwd_from=fwd), "group")["forwarded"] == {
        "date": "2026-01-01T00:00:00Z",
        "from_name": "Grace",
    }

    entity = types.MessageEntityCustomEmoji(offset=0, length=2, document_id=int(EMOJI_ID))
    rec = to_record(tg_msg(13, f"{ROBOT} hi", entities=[entity], out=True), "group")
    assert rec["custom_emojis"] == [{"id": EMOJI_ID}]


def test_service_message_record_only_names_the_action():
    msg = patched.MessageService(
        id=14,
        peer_id=types.PeerChannel(CHANNEL_ID),
        date=None,
        action=types.MessageActionChatJoinedByLink(inviter_id=42),
    )
    msg._finish_init(_CLIENT, {}, None)
    rec = to_record(msg, "supergroup")
    assert rec["id"] == 14 and rec["action"] == "MessageActionChatJoinedByLink"


# --- a fake TelegramClient ---------------------------------------------------


class FakeClient:
    def __init__(self, history: list[Any] | None = None, entity: Any = GROUP) -> None:
        self.entity = entity
        self.history: list[Any] = list(history or [])
        self.handlers: list[Any] = []
        self.connected = False
        self.authorized = True
        self.cached = True  # the entity is in the session's cache
        self.dialogs_read = 0
        self.next_id = 1000
        self.sends: list[dict[str, Any]] = []
        self.send_error: BaseException | None = None
        self.deliver_despite_error = False
        self.send_delay = 0.0
        self.read_error: BaseException | None = None
        self.photos: dict[int, bytes] = {}
        self.photo_error: BaseException | None = None
        self.photo_errors: list[BaseException] = []  # raised once each, first
        self.photo_gate: asyncio.Event | None = None  # downloads wait for it
        self.downloads: list[tuple[Any, Any, bool]] = []

    async def connect(self) -> None:
        self.connected = True

    def is_connected(self) -> bool:
        return self.connected

    async def disconnect(self) -> None:
        self.connected = False

    async def is_user_authorized(self) -> bool:
        return self.authorized

    def add_event_handler(self, callback: Any, event: Any) -> None:
        self.handlers.append(callback)

    async def get_entity(self, chat: Any) -> Any:
        if not self.cached or chat != utils.get_peer_id(self.entity):
            raise ValueError("Could not find the input entity")
        return self.entity

    async def get_dialogs(self) -> list[Any]:
        self.dialogs_read += 1
        self.cached = True
        return []

    async def get_messages(
        self, entity: Any, limit: int, min_id: int = 0, reverse: bool = False
    ) -> list[Any]:
        if self.read_error:
            raise self.read_error
        if reverse:
            return [m for m in self.history if m.id > min_id][:limit]
        return list(reversed(self.history[-limit:]))

    async def download_profile_photo(
        self, entity: Any, file: Any = None, download_big: bool = True
    ) -> Any:
        self.downloads.append((entity, file, download_big))
        if self.photo_gate is not None:
            await self.photo_gate.wait()
        if self.photo_errors:
            raise self.photo_errors.pop(0)
        if self.photo_error:
            raise self.photo_error
        return self.photos.get(entity if isinstance(entity, int) else entity.id)

    async def arrive(self, msg: Any) -> None:
        self.history.append(msg)
        for handler in self.handlers:
            await handler(SimpleNamespace(chat_id=utils.get_peer_id(msg.peer_id)))

    async def send_message(self, entity: Any, text: str, **kw: Any) -> Any:
        self.sends.append({"text": text, **kw})
        if self.send_delay:
            await asyncio.sleep(self.send_delay)
        if self.send_error and not self.deliver_despite_error:
            raise self.send_error
        self.next_id += 1
        reply = kw.get("reply_to")
        msg = tg_msg(
            self.next_id,
            text,
            sender=ME,
            out=True,
            entities=kw.get("formatting_entities"),
            reply_to=types.MessageReplyHeader(reply_to_msg_id=reply) if reply else None,
        )
        self.history.append(msg)
        if self.send_error:
            raise self.send_error
        return msg


def backend_for(client: FakeClient, **kw: Any) -> TelethonBackend:
    return TelethonBackend(SESSION, client_factory=lambda session: client, **kw)


# --- the backend -------------------------------------------------------------


async def test_start_refuses_a_session_that_is_not_logged_in():
    client = FakeClient()
    client.authorized = False
    with pytest.raises(ConfigError):
        await backend_for(client).start()


async def test_reads_after_an_id_and_the_newest_without_one():
    client = FakeClient([tg_msg(i) for i in range(10, 15)])
    backend = backend_for(client)
    await backend.start()

    newest = await backend.messages_after(CHAT, None, 2)
    assert [r["id"] for r in newest.messages] == [13, 14]
    assert (newest.chat_id, newest.chat_type, newest.has_more) == (CHAT, "supergroup", True)
    after = await backend.messages_after(CHAT, 12, 100)
    assert [r["id"] for r in after.messages] == [13, 14] and not after.has_more


async def test_an_unknown_chat_is_looked_up_in_the_dialogs_once():
    client = FakeClient([tg_msg(10)])
    client.cached = False
    backend = backend_for(client)
    await backend.start()
    await backend.messages_after(CHAT, 0, 10)
    assert client.dialogs_read == 1

    other = backend_for(FakeClient())
    await other.start()
    with pytest.raises(ConfigError):
        await other.messages_after(-100999, 0, 10)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (errors.ChannelPrivateError(request=None), ChatAccessError),
        (errors.AuthKeyUnregisteredError(request=None), AuthError),
        (errors.AuthKeyDuplicatedError(request=None), AuthError),
        (AuthKeyNotFound(), AuthError),
        (rpc_error(406, "TOPIC_CLOSED"), ToolError),
        (rpc_error(500, "RPC_CALL_FAIL"), TransportError),
        (errors.FloodWaitError(request=None, capture=300), ToolError),
        (ConnectionError("reset"), TransportError),
    ],
)
async def test_read_errors_are_sorted(error, expected):
    client = FakeClient()
    client.read_error = error
    backend = backend_for(client)
    await backend.start()
    with pytest.raises(expected):
        await backend.messages_after(CHAT, 0, 10)


async def test_wait_wakes_on_a_new_message_of_the_chat_only():
    client = FakeClient([tg_msg(10)])
    backend = backend_for(client)
    await backend.start()
    await backend.messages_after(CHAT, 0, 10)

    assert await backend.wait_for_events(CHAT, 0.01) is False
    await client.arrive(tg_msg(11, peer=types.PeerUser(42)))  # another chat
    assert await backend.wait_for_events(CHAT, 0.01) is False
    await client.arrive(tg_msg(12))
    assert await backend.wait_for_events(CHAT, 5) is True
    assert await backend.wait_for_events(CHAT, 0) is False

    await client.disconnect()
    with pytest.raises(TransportError):
        await backend.wait_for_events(CHAT, 0.01)


async def test_send_with_custom_emoji_and_reply():
    client = FakeClient([tg_msg(10)])
    backend = backend_for(client)
    await backend.start()

    msg_id = await backend.send(CHAT, f"{ROBOT} hi", reply_to=10, custom_emoji_id=EMOJI_ID)

    assert msg_id == 1001
    (sent,) = client.sends
    assert sent["text"] == f"{ROBOT} hi" and sent["reply_to"] == 10
    assert sent["parse_mode"] is None
    (entity,) = sent["formatting_entities"]
    assert (entity.offset, entity.length, entity.document_id) == (0, 2, int(EMOJI_ID))

    await backend.send(CHAT, "[bot] plain", reply_to=None, custom_emoji_id=None)
    assert client.sends[1]["formatting_entities"] is None


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (errors.ChatWriteForbiddenError(request=None), ToolError),
        (errors.FloodWaitError(request=None, capture=300), ToolError),
        (rpc_error(406, "TOPIC_CLOSED"), ToolError),
        (ConnectionError("reset"), TransportError),
        (rpc_error(500, "RANDOM_ID_DUPLICATE"), TransportError),
        (rpc_error(500, "RPC_CALL_FAIL"), TransportError),
        (rpc_error(-503, "Timeout"), TransportError),
    ],
)
async def test_send_errors_are_refusals_or_unknown_outcomes(error, expected):
    client = FakeClient()
    client.send_error = error
    backend = backend_for(client)
    await backend.start()
    with pytest.raises(expected) as caught:
        await backend.send(CHAT, "x", reply_to=None, custom_emoji_id=None)
    assert type(caught.value) is expected  # a refused send is never fatal


async def test_a_send_without_an_answer_is_a_transport_error():
    client = FakeClient()
    client.send_delay = 1.0
    backend = backend_for(client, send_timeout=0.01)
    await backend.start()
    with pytest.raises(TransportError):
        await backend.send(CHAT, "x", reply_to=None, custom_emoji_id=None)


# --- the bridge over the Telethon backend ------------------------------------


MARKER = Marker(f"{ROBOT} ", EMOJI_ID)


async def test_bridge_reads_sends_and_skips_its_echo():
    client = FakeClient([tg_msg(10)])
    out = Out()
    bridge = Bridge(CHAT, out, reconcile_seconds=3600, wait_seconds=5, fresh_grace=0, marker=MARKER)
    bridge.handle_hub_line(hello(""))
    task = asyncio.create_task(bridge.serve(backend_for(client)))
    await settle()
    assert out.lines == [{"type": "in", "cursor": "10"}]

    await client.arrive(tg_msg(11, "question", mentioned=True))
    await settle()
    (line,) = [line for line in out.of("in") if "id" in line]
    assert line["author"] == {
        "id": "42",
        "name": "Ada Lovelace",
        "self": False,
        "avatar_rev": "none",
    }
    assert line["addressed"] is True and line["text"] == "question"

    bridge.handle_hub_line(out_line(318, "answer", reply_to="11"))
    await settle()
    assert out.of("sent") == [{"type": "sent", "id": 318, "ext_id": "1001"}]
    assert out.lines[-1] == {"type": "in", "cursor": "1001"}  # the echo only moves the cursor

    mark = types.MessageEntityCustomEmoji(offset=0, length=2, document_id=int(EMOJI_ID))
    await client.arrive(
        tg_msg(1002, f"{ROBOT} from another client", sender=ME, out=True, entities=[mark])
    )
    await settle()
    assert out.lines[-1]["author"]["id"] == "assistant"

    bridge.closed.set()
    await asyncio.wait_for(task, 2)


@pytest.mark.parametrize("error", [ConnectionError("reset"), rpc_error(500, "RANDOM_ID_DUPLICATE")])
async def test_lost_send_answer_is_found_in_the_chat_after_reconnect(error):
    client = FakeClient([tg_msg(10)])
    client.send_error = error
    client.deliver_despite_error = True
    out = Out()

    async def reader(bridge):
        bridge.handle_hub_line(hello("10"))
        await asyncio.sleep(0.2)
        bridge.handle_hub_line(out_line(5, "hello"))
        for _ in range(300):
            await asyncio.sleep(0.01)
            if out.of("sent") or out.of("failed"):
                break
        bridge.closed.set()

    def make_backend():
        client.connected = False
        return backend_for(client)

    config = parse_args(["--chat", str(CHAT), "--custom-emoji-id", EMOJI_ID])
    code = await asyncio.wait_for(
        run(
            config,
            make_backend,
            stdin_reader=reader,
            write=out,
            backoff_start=0.01,
            fresh_grace=0,
        ),
        5,
    )

    assert code == 0
    assert len(client.sends) == 1  # found in the chat, not sent again
    assert out.of("sent") == [{"type": "sent", "id": 5, "ext_id": "1001"}]


# --- the session file and the command line ---------------------------------


def test_session_file_round_trip_is_private(tmp_path):
    path = tmp_path / "dir" / "session.json"
    save_session(path, SESSION)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert load_session(path) == SESSION
    assert SESSION.api_hash not in repr(SESSION) and SESSION.session not in repr(SESSION)


def test_missing_or_broken_session_file_is_a_configuration_error(tmp_path):
    with pytest.raises(ConfigError):
        load_session(tmp_path / "absent.json")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"api_id": "x"}))
    with pytest.raises(ConfigError):
        load_session(bad)


def test_telethon_backend_is_the_default_and_needs_a_session(tmp_path, caplog):
    absent = str(tmp_path / "absent.json")
    assert parse_args(["--chat", "1"]).backend == "telethon"
    assert main(["--chat", "1", "--session", absent]) == 2
    assert "tg-bridge login" in caplog.text
    with pytest.raises(ConfigError):
        parse_args(["--chat", "1", "--backend", "other"])


def test_login_refuses_to_replace_a_session_without_force(tmp_path, capsys):
    path = tmp_path / "session.json"
    save_session(path, SESSION)
    assert main(["login", "--session", str(path)]) == 2
    assert "--force" in capsys.readouterr().err
    assert load_session(path) == SESSION


async def test_login_saves_the_session(tmp_path):
    from tg_bridge.account import login

    class LoginClient:
        def __init__(self, session, api_id, api_hash, **kw):
            self.session = session
            self.phone_asked = False

        async def start(self, phone):
            assert phone() == "+10000000000"
            self.session.save = lambda: "1BBBB"

        async def get_me(self):
            return ME

        async def disconnect(self):
            pass

    path = tmp_path / "session.json"
    name = await login(
        path,
        12345,
        "0123456789abcdef",
        ask=lambda prompt: "+10000000000",
        client_factory=LoginClient,
    )
    assert name == "Operator"
    assert load_session(path) == SessionData(12345, "0123456789abcdef", "1BBBB")


# --- avatars -----------------------------------------------------------------


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
GRACE = types.User(id=43, first_name="Grace", photo=types.UserProfilePhoto(photo_id=777, dc_id=2))


def test_record_names_the_senders_photo():
    assert to_record(tg_msg(11, sender=GRACE), "group")["sender_photo"] == "777"
    assert to_record(tg_msg(12), "group")["sender_photo"] == ""
    assert "sender_photo" not in to_record(tg_msg(13, sender=None), "group")


async def test_profile_photo_is_downloaded_small_through_the_seen_sender():
    client = FakeClient([tg_msg(10, sender=GRACE)])
    client.photos[GRACE.id] = JPEG
    backend = backend_for(client)
    await backend.start()
    await backend.messages_after(CHAT, 0, 10)

    assert await backend.profile_photo(GRACE.id) == JPEG
    assert client.downloads == [(GRACE, bytes, False)]
    assert await backend.profile_photo(ADA.id) is None  # not seen: asked by id, has none
    assert client.downloads[-1][0] == ADA.id


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ValueError("Could not find the input entity"), ToolError),
        (rpc_error(400, "PEER_ID_INVALID"), ToolError),
        (ConnectionError("reset"), TransportError),
    ],
)
async def test_profile_photo_errors_are_sorted(error, expected):
    client = FakeClient()
    client.photo_error = error
    backend = backend_for(client)
    await backend.start()
    with pytest.raises(expected):
        await backend.profile_photo(GRACE.id)


async def _avatar_bridge(client: FakeClient, **kw: Any) -> tuple[Bridge, Out, asyncio.Task]:
    out = Out()
    bridge = Bridge(
        CHAT, out, reconcile_seconds=3600, wait_seconds=5, fresh_grace=0, marker=MARKER, **kw
    )
    bridge.handle_hub_line(hello("10"))
    task = asyncio.create_task(bridge.serve(backend_for(client)))
    await settle()
    return bridge, out, task


async def test_a_senders_avatar_goes_to_the_hub_once_per_photo():
    client = FakeClient([tg_msg(10)])
    client.photos[GRACE.id] = JPEG
    bridge, out, task = await _avatar_bridge(client)

    await client.arrive(tg_msg(11, sender=GRACE))
    await settle()
    await client.arrive(tg_msg(12, sender=GRACE))  # after its avatar went out
    await client.arrive(tg_msg(13))  # Ada has no photo
    await settle()

    ins = [line for line in out.of("in") if "id" in line]
    assert [line["author"]["avatar_rev"] for line in ins] == ["777", "777", "none"]
    assert out.of("avatar") == [
        {
            "type": "avatar",
            "author": "43",
            "mime": "image/jpeg",
            "data": base64.b64encode(JPEG).decode(),
            "rev": "777",
        },
        {"type": "avatar", "author": "42", "data": "", "rev": "none"},
    ]

    changed = types.User(
        id=43, first_name="Grace", photo=types.UserProfilePhoto(photo_id=778, dc_id=2)
    )
    await client.arrive(tg_msg(14, sender=changed))
    await settle()
    assert [line["rev"] for line in out.of("avatar")] == ["777", "none", "778"]
    await stop_bridge(bridge, task)


async def test_own_and_assistant_messages_carry_no_avatar():
    me = types.User(
        id=SELF_ID,
        first_name="Operator",
        is_self=True,
        photo=types.UserProfilePhoto(photo_id=5, dc_id=2),
    )
    client = FakeClient([tg_msg(10)])
    bridge, out, task = await _avatar_bridge(client)

    mark = types.MessageEntityCustomEmoji(offset=0, length=2, document_id=int(EMOJI_ID))
    await client.arrive(tg_msg(11, "from my phone", sender=me, out=True))
    await client.arrive(tg_msg(12, f"{ROBOT} an assistant", sender=me, out=True, entities=[mark]))
    await settle()

    ins = [line for line in out.of("in") if "id" in line]
    assert len(ins) == 2 and all("avatar_rev" not in line["author"] for line in ins)
    assert out.of("avatar") == [] and client.downloads == []
    await stop_bridge(bridge, task)


async def test_an_unreadable_or_odd_photo_is_skipped_until_it_changes(caplog):
    client = FakeClient([tg_msg(10)])
    client.photo_error = rpc_error(400, "PEER_ID_INVALID")
    bridge, out, task = await _avatar_bridge(client)

    await client.arrive(tg_msg(11, sender=GRACE))
    await settle()
    await client.arrive(tg_msg(12, sender=GRACE))
    await settle()
    assert out.of("avatar") == [] and len(client.downloads) == 1
    assert "avatar of 43 not read" in caplog.text

    client.photo_error = None
    client.photos[GRACE.id] = b"GIF89a"
    gif = types.User(id=43, first_name="Grace", photo=types.UserProfilePhoto(photo_id=779, dc_id=2))
    await client.arrive(tg_msg(13, sender=gif))
    await settle()
    assert out.of("avatar") == [] and len(client.downloads) == 2
    assert "not JPEG or PNG" in caplog.text
    await stop_bridge(bridge, task)


async def test_avatar_lines_keep_within_the_hubs_rate():
    people = [
        types.User(
            id=100 + i, first_name=f"P{i}", photo=types.UserProfilePhoto(photo_id=i, dc_id=2)
        )
        for i in range(AVATAR_LINES + 1)
    ]
    client = FakeClient([tg_msg(10)])
    for person in people:
        client.photos[person.id] = JPEG
    bridge, out, task = await _avatar_bridge(client, avatar_window=0.5)

    for i, person in enumerate(people):
        await client.arrive(tg_msg(11 + i, sender=person))
    await settle()
    assert len(out.of("avatar")) == AVATAR_LINES

    await asyncio.sleep(0.6)
    assert len(out.of("avatar")) == AVATAR_LINES + 1
    await stop_bridge(bridge, task)


def person(user_id: int, photo_id: int) -> types.User:
    return types.User(
        id=user_id,
        first_name=f"P{user_id}",
        photo=types.UserProfilePhoto(photo_id=photo_id, dc_id=2),
    )


async def test_a_png_is_sent_and_a_too_large_photo_skipped(caplog):
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    client = FakeClient([tg_msg(10)])
    client.photos[GRACE.id] = JPEG[:4] + b"\x00" * MAX_AVATAR_BYTES
    client.photos[44] = png
    bridge, out, task = await _avatar_bridge(client)

    await client.arrive(tg_msg(11, sender=GRACE))
    await client.arrive(tg_msg(12, sender=person(44, 9)))
    await settle()

    (line,) = out.of("avatar")
    assert line["author"] == "44" and line["mime"] == "image/png"
    assert base64.b64decode(line["data"]) == png
    assert f"avatar of 43 skipped: image/jpeg, {MAX_AVATAR_BYTES + 4} bytes" in caplog.text
    await stop_bridge(bridge, task)


async def test_a_photo_changed_during_its_download_is_sent_too():
    client = FakeClient([tg_msg(10)])
    client.photos[GRACE.id] = JPEG
    client.photo_gate = asyncio.Event()
    bridge, out, task = await _avatar_bridge(client)

    await client.arrive(tg_msg(11, sender=GRACE))
    await settle()
    await client.arrive(tg_msg(12, sender=person(43, 778)))
    await settle()
    client.photo_gate.set()
    await settle()

    assert [line["rev"] for line in out.of("avatar")] == ["777", "778"]
    await stop_bridge(bridge, task)


async def test_a_download_without_an_answer_is_tried_again_on_the_same_connection(caplog):
    client = FakeClient([tg_msg(10)])
    client.photos[GRACE.id] = JPEG
    client.photo_errors = [ConnectionError("reset")]
    bridge, out, task = await _avatar_bridge(client, avatar_retry=0.5)

    await client.arrive(tg_msg(11, sender=GRACE))
    await settle()
    assert out.of("avatar") == [] and "avatar of 43: no answer" in caplog.text
    await asyncio.sleep(0.5)

    assert [line["rev"] for line in out.of("avatar")] == ["777"]
    assert not task.done() and client.connected
    await stop_bridge(bridge, task)


async def test_a_download_without_an_answer_goes_to_the_back_of_the_queue():
    client = FakeClient([tg_msg(10)])
    client.photos[GRACE.id] = JPEG
    client.photos[44] = JPEG
    client.photo_errors = [ConnectionError("reset")]
    bridge, out, task = await _avatar_bridge(client, avatar_retry=0.05)

    await client.arrive(tg_msg(11, sender=GRACE))
    await client.arrive(tg_msg(12, sender=person(44, 9)))
    await settle()

    assert [line["author"] for line in out.of("avatar")] == ["44", "43"]
    await stop_bridge(bridge, task)


async def test_avatars_still_waiting_are_sent_after_a_reconnect():
    people = [person(100 + i, i) for i in range(AVATAR_LINES + 1)]
    client = FakeClient([tg_msg(10)])
    for p in people:
        client.photos[p.id] = JPEG
    bridge, out, task = await _avatar_bridge(client, avatar_window=3600)
    for i, p in enumerate(people):
        await client.arrive(tg_msg(11 + i, sender=p))
    await settle()
    assert len(out.of("avatar")) == AVATAR_LINES

    await client.disconnect()
    await client.arrive(tg_msg(100, sender=None))
    with pytest.raises(TransportError):
        await asyncio.wait_for(task, 2)
    bridge.avatar_window = 0.01
    task = asyncio.create_task(bridge.serve(backend_for(client)))
    await settle()

    assert len(out.of("avatar")) == AVATAR_LINES + 1
    await stop_bridge(bridge, task)


async def stop_bridge(bridge: Bridge, task: asyncio.Task) -> None:
    bridge.closed.set()
    await asyncio.wait_for(task, 2)
