# MCP backend

[Docs](README.md) / **MCP backend**

With `--backend mcp` (`mcp_backend.py`) the bridge talks to a [telegram-mcp](https://github.com/chigwell/telegram-mcp) server through an MCP hub over streamable HTTP (`mcp_http.py`): `initialize`, then `tools/list` and `tools/call`, each call an independent POST with the hub token as a bearer token, so a long-poll and a send run at the same time. Responses may be JSON or a server-sent-event stream. An expired session (HTTP 400 or 404) is re-initialized once; HTTP 401 or 403 is a configuration error.

The endpoint is `<hub-url>/telegram-<account>/mcp`. At start the bridge checks that `tools/list` offers the three required tools below (`get_messages_after`, `wait_for_events`, `send_message`) and exits with status 2 naming the missing ones otherwise. These tools come from a telegram-mcp fork with a per-message event log; upstream telegram-mcp does not offer `wait_for_events` and `get_messages_after` yet. Without such a server, use the Telethon backend.

## `get_messages_after`

Arguments: `chat_id`, `after_id` (optional), `limit` (at most 100).

Messages of the chat with an id above `after_id`, oldest first; without `after_id`, the newest `limit` messages, oldest first. The result is JSON:

```json
{"chat_id": -1001234567890, "chat_type": "supergroup", "has_more": false,
 "messages": [{"id": 5513, "sender": "Ada", "sender_id": 42, "date": "2026-10-10T18:02:11Z",
               "text": "…", "out": false, "addressed": true, "reply_to": 5510}]}
```

`chat_type` is `private`, `group`, `supergroup` or `channel`. A record may also carry `media` (`photo`, `voice`, `video_note`, `video`, `audio`, `gif`, `document: <name>`, `sticker <alt>`, `contact`, `geo`, `poll`), `duration`, `poll.question`, `forwarded` (`from_user`, `from_chat` or `from_name`), `transcript`, `action` (a service message) and `custom_emojis` (`[{"emoji": "🤖", "id": "…"}]`). It may also carry `sender_photo`: the id of the sender's current profile photo, `""` for none or hidden.

## Forum topics

A telegram-mcp server that names forum topics says so in the description of `get_messages_after`, which mentions its `topics` field. Then a record of a message in a topic carries `topic`, the topic's id (the id of the topic's first message), and the result carries one `topics` map of id to title for the topics present, so no title repeats per record:

```json
{"chat_id": -1001234567890, "chat_type": "supergroup", "has_more": false,
 "messages": [{"id": 5520, "…": "…", "topic": 77},
              {"id": 5521, "…": "…", "topic": 77, "reply_to": 5520}],
 "topics": {"77": "Release"}}
```

`reply_to` is only a real reply: a message that merely sits in a topic has none. The backend copies each title into its record as `topic_title`, so records are the same as the Telethon backend's, and the bridge makes each topic a titled Agora thread as described in [Architecture](architecture.md#forum-topics). The General topic (`topic` 1) is the room's, so the backend drops its `topic`. A topic the map does not name (the server could not read its title) is a thread without a title until a later record names it; a rename shows up the same way.

The bridge declares `threads: topic` in a second `hello` on either of two signals: `start` finds that mark in the description of `get_messages_after`, or a read returns a record with `topic` (or a `topics` map). The bridge sees what the hub lists, and the hub may keep the tool list it had when it connected to telegram-mcp, so a server updated to name topics behind such a list is noticed by its first record with a topic; the hello with `threads` goes out before the `in` line of that record. A server that does neither (upstream telegram-mcp, or a fork without topics) leaves `topic` out of the records and `threads` out of the caps, and a forum's messages land in the room. Once declared, `threads` stays for the process: a reconnect builds a new backend that may not know yet (the hub's list is stale), and a `hello` without `threads` would make the hub drop the `thread` of `out` lines. Going back to a server without topics means removing the bridges first, which restarts them. Limit: after a process restart behind a stale tool list, `threads` is declared only at the first record with a topic, so a reply handed to the bridge before that goes out without its thread (into the room, not the topic).

An `out` line with a `thread` goes into the topic by `send_message` with `reply_to` set to the topic's id (the first part to its own `reply_to` when it has one, as in the Telethon backend); `send_message` needs nothing else for topics. The server's `wait_for_events` events carry `topic` too; the bridge only uses them as a signal.

## `get_profile_photo` (optional)

Arguments: `peer_id`.

A peer's current profile photo as a small JPEG: JSON with `peer_id`, `photo_id` (the id `sender_photo` names; `""` for none) and, with a photo, `mime` and `data` (base64). When the endpoint does not offer it, the backend drops `sender_photo` from the records, so the bridge announces no avatars it could not send. An answer that is not this JSON, names another peer, or names a photo without `data` is a refusal: the bridge logs it and skips that person's avatar until the photo changes, so a photo that failed to download is never recorded as none.

## `wait_for_events`

Arguments: `chat_id`, `after_seq`, `boot` (from the previous result), `timeout` (seconds, at most 100).

Long-polls a per-message event log: returns as soon as there are events for the chat with a sequence number above `after_seq`, else after `timeout` with none. The result is JSON with `boot`, `last_seq`, `events`, `gap` and `reset`. The bridge only uses it as a signal: any event, gap or reset triggers a read with `get_messages_after`.

## `send_message`

Arguments: `chat_id`, `message`, `reply_to` (optional), `custom_emoji_id` (only with `--custom-emoji-id`: the first character of the message is sent as that custom emoji).

The answer is text starting with `Message sent successfully` and naming `message_id=<id>`; any other answer, or a tool error, is a refused send and its text becomes the `failed` reason.
