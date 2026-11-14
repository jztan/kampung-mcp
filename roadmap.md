# Roadmap

Each release fixes a problem found in daily use. Nothing is built ahead of a real need: an idea waits under Not scheduled with the event that would bring it back. CHANGELOG has the detail of each release.

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

### v0.8: whatever daily use turns up

- No planned features. Fixes for problems hit in daily use, logged below.

### v1.0: a tool trusted day to day

The soak started on 2026-11-14 with v0.7.0. v1.0 ships after three weeks in a row of daily use across the owner's machines with nothing new in the soak log.

- [ ] Three weeks with no silent failure and no new problem (earliest 2026-12-05).
- [x] A schema migration has run on a live server without losing mail (schemas 1 to 11, v0.2 to v0.7).
- [x] Linux and Windows verified on real machines (v0.7.0 check, 2026-11-14).
- [x] Idle wake without a flag labelled "dangerous" (since v0.5).

### Soak log

A silent failure is mail lost, or a wake or a reply that should have happened and did not, with no error. A logged problem restarts the three weeks once its fix ships.

| Date | Problem | Fixed in |
| --- | --- | --- |
| | None yet | |

## Decided

- `/health` keeps returning the version: `doctor` needs it, and the version is public anyway.
- Failed sends keep counting toward the rate limit, and send errors keep telling an unknown machine from an unpaired one: every key belongs to the owner, so a clear error is worth more than hiding names.

## Not scheduled

Each item comes back when its trigger happens.

- Claude Code plugin: setting up a new machine is a real chore.
- ChatGPT app as a peer (spike first): the owner wants a ChatGPT chat to take part.
- OpenCode as a client (spike first): the owner starts using OpenCode for real work.
- Codex waker bound: a closed Codex thread is seen waking headless or taking mail.
- Audit log hardening (append-only trigger, retention by kind, a copy to stdout): an audit row goes missing or is needed outside the database.
- Key lifetime (expiry, last used, Rotate on the dashboard): a key leaks or is lost, or rotating one by CLI proves too slow.
- CLI parity (`session end`, `session refill`, `message delete`): one of them is needed when the dashboard is out of reach.
- Landing page that shows the Claude apps (phone, desktop, web, voice) as peers.
- On demand only: Postgres, OIDC, a broker for multiple replicas, live dashboard updates.
- Keep the FastMCP 3 pin until Claude Code registers channel servers on protocol 2026-07-28.
