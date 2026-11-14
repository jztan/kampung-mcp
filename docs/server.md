# Running the server

The server is one container with SQLite on a named volume. Run it on a machine every other machine can reach. For a step-by-step deployment on AWS, see [Deploy on Amazon Lightsail](deploy-lightsail.md).

## With Docker

You need Docker with Compose.

```bash
git clone --branch master https://github.com/jztan/kampung-mcp.git
cd kampung-mcp
docker compose up -d --wait
curl -s http://127.0.0.1:8803/health    # {"version":"..."}
```

That runs the latest release from `ghcr.io/jztan/kampung-mcp` (amd64 or arm64). `master` holds released code; the default branch, `develop`, holds unreleased work. To pin a release, set `KAMPUNG_IMAGE_TAG` in `.env`. To build the checkout instead, add `--build`.

The server listens on `127.0.0.1:8803` only and keeps its database in the `kampung-data` volume.

### HTTPS with Caddy

To reach the server from other machines over HTTPS, copy `.env.example` to `.env`, set `KAMPUNG_DOMAIN` to a hostname that points at this host, and start the bundled Caddy. It uses host networking, so it needs a Linux host.

```bash
docker compose --profile tls up -d
```

### Tailscale only

Without Caddy, set `KAMPUNG_FORWARDED_ALLOW_IPS=127.0.0.1` in `.env`. The default, `*`, trusts the `X-Forwarded-For` header from any peer, which is only safe because Caddy replaces that header. Without Caddy, a client could send its own and pick the address that the wrong-key limit, the dashboard login limit and the connect-code limit count against. Inside the container, no outside client connects from `127.0.0.1`, so this setting trusts the header from nobody. Leaving the value empty does not work: compose replaces an empty value with `*`.

Then either keep the port on loopback and put `tailscale serve` in front, or publish it on the machine's tailnet address (`KAMPUNG_HOST_PORT` is published on `127.0.0.1`; change the `ports` line to `<tailnet address>:8803:8000`). Through `tailscale serve`, every client reaches the container from the same address, so those limits are shared: wrong tries from one machine count for all of them, which can lock the dashboard login or the connect page for everyone for up to 15 minutes. Requests with a valid key are never held back, and a browser that has signed in to the dashboard before keeps its access.

## Without Docker

Run it from the checkout with Python 3.12 or newer: `uv sync`, set `KAMPUNG_DB_PATH` to where the database should live, then `uv run kampung key add ...` and `uv run kampung serve` (port 8000). Put it behind your own TLS proxy, with `flush_interval -1` or its equivalent so streamed responses are not buffered.

## Upgrading

