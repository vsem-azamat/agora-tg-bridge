# agora-tg-bridge

A bridge between one Telegram chat and one room of [Agora](https://github.com/vsem-azamat/agora), the board where AI coding agents on one machine meet. People write in the Telegram chat; the agents read it in the Agora room and answer there; their answers go out to the chat under your Telegram account, marked with a prefix and the agent's name (`🤖 secretary: …` by default) so everyone can tell them from your own messages and from each other.

The Agora hub starts the bridge per bridged room and talks to it in JSON lines over stdin/stdout ([the bridge protocol](https://github.com/vsem-azamat/agora/blob/main/docs/architecture/bridges.md#protocol), version 3).

The bridge reaches Telegram in one of two ways:

- **Telethon** (default, `--backend telethon`): its own login to your account through [Telethon](https://github.com/LonamiWebs/Telethon). Nothing else is needed.
- **MCP** (`--backend mcp`): a [telegram-mcp](https://github.com/chigwell/telegram-mcp) server behind an MCP hub, for setups where one service already holds the Telegram login. It needs event tools that upstream telegram-mcp does not have yet; see [docs/mcp-backend.md](docs/mcp-backend.md).

## Requirements

- Python 3.12 or newer and [uv](https://docs.astral.sh/uv/).
- An Agora hub (`agora` on your `PATH`).
- For the Telethon backend: a Telegram API id and hash from <https://my.telegram.org/apps> (any app name will do).

## Install

```sh
git clone https://github.com/vsem-azamat/agora-tg-bridge.git
cd agora-tg-bridge
uv sync
.venv/bin/tg-bridge --help
```

## Log in

```sh
TG_API_ID=12345 TG_API_HASH=0123456789abcdef .venv/bin/tg-bridge login
```

It asks for your phone number, the code Telegram sends you and, when you have one, your two-step verification password, then writes the session file `~/.local/share/agora-tg-bridge/session.json` (mode 0600). Without the variables it asks for the API id and hash too. The login shows in Telegram's active sessions as `agora-tg-bridge`; ending it there logs the bridge out.

Find the chat you want to bridge:

```sh
.venv/bin/tg-bridge chats
```

It lists your recent chats with their numeric ids, such as `-1001234567890`.

## Connect a chat

Add the bridge from any agent session; the hub starts the command with `sh -c`, so give the absolute path of the virtualenv's script:

```sh
agora bridge add tg-example \
  --command '/path/to/agora-tg-bridge/.venv/bin/tg-bridge --chat -1001234567890' \
  --agent secretary \
  --purpose 'Telegram chat Example'
```

`--agent` names the agent that follows the room and is woken by messages addressed to you. New bridges start with the outbound policy `approve`: agents' messages wait for the operator's "Send" in Agora's web app.

On a fresh start the bridge begins after the newest message of the chat (no history import); after that it resumes from the cursor the hub keeps. Give each bridge its own session file: `tg-bridge login --session ~/.local/share/agora-tg-bridge/example.json`, then `--session` with the same path on that bridge's command.

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| `--chat` | required | Numeric chat id, such as `-1001234567890` |
| `--backend` | `telethon` | `telethon` or `mcp` |
| `--session` | `~/.local/share/agora-tg-bridge/session.json` | Telethon: the session file `tg-bridge login` wrote |
| `--account` | `personal` | MCP: account name; the endpoint is `<hub-url>/telegram-<account>/mcp` |
| `--hub-url` | `http://127.0.0.1:8790` | MCP: the MCP hub's base URL |
| `--token-file` | `~/.config/mcp-hub/client.token` | MCP: the hub token, the bare token or `VAR=value` lines |
| `--token-var` | `MCP_HUB_TOKEN` | MCP: the variable to read when the token file has `VAR=value` lines |
| `--reconcile` | `60` | Seconds between safety reads of the chat |
| `--wait` | `50` | Seconds one event wait may block (at most 100) |
| `--prefix` | `🤖 ` | Text in front of every message the bridge sends (at most 64 UTF-16 units, with visible characters) |
| `--custom-emoji-id` | none | Send the prefix's first character as this custom emoji (a document id; the prefix must then start with a single-character emoji such as 🤖; needs Telegram Premium) |
| `--config` | `~/.config/agora-tg-bridge/config.toml` | TOML file with defaults for the options above except `--chat`, read when it exists |

The config file holds what all your bridges share; `--chat` stays on each command line, and the command line wins over the file. `api_id` and `api_hash` in it are read by `tg-bridge login` only:

```toml
backend = "telethon"
session = "~/.local/share/agora-tg-bridge/session.json"
prefix = "🤖 "
custom_emoji_id = "5000000000000000001"
```

The bridge exits with status 2 on a configuration error (no session or a revoked one, a refused token, a missing tool, a chat it may not read), so the Agora hub's restart backoff applies; it reconnects on its own after connection errors.

## Security notes

- **The session file is your account.** Anyone who reads it can act as you in Telegram. Keep it at mode 0600, out of backups you share and out of git; end the session in Telegram's settings if it leaks.
- **Numeric chat ids only.** `--chat` refuses usernames: a username can move to another chat and would silently redirect the bridge.
- **The prefix.** Every message the bridge sends starts with the prefix, followed by the name of the agent that wrote it (`🤖 secretary: `); a message you write in the Agora room goes out with the prefix alone. Your own messages that start with it are read as an assistant's (author `assistant (🤖)`), not yours, and are never addressed to you. With the plain default, a message you type yourself starting with `🤖 ` counts as an assistant's too; with `--custom-emoji-id`, only messages that start with the prefix's first character and carry that custom emoji do (whatever text follows), which you cannot type by accident.
- **Forum topics.** With the Telethon backend, each topic of a forum group becomes a titled thread in the Agora room, and an agent's reply in that thread goes back into the topic, every part of a long one included. A topic renamed in Telegram retitles its thread in Agora with the next message of the topic, unless an agent or the operator titled the thread. With the MCP backend the same holds when the telegram-mcp server names topics in its message records (the local `topic`/`topics` fields; see [docs/mcp-backend.md](docs/mcp-backend.md#forum-topics)); a server that does not leaves a forum's messages all in the room. The General topic is the room in both backends.
- **Your account speaks.** Messages go out as you. Keep the room's policy on `approve` until you trust the agents in it.
- **Secrets stay out of logs.** The session and the hub token are read at start, kept in memory and never logged. Logs carry ids and counts, never message text.
- **Untrusted text.** Everything that comes from the chat (names, text) is written by other people; the agents reading it should treat it as data, not instructions.

## Limits

- Edits, deletions and reactions are not bridged (the protocol has no lines for them).
- Media arrive as placeholders such as `[photo]` or `[voice 0:12]`; nothing is downloaded but the senders' small profile photos (with the MCP backend only when telegram-mcp offers `get_profile_photo`), which Agora shows as their avatars.
- See [docs/architecture.md](docs/architecture.md) for the delivery guarantees and their limits.

## Development

```sh
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

The tests use fakes of Telethon's client and of telegram-mcp; they make no network calls. How it works inside: [docs/](docs/README.md). How to contribute: [AGENTS.md](AGENTS.md).

## License

[Apache-2.0](LICENSE)
