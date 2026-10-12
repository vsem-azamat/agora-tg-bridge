# Telethon backend

[Docs](README.md) / **Telethon backend**

The default backend (`--backend telethon`, `telethon_backend.py`): the bridge's own connection to Telegram through [Telethon](https://github.com/LonamiWebs/Telethon), logged in as your account. It needs nothing else running.

## Session file

`tg-bridge login` (`account.py`) asks for the phone number, the login code and, when the account has one, the two-step verification password, and writes the session file (`session.py`, default `~/.local/share/agora-tg-bridge/session.json`, `--session` to choose another):

```json
{"version": 1, "api_id": 12345, "api_hash": "…", "session": "<Telethon StringSession>"}
```

The API id and hash come from `$TG_API_ID` and `$TG_API_HASH`, the config file's `api_id` and `api_hash`, or a prompt; an application registered at <https://my.telegram.org/apps> gives them. The file is a full login to the account: it is written atomically with mode 0600 in a directory with mode 0700, the bridge warns when others can read it, and neither the file nor its values are ever logged. `login` refuses to replace an existing file without `--force`. The login shows in Telegram's active sessions as `agora-tg-bridge`; ending it there logs the bridge out.

The bridge loads the session into memory (a `StringSession`) and never writes the file. Give each bridge its own session file (`tg-bridge login --session <file>`, then `--session <file>` on that bridge's command): whether Telegram delivers updates to every connection of one login is not verified, and a bridge without updates sees new messages only at its safety read.

`tg-bridge chats` lists the account's recent chats with the numeric ids `--chat` takes.

## Reading and events

- **Chat.** The chat is resolved once per connection with `get_entity`; an id not in the session's cache yet is looked up in the dialog list once, and an id the account does not have is a configuration error. The chat type comes from the entity: a user is `private`, a basic group `group`, a megagroup `supergroup`, a broadcast channel `channel`.
- **History.** `get_messages` with `min_id` and `reverse` reads the messages after an id, oldest first; without an id it reads the newest ones. Each message becomes the same record the MCP backend returns ([MCP backend](mcp-backend.md#get_messages_after)): sender, date in UTC, text cleaned as telegram-mcp cleans it (`text.normalize`: invisible and format characters removed, runs of blank lines collapsed), `reply_to`, `media` (a link preview is not media), `duration` for voice and video messages, the poll question, the forward's origin, custom emoji ids, `sender_photo` (the id of the sender's profile photo, empty when there is none or it is hidden), and `addressed` (private chat: every message except own ones; group: Telegram's mention flag; channel: never). Service messages carry only their action and move the cursor.
- **Events.** A `NewMessage` handler wakes the bridge for every new message of the chat, incoming or the account's own from another device. The bridge then reads the chat; the safety read every `--reconcile` seconds covers updates Telegram did not deliver.

## Profile photos

The backend keeps the last 2000 senders it read (`SENDERS_KEPT`) and downloads a sender's small profile photo through that entity (`download_profile_photo`, `download_big=False`), so senders Telegram sends only as min users work too. A kept sender without a photo has none and nothing is downloaded. A sender no longer kept is asked for by id; one Telethon cannot find, and a download that brings nothing (Telethon's answer to some refusals), is a refusal like Telegram's own errors, so a failed download is never reported as no photo.

## Sending

`send_message` sends plain text (`parse_mode=None`, so no Markdown is applied), as a reply when `reply_to` is set. With `--custom-emoji-id`, the first character of the message gets a `MessageEntityCustomEmoji` with that document id.

Telethon sleeps through flood waits of up to 60 s (`FLOOD_SLEEP_SECONDS`) and retries server errors itself; the client raises Telegram's own error after its last retry (`raise_last_call_error`). A send waits up to 115 s (`SEND_TIMEOUT`) for its answer.

## Errors

| Telethon raises | The bridge does |
| --- | --- |
| The session is not authorized, `UnauthorizedError`, `AuthKeyDuplicatedError`, `AuthKeyNotFound` | Exits with status 2: log in again |
| `ChannelPrivateError`, `ForbiddenError` and its kind on a read | At start: exits with status 2. Later: logs and reads again at the next event |
| The same on a send | Answers `failed` with the error's name |
| `FloodWaitError` longer than 60 s, other `RPCError` (other 406 errors included) | Reads: logs and retries. Sends: `failed` |
| `ServerError` (such as `RANDOM_ID_DUPLICATE` after a resent request), `TimedOutError` | Like a dropped connection: Telegram may have done it |
| `ConnectionError`, `OSError`, a timeout, a dropped connection | Reconnects with backoff; a send without an answer is checked in the chat ([delivery guarantees](architecture.md#delivery-guarantees)) |
