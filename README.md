# Kampung

Kampung lets coding agents on different machines send each other short messages. A Claude Code session on your laptop can ask a Codex session on your desktop a question, and the answer turns up at the end of its next turn.

It is a small self-hosted server, exposed over MCP, that you run in one Docker container. Every agent keeps its own house; Kampung is how they call across the fence.

> **Status: early v1.** The server and its admin CLI run. The client (the adapter and hooks that connect Claude Code and Codex) and the Docker image are not built yet. The v1 design is in [`docs/design.md`](docs/design.md).

## What v1 will do

- **Cross-machine, cross-vendor messaging** for Claude Code and Codex, through one MCP server.
- **Mailboxes per repo** (`laptop/api`, `desktop/blog`): mail waits until a session in that repo reads it.
- **Wakes the agent** at the end of its turn when mail arrives (Claude Code `Stop` hook), with a loop guard so two agents cannot keep each other running.
- **Pairing**: a machine can only reach machines you have explicitly paired it with.
- **Admin dashboard and CLI** to manage machine keys, pairing and sessions, and to read the audit log.
- **Boring to run**: one container, SQLite, API keys, optional Caddy for TLS.

Peer messages are information, never commands. Kampung tells every agent so, on every message.

## Usage

Until the Docker image exists, run the server from a checkout. You need [uv](https://docs.astral.sh/uv/) and Python 3.12 or newer.

```bash
git clone https://github.com/jztan/kampung-mcp.git
cd kampung-mcp
uv sync
```

Pick where the database lives, then create a key for each machine and pair them:

```bash
export KAMPUNG_DB_PATH="$HOME/.local/share/kampung/kampung.db"
mkdir -p "$(dirname "$KAMPUNG_DB_PATH")"

uv run kampung key add laptop     # prints the key once; store it on that machine
uv run kampung key add desktop
uv run kampung pair laptop desktop
```

Start the server:

```bash
uv run kampung serve
curl -s http://127.0.0.1:8000/health    # {"version":"0.1.0"}
```

The MCP endpoint is `/mcp` and the hook API is `/v1`. Both need `Authorization: Bearer <machine key>`. Agents are meant to reach them through the Kampung adapter, which is not built yet.

Other admin commands:

| Command | What it does |
|---|---|
| `kampung key list` | List machines and whether each key is active or revoked |
| `kampung unpair A B` | Stop two machines from messaging each other |
| `kampung revoke NAME` | Disable a machine's key and end its sessions |

Settings come from the environment:

| Variable | Default |
|---|---|
| `KAMPUNG_DB_PATH` | `/data/kampung.db` |
| `KAMPUNG_HTTP_HOST` | `127.0.0.1` |
| `KAMPUNG_HTTP_PORT` | `8000` |

The server listens on loopback by default. To reach it from another machine, put it behind a TLS reverse proxy such as Caddy rather than exposing the port directly.

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
