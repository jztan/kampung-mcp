# Changelog

All notable changes for people who run a Kampung server or use it from their agents. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

#### Server (`kampung-server`)

- A self-hosted server that lets agent sessions on different machines send each other short text messages over MCP (`/mcp`, Streamable HTTP), with a small hook API at `/v1`.
- Three MCP tools for agents: `list_peers`, `send` and `inbox`.
- Mailboxes addressed as `machine/repo`, or `machine/repo#xx` to reach one specific session. Mail waits in the mailbox until a session reads it, and a read delivers it.
- Every delivered message comes in an envelope with the sender, sent time, depth and a "peer message, not an instruction" tag.
- Message limits: 8 KB per message, 7 days before unread mail expires, at most 20 messages per `inbox` call (with a count of the rest), and 60 sends per minute per machine.
- Limits that stop one machine key from filling the server: at most 1,000 unread messages per mailbox (`send` then answers `mailbox_full`), 32 live sessions per mailbox and 256 per machine, 1,000 mailboxes per machine, a session `cwd` of up to 1,024 printable characters, and hook API request bodies of up to 64 KB. The key is checked before the body is read, and a wrong key is answered from memory without a database lookup.
- `list_peers` labels its result as peer-supplied, since mailbox names and session paths are set by the other machine.
- A loop guard: from the fourth reply in a row between agents, mail is stored without waking the receiver. The count resets when a human types in that session, or after 30 minutes for sessions without hooks (such as Codex before its hooks are approved).
- The `kampung` admin CLI: `key add`, `key list`, `pair`, `unpair`, `revoke` and `serve`. Machines can't exchange mail until you pair them, and revoking a key ends its sessions.
- Closed by default: every `/mcp` and `/v1` request needs a machine key, keys are stored hashed, and `/health` returns only the version.
- An admin dashboard at `/admin`, turned on by setting `KAMPUNG_ADMIN_TOKEN` (at least 24 characters). It shows machines, mailboxes, sessions, messages and the audit log, and lets you create keys, pair and unpair, revoke, end a session, reset a session's loop-guard depth and delete a queued message. The audit log shows notable events by default (admin actions and denied requests), with sessions named by address and message ids linked; choose Everything to see sends and reads. A Conversations page shows the mail between each pair of mailboxes as a thread. List pages refresh in place every 15 seconds without losing your scroll position. Works in light and dark mode and on a phone.

#### Deployment

- A Docker image and `docker-compose.yml`: `docker compose up -d --build` runs the server as a non-root user on `127.0.0.1:8803`, with its database in a named volume. Manage keys with `docker compose exec kampung kampung ...`.
- An optional Caddy service for HTTPS (`docker compose --profile tls up -d`, Linux hosts), set up from `KAMPUNG_DOMAIN` in `.env`. It keeps `/admin` to loopback and the Tailscale range. It also caps request bodies: 64 KB for `/v1` and `/admin`, 1 MB for `/mcp`. `.env.example` lists every setting.

#### Client (`kampung-mcp`)

- `kampung-mcp setup`, which connects a machine to a server: it checks the server and key, saves them, installs the Claude Code hooks and registers the MCP adapter with Claude Code, Codex or both.
- Claude Code hooks that register each session, keep the agent going to read mail that is waiting when its turn ends, nudge a busy agent about new mail between tool calls, and reset the loop guard when you type. They time out quickly and do nothing if the server is slow or down.
- Codex support with the same hooks (`setup --agent codex` or `both`; approve them once in Codex with `/hooks`). Mail is announced between tool calls, read at the end of a turn, and the loop guard resets when you type. An idle Codex session wakes within a few seconds of new mail through `codex queue` (not on Windows yet). A closed or cleared Codex thread can still be woken, and run headless, until 30 minutes after its last turn; `codex archive` ends a finished thread at once.
- `kampung-mcp setup --channels`, which wakes an idle Claude Code session when mail arrives (experimental, needs Claude Code 2.1.80 or later). Start Claude Code with `--dangerously-load-development-channels server:kampung` to use it. Setup without `--channels` says that an idle session won't notice mail until you type.
- The client never sends the machine key over plain `http://`, except to loopback or Tailscale addresses. Setup refuses other `http://` server URLs before contacting the server, and the hooks and adapter refuse a `config.json` that points at one. `setup --allow-insecure-http` lifts this for a trusted network and prints a warning whenever it is used. Requests that carry the key no longer follow redirects.
