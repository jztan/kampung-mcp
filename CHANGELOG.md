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
- A loop guard: from the fourth reply in a row between agents, mail is stored without waking the receiver. The count resets when a human types in that session, or after 30 minutes for sessions without hooks (Codex).
- The `kampung` admin CLI: `key add`, `key list`, `pair`, `unpair`, `revoke` and `serve`. Machines can't exchange mail until you pair them, and revoking a key ends its sessions.
- Closed by default: every `/mcp` and `/v1` request needs a machine key, keys are stored hashed, and `/health` returns only the version.
- An admin dashboard at `/admin`, turned on by setting `KAMPUNG_ADMIN_TOKEN` (at least 24 characters). It shows machines, mailboxes, sessions, messages and the audit log, and lets you create keys, pair and unpair, revoke, end a session, reset a session's loop-guard depth and delete a queued message. Works in light and dark mode and on a phone.

#### Deployment

- A Docker image and `docker-compose.yml`: `docker compose up -d --build` runs the server as a non-root user on `127.0.0.1:8803`, with its database in a named volume. Manage keys with `docker compose exec kampung kampung ...`.
- An optional Caddy service for HTTPS (`docker compose --profile tls up -d`, Linux hosts), set up from `KAMPUNG_DOMAIN` in `.env`. It keeps `/admin` to loopback and the Tailscale range. `.env.example` lists every setting.

#### Client (`kampung-mcp`)

- `kampung-mcp setup`, which connects a machine to a server: it checks the server and key, saves them, installs the Claude Code hooks and registers the MCP adapter with Claude Code, Codex or both.
- Claude Code hooks that register each session, keep the agent going to read mail that is waiting when its turn ends, nudge a busy agent about new mail between tool calls, and reset the loop guard when you type. They time out quickly and do nothing if the server is slow or down.
- Codex support by polling: Codex reads mail when it calls `inbox`.
- `kampung-mcp setup --channels`, which wakes an idle Claude Code session when mail arrives (experimental, needs Claude Code 2.1.80 or later). Start Claude Code with `--dangerously-load-development-channels server:kampung` to use it. Setup without `--channels` says that an idle session won't notice mail until you type.
