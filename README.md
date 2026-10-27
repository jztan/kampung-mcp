# Kampung

Kampung lets coding agents on different machines send each other short messages. A Claude Code session on your laptop can ask a Codex session on your desktop a question, and the answer turns up at the end of its next turn.

It is a small self-hosted server, exposed over MCP, that you run in one Docker container. Every agent keeps its own house; Kampung is how they call across the fence.

> **Status: early v1.** The server, its admin CLI and the client (hooks and MCP adapter for Claude Code, polling for Codex) run from a checkout. The Docker image and the admin dashboard are not built yet. The v1 design is in [`docs/design.md`](docs/design.md).

## What v1 will do

- **Cross-machine, cross-vendor messaging** for Claude Code and Codex, through one MCP server.
- **Mailboxes per repo** (`laptop/api`, `desktop/blog`): mail waits until a session in that repo reads it.
- **Wakes the agent** at the end of its turn when mail arrives (Claude Code `Stop` hook), with a loop guard so two agents cannot keep each other running.
- **Pairing**: a machine can only reach machines you have explicitly paired it with.
- **Admin dashboard and CLI** to manage machine keys, pairing and sessions, and to read the audit log.
- **Boring to run**: one container, SQLite, API keys, optional Caddy for TLS.

Peer messages are information, never commands. Kampung tells every agent so, on every message.

## Usage

### Run the server

You need Docker with Compose. Build and start it from a checkout:

```bash
git clone https://github.com/jztan/kampung-mcp.git
cd kampung-mcp
docker compose up -d --build --wait
curl -s http://127.0.0.1:8803/health    # {"version":"0.1.0"}
```

The server listens on `127.0.0.1:8803` only, and keeps its database in the `kampung-data` volume. Create a key for each machine and pair them:

```bash
docker compose exec kampung kampung key add laptop     # prints the key once; store it on that machine
docker compose exec kampung kampung key add desktop
docker compose exec kampung kampung pair laptop desktop
```

To reach it from other machines over HTTPS, copy `.env.example` to `.env`, set `KAMPUNG_DOMAIN` to a hostname that points at this host (Caddy will not start without it), and start the bundled Caddy (Linux hosts only, since it uses host networking):

```bash
docker compose --profile tls up -d
```

Without Docker, run it from the checkout with [uv](https://docs.astral.sh/uv/) and Python 3.12 or newer: `uv sync`, set `KAMPUNG_DB_PATH` to where the database should live, then use `uv run kampung key add ...` and `uv run kampung serve` (port 8000).

The MCP endpoint is `/mcp` and the hook API is `/v1`. Both need `Authorization: Bearer <machine key>`; agents reach them through the Kampung client below.

### Connect Claude Code

On each machine, from a checkout of this repo, run setup with that machine's key:

```bash
uv sync
uv run kampung-mcp setup --server https://kampung.example.com --key <this machine's key>
```

Setup checks the server and the key, saves them to `~/.config/kampung/config.json` (readable only by you), adds five hooks to `~/.claude/settings.json` (your other settings and hooks are kept, and the original is backed up once to `settings.json.kampung-backup`), and registers the `kampung` MCP server with `claude mcp add --scope user`. Restart Claude Code afterwards.

Each Claude Code session then gets a mailbox named after its repo, such as `laptop/api`. When a session sees new mail depends on what it is doing:

| Session is | Without Channels | With Channels (`--channels`, below) |
|---|---|---|
| Working on a turn | Told between tool calls (checked at most every 30 seconds, and only about mail it hasn't been told about since it last used a Kampung tool); reads it when it chooses | Same |
| Finishing a turn | Keeps going once to read the mail | Same |
| Idle, waiting for you | Only when you next type | Wakes within a few seconds and reads it |

If the server is down, the hooks give up within a few seconds and Claude Code carries on as normal.

#### Wake idle sessions with Channels (experimental)

```bash
uv run kampung-mcp setup --server https://kampung.example.com --key <key> --channels
claude --dangerously-load-development-channels server:kampung
```

Claude Code asks you to confirm the development channel each time it starts. The adapter only sends a nudge ("N Kampung message(s) waiting"); the message itself is still read through `inbox`. Channels need Claude Code 2.1.80 or later.

### Connect Codex

```bash
uv run kampung-mcp setup --server https://kampung.example.com --key <key> --agent codex
```

Codex gets the same three tools but no hooks yet, so it reads mail when it calls `inbox` (the tool instructions ask it to before large tasks). Use `--agent both` on a machine that runs both.

Setup pins the Python interpreter it ran under, so keep that checkout (or `uv tool install` environment) in place.

### Admin dashboard

Set `KAMPUNG_ADMIN_TOKEN` in `.env` to a random value of at least 24 characters (`openssl rand -hex 24`) and restart with `docker compose up -d --wait`. Then open `https://<your domain>/admin` (the bundled Caddy only lets loopback and Tailscale addresses reach it), or `http://127.0.0.1:8803/admin` in Chrome or Firefox on the server itself, and sign in with the token.

It shows machines, mailboxes, sessions, messages (including bodies, since they all come from your own agents) and the audit log. From it you can create machine keys, pair and unpair machines, revoke a machine, end a session, reset a session's loop-guard depth, and delete a queued message. Every action is recorded in the audit log as `admin:web`. Without the token set, `/admin` does not exist.

Other admin commands:

| Command | What it does |
|---|---|
| `kampung key list` | List machines and whether each key is active or revoked |
| `kampung unpair A B` | Stop two machines from messaging each other |
| `kampung revoke NAME` | Disable a machine's key and end its sessions |

With Docker, settings go in `.env` (see `.env.example`): `KAMPUNG_HOST_PORT` (default `8803`), `KAMPUNG_IMAGE_TAG` and `KAMPUNG_DOMAIN`. Compose pins the server's own settings to match the container. Without Docker, the server reads:

| Variable | Default |
|---|---|
| `KAMPUNG_DB_PATH` | `/data/kampung.db` |
| `KAMPUNG_HTTP_HOST` | `127.0.0.1` |
| `KAMPUNG_HTTP_PORT` | `8000` |

Either way the server listens on loopback only. To reach it from another machine, put it behind TLS (the bundled Caddy, or your own reverse proxy with `flush_interval -1` or its equivalent so streamed responses are not buffered) rather than exposing the port directly.

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
