# Security

## Reporting a vulnerability

Please don't open a public issue. Use **Report a vulnerability** on the repository's Security tab, with the version (`/health` returns it), what you did and what happened.

## Deployment model

Kampung is built for one owner per server: one container, one SQLite database, one set of machine keys. Two people means two containers. There is no tenant boundary inside a server, so don't share one.

<p align="center">
  <img src="docs/images/security-model.svg" alt="Each machine is a trust boundary holding one machine key in config.json. Calls go with the key over HTTPS or Tailscale to Caddy, which terminates TLS, caps request bodies and keeps /admin to loopback and the tailnet. A client with no key or a wrong key is rejected at the first check before its body is read. Inside the Kampung server every request passes six checks in order: machine key, ownership, pairing, limits, loop guard and the envelope that marks peer text as information only. Mail, hashed keys and the audit log are kept in SQLite on a volume." width="860">
</p>

- **Closed by default.** Every `/mcp` and `/v1` request needs a machine key. `/health` is open and returns only the version. `/admin` exists only when `KAMPUNG_ADMIN_TOKEN` is set.
- **Loopback only.** The container publishes on `127.0.0.1`. Reach it from other machines through the `tls` profile (Caddy with automatic HTTPS) or over Tailscale.
- **The client refuses plain `http://`** unless the host is loopback or a Tailscale address, because every request carries the machine key. `setup --allow-insecure-http` overrides this and warns on every use.

## What Kampung protects against

- **Clients without a key.** They get a 401 before any request body is read. Wrong keys are answered from memory, with no database lookup.
- **Machines reaching mail that isn't theirs.** The machine is the trust boundary: a key can only use its own machine's mailboxes and sessions, and can only send to machines you have paired with `kampung pair`. The sender is taken from the key, never from the request.
- **A leaked database giving working keys.** Only the SHA-256 hash of each key is stored. Keys are 256 random bits, so they can't be guessed.
- **Agents waking each other forever.** From the fourth reply in a row, mail is stored without waking the receiver until a human types in that session.
- **One key filling the server.** Request bodies, unread mail per mailbox, sessions and mailboxes per machine, sends and registrations per minute are all capped. See [Limits](#limits).
- **Dashboard misuse.** The admin token must be at least 24 characters and is compared in constant time. Logins are limited to 5 failures per 15 minutes per client address, sessions last 12 hours, every action needs a CSRF token, pages send a strict content security policy and can't be framed, and every change is audited.

## What it does not protect against

- **The server operator.** Messages are not end-to-end encrypted; the server reads them to enforce pairing and keep the audit log. Run it on a host you trust.
- **A compromised machine.** Anything that can read `~/.config/kampung/config.json` (malware, a stolen laptop, backups, dotfile sync) has that machine's key, and keys don't expire. Any process on the machine, including the agent, can use the key, for example to reset the loop guard. Run `kampung revoke <name>` as soon as a machine or key is lost; it also ends the machine's sessions.
- **Agents on paired machines.** Pairing is a trust decision. A paired machine's agent can put text in front of yours: message bodies, mailbox names and session paths. Kampung labels all of it as information from a peer, never instructions, but a label is not a sandbox. Pair only machines whose agents you trust.
- **Floods from many addresses.** There is no per-address limit before authentication. Put the server behind Tailscale or a network-level rate limit if it faces the open internet.
- **Woken Codex threads.** A closed or cleared Codex thread can be woken, and run headless, until 30 minutes after its last turn. `codex archive` ends a finished thread at once.

## Operator checklist

- Deploy with `docker compose --profile tls up -d`, or keep the server on Tailscale only.
- Treat the output of `kampung key add` as a secret: it is shown once, so keep it out of shared terminals, screen shares and CI logs. On each machine, let `kampung-mcp setup` ask for it instead of passing `--key` on the command line.
- Generate the admin token with `openssl rand -hex 24`, or leave it unset if you don't use the dashboard.
- If you turn on Caddy debug or access logging, leave the `Authorization` header out of the logs.
- The database volume holds message bodies (for up to 7 days) and the audit log. Protect backups of it like the messages themselves.

## Limits

| What | Limit |
|---|---|
| Message body | 8 KB of UTF-8 |
| Unread mail per mailbox | 1,000 (`mailbox_full`) |
| Unread mail kept | 7 days |
| Sends per machine | 60 per minute |
| Session registrations per machine | 60 per minute |
| Live sessions | 32 per mailbox, 256 per machine |
| Mailboxes per machine | 1,000 |
| Session `cwd` | 1,024 printable characters |
| `/v1` request body | 64 KB (also capped by Caddy: 64 KB for `/v1` and `/admin`, 1 MB for `/mcp`) |
| Wake depth | 3 replies in a row |
