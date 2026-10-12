# Architecture

[Docs](README.md) / **Architecture**

The bridge is one process per bridged chat, connected to Telegram through a backend: [Telethon](telethon-backend.md) (its own login) or [MCP](mcp-backend.md) (telegram-mcp behind an MCP hub). Both give the bridge the same message records, events and sends (`backend.Backend`), so everything below holds for either.

The Agora hub starts the bridge, writes `hello` and `out` lines to its standard input and reads `in`, `avatar`, `sent` and `failed` lines from its standard output; its standard error goes to the hub's log.

| Module | Holds |
| --- | --- |
| `cli.py` | Entry point: reads stdin, writes stdout, the reconnect loop with backoff, signals |
| `config.py` | Command line, the config file and the MCP hub token |
| `backend.py` | What the bridge needs from a backend: `Backend`, `History`, `Record` |
| `errors.py` | Backend errors, sorted by what the bridge does about them |
| `telethon_backend.py` | The Telethon backend |
| `session.py` | The Telethon session file |
| `account.py` | `tg-bridge login` and `tg-bridge chats` |
| `mcp_backend.py` | The MCP backend |
| `bridge.py` | The bridge itself: cursor, reading, send jobs, the delivery check |
| `text.py` | UTF-16 length and the text normalization telegram-mcp applies, for comparing |
| `render.py` | A message record to an `in` line: placeholders for media, forwards, service messages |
| `mcp_http.py` | A small MCP client over streamable HTTP |

## Start

The bridge waits for `hello`, starts the backend (Telethon: connects and checks the session is logged in; MCP: checks that the endpoint offers the tools it needs) and resolves the chat. A fresh process then waits 1.5 s (`FRESH_GRACE_SECONDS`), so messages the hub hands again after a restart are matched against the chat before it is read.

- With an empty cursor it starts after the newest message (no history import) and reports that position as a cursor-only `in` line.
- With a cursor (a Telegram message id) it reads every message after it, in pages of 100.

## Inbound

It waits for events of this chat (Telethon: a new-message update; MCP: `wait_for_events`, also on a gap or a server restart) and reads the chat after the cursor whenever one comes, and every `--reconcile` seconds (default 60) as a safety net.

Each message becomes an `in` line:

- `id` and `cursor`: the message id;
- `author`: `id`, `name`, `self` for the account's own messages written from another device, and `avatar_rev` (see [Avatars](#avatars));
- `text`, `reply_to`, `at`;
- `addressed`: in a private chat every message except own ones; in a group Telegram's mention flag, which covers mentions of the account and replies to its messages.

Media become placeholders (`[photo]`, `[voice 0:12]`, `[video message 0:09]`, `[file name.pdf]`, `[sticker 😀]`, `[poll: …]`, `[location]`, …), forwards get `[forwarded from …]`, a voice transcript the server already has is added and marked as not verbatim. Service messages only move the cursor.

The marker (`Marker`: `--prefix`, optionally `--custom-emoji-id`) tells assistants' messages from the operator's. An own message that starts with the prefix (with `--custom-emoji-id`: with the prefix's first character, and carries that custom emoji, whatever text follows) is not the operator's: unless it is one of this bridge's, it comes in from the author `assistant (<prefix>)` with `self: false` and is not addressed. Texts are compared after the normalization telegram-mcp applies to message text (`text.normalize`, applied to the Telethon backend's raw text too) (invisible characters and leading whitespace removed), so a prefix `[bot] ` needs its space: `[bot]note` is the operator's.

## Avatars

When the backend names each sender's profile photo (`sender_photo` in the record: the Telethon backend always, the MCP backend when the endpoint offers `get_profile_photo`), the `in` line's author carries `avatar_rev`: the photo's id, or `none` for a sender without a photo or one who hides it. The first time a process sees a person with a rev, it writes an `avatar` line for them: the small profile photo (160 px JPEG) in base64 with its `mime`, or empty `data` for `none`. The hub ignores a rev it already has without decoding, so sending every avatar again after a restart costs nothing there. The account's own messages and assistants' messages carry no avatar: the hub shows them as the operator's or an assistant's.

Avatar lines are written by their own loop, outside the chat lock, at most 30 in any 65 s (`AVATAR_LINES`, `AVATAR_WINDOW_SECONDS`; the hub drops more than 30 a minute); the others wait their turn, oldest first, also across a reconnect. A download that gets no answer goes to the back of the queue and the loop pauses for 30 s (`AVATAR_RETRY_SECONDS`); it does not end the connection. A photo Telegram refuses, or one that is not a JPEG or PNG of at most 256 KiB, is logged and skipped until the person's photo changes.

## Outbound

Each `out` line becomes a send job: one send per part, as plain text starting with the prefix (its first character as the custom emoji when `--custom-emoji-id` is set), the first part as a reply when `reply_to` names a Telegram message. Text over 4000 UTF-16 units (`MAX_MESSAGE_UTF16`) is split at line breaks or spaces into several messages, each with the prefix.

The bridge answers `sent` with the first message id (without `ext_id` when the server named none) before any read of the chat can see it: reads and sends share one lock. Its own messages coming back only move the cursor. An `out` id handed twice in one process is sent once.

A refused send answers `failed` with a short reason; when a later part of a split message fails, the reason says `sent in part: N of M`.

## Delivery guarantees

The bridge never sends a message twice and never answers blindly, within these rules:

- **Earlier processes.** A message the hub hands before `hello` or within 30 s of it (`CLAIM_WINDOW_SECONDS`; the hub re-hands unanswered messages within about a second) may have been sent by an earlier process that died before answering `sent`. For such a message the bridge first looks in the chat after the `hello` cursor for its own marked message with exactly that text and the same `reply_to` that is not already known as one of its own, and claims it instead of sending again. If that copy was already reported as an incoming message, the hub merges it into the sent message.
- **Running process.** Normal sends in a running process never claim, so an identical assistant message from another client cannot swallow them.
- **Lost answers.** A send waits up to 115 s for its answer (Telethon, in either backend, may sleep through a 60 s flood wait). When a call gets no answer, the bridge does not answer either: it reconnects and looks in the chat, then once more after 65 s (`RECHECK_SECONDS`), and answers `sent` if the message is there or `failed` if it is not. The second check holds the chat lock, so incoming messages wait for it too.
- **Stopping.** On `SIGTERM` a send in flight is finished (for up to 4 s; the hub kills the bridge 5 s after `SIGTERM`) before the process exits.

Limits: the check matches text and `reply_to`. A message the hub hands more than 30 s after `hello` is always sent; if an earlier process had already delivered it, it goes out twice. With the MCP backend, messages the same account sends through other telegram-mcp clients produce no event; they arrive with the next safety read.

## Failures

Connection errors and temporary Telegram errors reconnect with backoff from 1 s to 60 s, reset after a minute of steady work. These exit with status 2, so the Agora hub's own restart backoff applies:

- no session file, a session that is not logged in or that Telegram refused (Telethon);
- a token the MCP hub refuses;
- a missing tool;
- a chat refused by the server's allowlist;
- a chat that cannot be read in 6 attempts in a row (`STARTUP_ATTEMPTS`).

Logs go to standard error and carry ids and counts, never message text or the token.
