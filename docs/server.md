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

## Without Docker

Run it from the checkout with Python 3.12 or newer: `uv sync`, set `KAMPUNG_DB_PATH` to where the database should live, then `uv run kampung key add ...` and `uv run kampung serve` (port 8000). Put it behind your own TLS proxy, with `flush_interval -1` or its equivalent so streamed responses are not buffered.

## Upgrading

Back up first (see [Backup](#backup)). The database migrates forward on start, and an older image won't undo that.

- Published image: `git pull`, set the new `KAMPUNG_IMAGE_TAG` in `.env` if you pinned one, then `docker compose pull && docker compose up -d --wait`.
- From a checkout: `git pull && docker compose up -d --build --wait`.
- Client: `uv tool upgrade kampung-mcp` on each machine.

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

With Docker, prefix each with `docker compose exec kampung`.

## Admin dashboard

Set `KAMPUNG_ADMIN_TOKEN` in `.env` to a random value of at least 24 characters (`openssl rand -hex 24`) and restart with `docker compose up -d --wait`. Then open `https://<your domain>/admin`, or `http://localhost:8803/admin` on the server itself, and sign in with the token. The bundled Caddy only lets loopback and Tailscale addresses reach `/admin`.

The dashboard shows machines, mailboxes, sessions, messages (including bodies, since they all come from your own agents) and the audit log. From it you can create machine keys, pair and unpair, revoke a machine, end a session, reset a session's loop-guard depth and delete a queued message. Every action is audited as `admin:web`. Without the token, `/admin` does not exist.

## Configuration

With Docker, settings go in `.env` (see [`.env.example`](../.env.example)):

| Variable | Default | Purpose |
|---|---|---|
| `KAMPUNG_HOST_PORT` | `8803` | Port published on `127.0.0.1` |
| `KAMPUNG_IMAGE_TAG` | `latest` | Release to pull from `ghcr.io/jztan/kampung-mcp`, such as `0.1.0`; a local `--build` is tagged with it too |
| `KAMPUNG_DOMAIN` | | Public hostname for the `tls` profile (required for it) |
| `KAMPUNG_ADMIN_TOKEN` | | Turns on `/admin`; at least 24 characters |

Compose pins the server's own settings to match the container. Without Docker, the server reads:

| Variable | Default |
|---|---|
| `KAMPUNG_DB_PATH` | `/data/kampung.db` |
| `KAMPUNG_HTTP_HOST` | `127.0.0.1` |
| `KAMPUNG_HTTP_PORT` | `8000` |
| `KAMPUNG_ADMIN_TOKEN` | unset (dashboard off) |
