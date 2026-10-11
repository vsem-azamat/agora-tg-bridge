# MCP backend

[Docs](README.md) / **MCP backend**

The bridge talks to a [telegram-mcp](https://github.com/chigwell/telegram-mcp) server through an MCP hub over streamable HTTP (`mcp_http.py`): `initialize`, then `tools/list` and `tools/call`, each call an independent POST with the hub token as a bearer token, so a long-poll and a send run at the same time. Responses may be JSON or a server-sent-event stream. An expired session (HTTP 400 or 404) is re-initialized once; HTTP 401 or 403 is a configuration error.

The endpoint is `<hub-url>/telegram-<account>/mcp`. At start the bridge checks that `tools/list` offers all three tools below and exits with status 2 naming the missing ones otherwise.

## `get_messages_after`

Arguments: `chat_id`, `after_id` (optional), `limit` (at most 100).

Messages of the chat with an id above `after_id`, oldest first; without `after_id`, the newest `limit` messages, oldest first. The result is JSON:

```json
{"chat_id": -1001234567890, "chat_type": "supergroup", "has_more": false,
 "messages": [{"id": 5513, "sender": "Ada", "sender_id": 42, "date": "2026-10-10T18:02:11Z",
               "text": "…", "out": false, "addressed": true, "reply_to": 5510}]}
```

`chat_type` is `private`, `group`, `supergroup` or `channel`. A record may also carry `media` (`photo`, `voice`, `video_note`, `video`, `audio`, `gif`, `document: <name>`, `sticker <alt>`, `contact`, `geo`, `poll`), `duration`, `poll.question`, `forwarded` (`from_user`, `from_chat` or `from_name`), `transcript`, `action` (a service message) and `custom_emojis` (`[{"emoji": "🤖", "id": "…"}]`).

## `wait_for_events`

Arguments: `chat_id`, `after_seq`, `boot` (from the previous result), `timeout` (seconds, at most 100).

Long-polls a per-message event log: returns as soon as there are events for the chat with a sequence number above `after_seq`, else after `timeout` with none. The result is JSON with `boot`, `last_seq`, `events`, `gap` and `reset`. The bridge only uses it as a signal: any event, gap or reset triggers a read with `get_messages_after`.

## `send_message`

Arguments: `chat_id`, `message`, `reply_to` (optional), `custom_emoji_id` (only with `--custom-emoji-id`: the first character of the message is sent as that custom emoji).

The answer is text starting with `Message sent successfully` and naming `message_id=<id>`; any other answer, or a tool error, is a refused send and its text becomes the `failed` reason.
