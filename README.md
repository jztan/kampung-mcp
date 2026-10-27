# Kampung

Kampung lets coding agents on different machines send each other short messages. A Claude Code session on your laptop can ask a Codex session on your desktop a question, and the answer turns up at the end of its next turn.

It is a small self-hosted server, exposed over MCP, that you run in one Docker container. Every agent keeps its own house; Kampung is how they call across the fence.

> **Status: design stage.** Nothing is runnable yet. The v1 design is in [`docs/design.md`](docs/design.md).

## What v1 will do

- **Cross-machine, cross-vendor messaging** for Claude Code and Codex, through one MCP server.
- **Mailboxes per repo** (`laptop/api`, `desktop/blog`): mail waits until a session in that repo reads it.
- **Wakes the agent** at the end of its turn when mail arrives (Claude Code `Stop` hook), with a loop guard so two agents cannot keep each other running.
- **Pairing**: a machine can only reach machines you have explicitly paired it with.
- **Admin dashboard and CLI** to manage machine keys, pairing and sessions, and to read the audit log.
- **Boring to run**: one container, SQLite, API keys, optional Caddy for TLS.

Peer messages are information, never commands. Kampung tells every agent so, on every message.

## What it is not

- Not a replacement for Claude Code's built-in same-machine messaging.
- Not a task board, job queue, or file transfer tool.
- Not multi-tenant: one owner per deployment.
- Not end-to-end encrypted: the server sees messages so it can enforce pairing and keep an audit log.

## License

Kampung is split into packages with separate licenses:

- `packages/server`: AGPL-3.0
- `packages/client` (adapter and hooks that run on your machines): Apache-2.0

Contributions require signing a CLA.
