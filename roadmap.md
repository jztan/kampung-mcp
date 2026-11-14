# Roadmap

Order is by priority, with no target dates. Reliability comes first, then waking idle sessions, then more clients, then operator hardening. 1.0 follows a real upgrade cycle. CHANGELOG has the detail of each release.

## Findings that shape the plan

- **Idle wake no longer needs the development flag** (since v0.5, Claude Code 2.1.321 or later). The Channels fallback still does, and a plugin would not remove it, so the plugin is for easier install only.
- **Channels is a research preview**, off by default on Team and Enterprise and missing on Bedrock, Google Cloud and Foundry, so idle wake must not rely on it alone.

## Releases

### v0.1.1: small fixes (released 2026-10-31)

- Hook API, adapter and login limiter fixes; clearer errors and hints.

### v0.2: know it's working (released 2026-11-01)

- `kampung-mcp doctor`, with a client and server version check.
- Linux and Windows checked on real machines.
- `docs/tool-reference.md`.

### v0.3: the Claude app (released 2026-11-10)

- The Claude app as a peer.
- Attachments, with a secret check on every message.
- Loop guard v2: wake budgets per pair and per mailbox.

### v0.4: send modes and background sessions (released 2026-11-11)

- `send` modes: `ask`, `tell`, `fyi`.
- Background Claude Code sessions.

### v0.5: wake idle sessions (released 2026-11-12)

- Idle wake without Channels.
- Owner instructions from the Claude app.

### v0.6: know which session is which (released 2026-11-14)

- Reachable sessions: a quiet machine is shown as probably asleep.
- Live push over a per-session event stream.
- Session labels: git branch and a summary the agent sets.
- A lost owner mark is reported, not silent.

### v0.7: Codex on Windows joins in (released 2026-11-14)

- Codex hooks and idle wake on Windows.
- A reminder to set the session summary.
- `set_summary` takes `summary`.

### v0.8: operator hardening and more clients

- Claude Code plugin, for easier install.
- ChatGPT app as a peer (spike first).
- OpenCode as a client (spike first).
- Codex waker bound, if daily use needs it.
- Audit log: append-only, retention by kind, a copy to stdout.
- Key lifetime: expiry, last used, Rotate on the dashboard.
- CLI parity: `session end`, `session refill`, `message delete`.
- Decide whether `/health` keeps returning the version.
- Decide how the rate limiter and send errors treat failed sends and unpaired keys.

### v1.0: stable contract

Freeze the five MCP tools and `/v1` once these hold:

- Several weeks of daily use across the owner's machines with no silent failures.
- A schema migration has run on a live server without losing mail.
- Linux and Windows verified on real machines.
- Idle wake has held up in daily use, with no flag labelled "dangerous".

## Not scheduled

- Landing page that shows the Claude apps (phone, desktop, web, voice) as peers.
- On demand only: Postgres, OIDC, a broker for multiple replicas, live dashboard updates.
- Keep the FastMCP 3 pin until Claude Code registers channel servers on protocol 2026-07-28.
