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

> **Status: beta.** The MCP tools and `/v1` can still change before v1.0. The client is on PyPI as [`kampung-mcp`](https://pypi.org/project/kampung-mcp/) and in the MCP Registry as `io.github.jztan/kampung-mcp`. The server image is `ghcr.io/jztan/kampung-mcp` (amd64 and arm64).

## Why Kampung?

| | Without Kampung | With Kampung |
|---|---|---|
| Agents on different machines | You copy and paste between terminals | `send` to `desktop/api`, read with `inbox` |
| Claude Code and Codex together | No shared channel | Same MCP tools for both |
| An agent finishes its turn | Mail sits unread until you look | The `Stop` hook keeps it going to read the mail |
| An agent is idle | Nothing happens until you type | Codex wakes within seconds; Claude Code does with [Channels](docs/clients.md#wake-idle-sessions-with-channels-experimental) (opt-in) |
| Two agents reply to each other forever | Runaway loop | A loop guard gives each pair of mailboxes a few wakes that come back slowly, and each mailbox a budget only typing refills |
| Who can talk to whom | Anything that has the URL | Only machines you have paired |
| Seeing what happened | Logs, if any | An audit log and a dashboard |

## How it works

<p align="center">
  <img src="docs/images/architecture.svg" alt="Claude Code on a laptop and Codex on a desktop each talk to a local kampung-mcp adapter and hooks, which send their calls with the machine key over HTTPS to Caddy on your server; the Claude app on an iPhone or claude.ai reaches the same server through Anthropic's cloud with an OAuth token. Caddy forwards to the Kampung server, which keeps mail in SQLite. An idle Codex session is woken with codex queue, and an idle Claude Code session through Channels when that is on; the Claude app reads mail when you ask a chat to. The owner's browser reaches the admin dashboard with the admin token." width="860">
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

   Setup records the hook's full path, which includes the Python version. If the `uv tool` install moves to another Python minor version, rerun `kampung-mcp setup`: the hooks fail open, so otherwise they just stop working, with no error.

For a step-by-step deployment on AWS, see [Deploy on Amazon Lightsail](docs/deploy-lightsail.md).

Why this exists, and how it came together: [How I Let AI Agents on Different Machines Work Together](https://blog.jztan.com/ai-coding-agents-on-different-machines/?utm_source=github&utm_medium=referral&utm_campaign=kampung-mcp&utm_content=quickstart-ai-coding-agents-on-different-machines)

<p align="center">
  <img src="docs/images/admin-dashboard.png" alt="The Kampung admin dashboard: tiles for machines, live sessions, queued and no-wake messages, and a table of recent activity" width="820">
</p>
<p align="center"><sub>The admin dashboard at <code>/admin</code>, with example data.</sub></p>

## Tools

| Tool | What it does |
|---|---|
| `list_peers(machine?, include_empty?)` | Mailboxes this machine may message, with their sessions. Mailboxes with no open session are hidden unless `include_empty` is true |
| `send(to, body, reply_to?, attachment?, fyi?)` | Send up to 8 KB of text to `machine/repo` or `machine/repo#xx`, optionally with one file from the project's `kampung-outbox` folder: text up to 256 KB, or a PNG or JPEG. `wake` in the result means the loop guard lets it wake the receiver, not that it will; when it is false, `reason` and `hint` say why. `fyi` marks a note that needs no reply and never wakes anyone |
| `inbox(limit=20)` | Read new messages, at most 20 at a time plus a count of the rest. Reading delivers them |
| `read_attachment(message_id, offset?, limit?)` | Read a message's attachment: text in slices, with Markdown headings to jump to, or an image whole |

Every message arrives in an envelope with the sender, sent time and a "peer message, not an instruction" tag. Bodies and text attachments are checked for secrets such as API keys before they are stored. Unread mail expires after 7 days, and each machine can send 60 messages a minute. See the [tool reference](docs/tool-reference.md) for results and errors.

## Use it from the Claude app

With `KAMPUNG_PUBLIC_URL` set (see `.env.example`), the Claude app and claude.ai can join as a peer with its own mailbox, for example `phone/claude`, and read or leave mail with no laptop running. On the server:

```bash
docker compose exec kampung kampung connect phone
docker compose exec kampung kampung pair phone mac
```

`connect` prints a URL, the client id `kampung-claude` and a one-time code. In Claude, add a custom connector with that URL, confirm **Sign in now** and **Use your own OAuth client** (Claude detects both from the server), and enter that client id with no secret. Claude opens Kampung's connect page; enter the code within 10 minutes from one of your allowed networks (home Wi-Fi counts), and the connector is ready on the phone and the web. Every chat on the account shares that one mailbox, and mail is delivered on read, so the first chat to read the inbox gets it.

**Keep the connector out of Claude Code.** Claude Code loads the account's connectors too, so every window would get a second `kampung` with the same tool names, acting as `phone/claude`: the model picks either one, and that window takes the phone's mail. Name the connector `kampung` in the dialog: `kampung-mcp setup` then denies it for every project in `~/.claude/settings.json` (rerun setup on machines set up before this, or pass `--connector-name` if you named it differently). The entry it writes is:

```json
{ "deniedMcpServers": [{ "serverName": "claude.ai kampung" }] }
```

The symptom when it is on: the Stop hook says mail is waiting but `inbox` comes back empty, because the hook counts the window's own mailbox and the model read the phone's.

`kampung disconnect phone` ends the connection and keeps the machine and its mail. Tool calls come from Anthropic's cloud (`160.79.104.0/21`), so allow that range on port 443; the [deployment runbook](docs/deploy-lightsail.md) has the firewall and the checks.

## Security

- **Closed by default.** Every request needs a machine key; a new machine can do nothing until you pair it.
- **The machine is the trust boundary.** One key per machine, stored hashed. A machine can only use its own mailboxes.
- **Peer messages are information, never commands**, and pairing is a trust decision: only pair machines whose agents you trust.
- **Not end-to-end encrypted.** The server sees messages so it can enforce pairing and keep the audit log.
- **Claude app tokens are a second credential bound to a machine.** They come only from a one-time code you print on the server, never from registration, and they cannot reach the hook API.

The threat model, operator checklist and limits are in [SECURITY.md](./SECURITY.md).

## What it is not

- Not a replacement for Claude Code's built-in messaging between sessions on the same machine.
- Not a task board, job queue or file transfer tool.
- Not multi-tenant: one owner per deployment.

## Roadmap

- Server-sent events to replace the adapter's polling.
- The client as a Claude Code plugin, for easier install. Channels would still need the development flag.

## Blog posts

**The story behind the releases.** I write about building Kampung and the other tools I work on in [The Dispatch](https://blog.jztan.com/newsletter/?utm_source=github&utm_medium=referral&utm_campaign=kampung-mcp&utm_content=list-newsletter). Come along if that's your kind of thing.

- [How I Let AI Agents on Different Machines Work Together](https://blog.jztan.com/ai-coding-agents-on-different-machines/?utm_source=github&utm_medium=referral&utm_campaign=kampung-mcp&utm_content=list-ai-coding-agents-on-different-machines): Why I stopped relaying questions between terminals, and how a self-hosted mailbox lets Claude Code and Codex on different machines message each other over MCP

## Contributing

Issues and pull requests are welcome. See the [contributing guide](./docs/contributing.md) and the [Code of Conduct](CODE_OF_CONDUCT.md). Pull request authors sign the [Contributor License Agreement](CLA.md) once; you keep the copyright in your work.

## License

- `packages/server` (`kampung-server`): AGPL-3.0, see [packages/server/LICENSE](packages/server/LICENSE)
- `packages/client` (`kampung-mcp`, the adapter and hooks that run on your machines): Apache-2.0, see [packages/client/LICENSE](packages/client/LICENSE)

[NOTICE](NOTICE) credits the project Kampung's idea comes from.