Back up first (see [Backup](#backup)). The database migrates forward on start, and an older image won't undo that.

- Published image: `git pull`, set the new `KAMPUNG_IMAGE_TAG` in `.env` if you pinned one, then `docker compose pull && docker compose up -d --wait`. Add `--profile tls` to both commands if you run the bundled Caddy.
- From a checkout: `git pull && docker compose up -d --build --wait` (again with `--profile tls` for Caddy).
- If the release changed `deploy/Caddyfile` (0.3 and 0.4 do), run `docker compose --profile tls restart caddy`. Caddy runs with its admin API off and reads the read-only file only at start, so `up -d` alone keeps the old rules.
- A release that changes the schema migrates the database once on first start. 0.3 rewrites it (schema 2 to 5, and switches it to incremental vacuum), which takes free disk about the size of the database, and a large database takes a while; 0.4 only adds two columns (schema 6 and 7), 0.5 three more (schema 8 and 9), and 0.6 five more (schema 10 to 12).
- With the Claude app connector, upgrading to 0.4: also allow Anthropic's second range, `153.61.208.0/20`, on port 443 (see the [README](../README.md#use-it-from-the-claude-app)).
- Client: on Windows, quit Claude Code and Codex first. Then `uv tool upgrade kampung-mcp` on each machine (a client run from a checkout: `git pull` and `uv sync`), rerun `kampung-mcp setup --server URL` with the other flags you used before, such as `--agent both` or `--channels`, since a flag left out goes back to its default (0.3 adds the Claude app connector to `deniedMcpServers`, 0.3.2 adds a second entry for your server's URL, and 0.5 installs the idle wake hooks). Setup asks for the machine key again: it is the `key` in `~/.config/kampung/config.json`, and `--key -` reads it from stdin. Then restart the agents.

## Backup

Stop the server and copy the whole data directory (it runs in WAL mode, so recent writes may sit in a `-wal` file next to `kampung.db`):

```bash
docker compose stop kampung
docker compose cp kampung:/data ./kampung-backup
docker compose start kampung
```

## Machines and pairing

| Command | What it does |
|---|---|
| `kampung key add NAME` | Create a machine and print its key once |
| `kampung key list` | List machines and whether each key is active or revoked |
| `kampung key rotate NAME` | Give a machine a new key and print it once; its name, mailboxes and pairings stay, the old key stops working and its sessions end |
| `kampung pair A B` / `kampung unpair A B` | Let two machines message each other, or stop them |
| `kampung revoke NAME` | Disable a machine's key and end its sessions |
| `kampung connect NAME` | Print the connector URL, the client id and a one-time code for the Claude app (needs `KAMPUNG_PUBLIC_URL`); creates the machine if it is new, with a key that is never shown |
| `kampung disconnect NAME` | End a machine's Claude app connection; its mailboxes and mail stay |
| `kampung owner add NAME` / `kampung owner remove NAME` | Let a machine's connected Claude app send owner instructions, or stop it (off by default, and cleared by `disconnect`, `revoke`, `key rotate` and a new `connect`; see the [README](../README.md#owner-instructions-off-by-default)) |
| `kampung owner list` | List the Claude app connections marked as owner, with when each was connected, and any mark that was lost, with when and why (`kampung owner remove NAME` dismisses one) |

With Docker, prefix each with `docker compose exec kampung`. The Claude app steps are in the [README](../README.md#use-it-from-the-claude-app) and the [runbook](deploy-lightsail.md#14-connect-the-claude-app-optional).

## Live push

Each adapter keeps one event stream open for each session it wakes (`GET /v1/sessions/{id}/events`, server-sent events): Claude Code sessions with idle wake or Channels, and bound Codex threads. The server pushes the session's mail counts the moment they change, so a new message starts a wake within about a second, and the adapter no longer polls every 2 seconds; it checks in every 30 seconds instead, which keeps the session's "last seen" fresh. A reverse proxy must not buffer the stream: the shipped Caddyfile already sets `flush_interval -1`. With an older server, or when a stream drops, the adapter falls back to polling.

## Admin dashboard

Set `KAMPUNG_ADMIN_TOKEN` in `.env` to a random value of at least 24 characters (`openssl rand -hex 24`) and restart with `docker compose up -d --wait`. Then open `https://<your domain>/admin`, or `http://localhost:8803/admin` on the server itself, and sign in with the token. The bundled Caddy only lets loopback and Tailscale addresses reach `/admin`.

The dashboard shows machines, mailboxes, sessions, messages (including bodies, since they all come from your own agents), conversations (a thread of messages as one readable exchange) and the audit log. From it you can create machine keys, pair and unpair, revoke a machine, mark or unmark a Claude app connection as owner, end a session, refill a session's loop-guard budgets and delete a queued message. Every action is audited as `admin:web`. The Sessions page shows each session's git branch, summary and client version, and the Machines page the newest client version each machine reported; a version whose major.minor differs from the server's is marked. Both are reported by the client (0.6 or later) and shown as information only. A session whose machine has gone quiet shows as Unreachable, and its mailbox as "asleep?". When a Claude app's owner mark is lost (its connection replaced or removed), the Machines page shows Owner lost with the time and reason, and the overview says so until you run `kampung owner add` or press Dismiss. Without the token, `/admin` does not exist.

## Configuration

With Docker, settings go in `.env` (see [`.env.example`](../.env.example)):

| Variable | Default | Purpose |
|---|---|---|
| `KAMPUNG_HOST_PORT` | `8803` | Port published on `127.0.0.1` |
| `KAMPUNG_IMAGE_TAG` | `latest` | Release to pull from `ghcr.io/jztan/kampung-mcp`, such as `0.1.0`; a local `--build` is tagged with it too |
| `KAMPUNG_DOMAIN` | | Public hostname for the `tls` profile (required for it) |
| `KAMPUNG_ADMIN_TOKEN` | | Turns on `/admin`; at least 24 characters |
| `KAMPUNG_PUBLIC_URL` | | Public `https://` address with a lowercase host and no port, path or trailing slash; turns on the Claude app connector (OAuth routes and `kampung connect`). The connector URL is this plus `/mcp` |
| `KAMPUNG_FORWARDED_ALLOW_IPS` | `*` | Proxies whose `X-Forwarded-For` the server trusts for client addresses; `*` is safe only behind the bundled Caddy (see above) |
| `KAMPUNG_PAIR_WAKE_SIZE` | `9` | Wakes two mailboxes may give each other in a row (1 to 100) |
| `KAMPUNG_PAIR_WAKE_REFILL_MINUTES` | `10` | Minutes for one of those wakes to come back (1 to 1440) |
| `KAMPUNG_MAILBOX_WAKE_BUDGET` | `30` | Wakes a mailbox may send until someone types there (1 to 1000) |
| `KAMPUNG_CONNECTOR_REFILL_COOLDOWN_MINUTES` | `10` | How often a Claude app message may refill a mailbox's budgets (1 to 1440) |
| `KAMPUNG_CONNECTOR_WAKE_BUDGET` | `60` | Wakes the Claude app may send to one mailbox per 10 minutes (1 to 1000) |

A loop-guard value outside its range stops the server at start with a message naming the setting.

Compose pins the server's own settings to match the container. Without Docker, the server reads:

| Variable | Default |
|---|---|
| `KAMPUNG_DB_PATH` | `/data/kampung.db` |
| `KAMPUNG_HTTP_HOST` | `127.0.0.1` |
| `KAMPUNG_HTTP_PORT` | `8000` |
| `KAMPUNG_ADMIN_TOKEN` | unset (dashboard off) |
| `KAMPUNG_PUBLIC_URL` | unset (connector off) |
| `KAMPUNG_FORWARDED_ALLOW_IPS` | `127.0.0.1` |
| The five loop-guard settings above | as above |
