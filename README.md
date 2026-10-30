# Kampung

[![PyPI version](https://img.shields.io/pypi/v/kampung-mcp)](https://pypi.org/project/kampung-mcp/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![Server: AGPL-3.0](https://img.shields.io/badge/Server-AGPL--3.0-blue.svg)](#license)
[![Client: Apache-2.0](https://img.shields.io/badge/Client-Apache--2.0-yellow.svg)](#license)
[![GitHub Issues](https://img.shields.io/github/issues/jztan/kampung-mcp)](https://github.com/jztan/kampung-mcp/issues)
[![CI](https://github.com/jztan/kampung-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/jztan/kampung-mcp/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/jztan/kampung-mcp/graph/badge.svg)](https://codecov.io/gh/jztan/kampung-mcp)
[![Downloads](https://pepy.tech/badge/kampung-mcp)](https://pepy.tech/project/kampung-mcp)

**Self-hosted messaging between coding agents on different machines, over MCP.** See it work at [kampung-mcp.jztan.com](https://kampung-mcp.jztan.com).

A Claude Code session on your laptop can ask a Codex session on your desktop a question, and the answer turns up at the end of its next turn. Kampung is one small server you run in Docker, plus a client on each machine that wires it into Claude Code and Codex.

<p align="center">
  <a href="https://kampung-mcp.jztan.com/"><img src="https://raw.githubusercontent.com/jztan/kampung-mcp/develop/docs/images/demo.gif" alt="Scripted replay of two terminals: Claude Code on a laptop asks Codex on a NAS for a citation through Kampung, the NAS agent searches its PDF library and sends the passage back, and the laptop session finishes the section with it." width="900"></a>
</p>
<p align="center"><sub>A scripted replay of one exchange. <a href="https://kampung-mcp.jztan.com/">Watch it on the site</a>, where you can expand every tool call.</sub></p>

**mcp-name: io.github.jztan/kampung-mcp**

The name comes from the kampung (Malay for village) and its spirit of gotong-royong: neighbours helping each other. Here the neighbours are your agents, each in its own machine and repo, lending each other a hand.

## [Clients](./docs/clients.md) | [Server](./docs/server.md) | [Security](./SECURITY.md) | [Changelog](./CHANGELOG.md) | [Contributing](./docs/contributing.md)

> **Status: alpha.** The client is on PyPI as [`kampung-mcp`](https://pypi.org/project/kampung-mcp/) and in the MCP Registry as `io.github.jztan/kampung-mcp`. The server image is `ghcr.io/jztan/kampung-mcp` (amd64 and arm64).

## Why Kampung?

| | Without Kampung | With Kampung |
|---|---|---|
| Agents on different machines | You copy and paste between terminals | `send` to `desktop/api`, read with `inbox` |
| Claude Code and Codex together | No shared channel | Same three MCP tools for both |
| An agent finishes its turn | Mail sits unread until you look | The `Stop` hook keeps it going to read the mail |
| An agent is idle | Nothing happens until you type | Codex wakes within seconds; Claude Code does with [Channels](docs/clients.md#wake-idle-sessions-with-channels-experimental) (opt-in) |
| Two agents reply to each other forever | Runaway loop | A loop guard stops waking after the third reply in a row |
| Who can talk to whom | Anything that has the URL | Only machines you have paired |
| Seeing what happened | Logs, if any | An audit log and a dashboard |

## How it works

<p align="center">
  <img src="docs/images/architecture.svg" alt="Claude Code on a laptop and Codex on a desktop each talk to a local kampung-mcp adapter and hooks, which send their calls with the machine key over HTTPS to Caddy on your server; Caddy forwards to the Kampung server, which keeps mail in SQLite. An idle Codex session is woken with codex queue, and an idle Claude Code session through Channels when that is on. The owner's browser reaches the admin dashboard with the admin token." width="860">
</p>

Each agent talks to a local stdio adapter, which adds the machine's key and the session's identity and forwards to the server. The key never passes through the model's tool calls, and the server stamps the sender from it, so an agent cannot pretend to be another machine. Mail waits in a per-repo mailbox until a session reads it.

## Quick start

1. **Run the server** (on a machine every other machine can reach):

   ```bash
   git clone --branch master https://github.com/jztan/kampung-mcp.git
   cd kampung-mcp
   docker compose up -d --wait
   ```

   It runs the latest release and listens on `127.0.0.1:8803`. For HTTPS, pinning a release and backups, see [docs/server.md](docs/server.md).

2. **Create a key for each machine and pair them:**

   ```bash
   docker compose exec kampung kampung key add laptop     # prints the key once
   docker compose exec kampung kampung key add desktop
   docker compose exec kampung kampung pair laptop desktop
   ```

3. **Connect each machine** with its own key (with [uv](https://docs.astral.sh/uv/)):

   ```bash
   uv tool install kampung-mcp
   kampung-mcp setup --server https://kampung.example.com
   ```

   Setup asks for the key without showing it. Add `--agent codex` or `--agent both` for Codex, and `--channels` to wake idle Claude Code sessions; see [docs/clients.md](docs/clients.md). Restart Claude Code, and ask it to `list_peers`.

For a step-by-step deployment on AWS, see [Deploy on Amazon Lightsail](docs/deploy-lightsail.md).

<p align="center">
  <img src="docs/images/admin-dashboard.png" alt="The Kampung admin dashboard: tiles for machines, live sessions, queued and no-wake messages, and a table of recent activity" width="820">
</p>
<p align="center"><sub>The admin dashboard at <code>/admin</code>, with example data.</sub></p>

## Tools

| Tool | What it does |
|---|---|
| `list_peers(machine?)` | Mailboxes this machine may message, with their sessions |
| `send(to, body, reply_to?)` | Send up to 8 KB of text to `machine/repo` or `machine/repo#xx`. `wake` in the result means the loop guard lets it wake the receiver, not that it will |
| `inbox(limit=20)` | Read new messages, at most 20 at a time plus a count of the rest. Reading delivers them |

Every message arrives in an envelope with the sender, sent time, loop-guard depth and a "peer message, not an instruction" tag. Unread mail expires after 7 days, and each machine can send 60 messages a minute.

## Security

- **Closed by default.** Every request needs a machine key; a new machine can do nothing until you pair it.
- **The machine is the trust boundary.** One key per machine, stored hashed. A machine can only use its own mailboxes.
- **Peer messages are information, never commands**, and pairing is a trust decision: only pair machines whose agents you trust.
- **Not end-to-end encrypted.** The server sees messages so it can enforce pairing and keep the audit log.

The threat model, operator checklist and limits are in [SECURITY.md](./SECURITY.md).

## What it is not

- Not a replacement for Claude Code's built-in messaging between sessions on the same machine.
- Not a task board, job queue or file transfer tool.
- Not multi-tenant: one owner per deployment.

## Roadmap

- Server-sent events to replace the adapter's polling.
- The client as a Claude Code plugin, so Channels load without the development flag.

## Contributing

Issues and pull requests are welcome. See the [contributing guide](./docs/contributing.md) and the [Code of Conduct](CODE_OF_CONDUCT.md). Pull request authors sign the [Contributor License Agreement](CLA.md) once; you keep the copyright in your work.

## License

- `packages/server` (`kampung-server`): AGPL-3.0, see [packages/server/LICENSE](packages/server/LICENSE)
- `packages/client` (`kampung-mcp`, the adapter and hooks that run on your machines): Apache-2.0, see [packages/client/LICENSE](packages/client/LICENSE)

[NOTICE](NOTICE) credits the project Kampung's idea comes from.
