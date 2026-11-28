# Roadmap

Each release fixes a problem found in daily use; nothing is built ahead of a real need. CHANGELOG has the detail of each release.

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

### v0.8: easier to try (released 2026-11-15)

- `kampung-mcp try` and `kampung-mcp uninstall`.
- `list_peers` shows each session's latest step; `set_summary` is removed.
- Inbox filters by sender and by reply, so Claude app chats can share one mailbox without taking each other's mail.

### v0.9: know whether a message was read (released 2026-11-18)

- Read receipts: `inbox` with `replies_to` says whether each message sent is unread, read or expired.

### v0.10: old mailboxes tidy themselves (released 2026-11-27)

- Idle empty mailboxes are removed; a mailbox can be deleted by hand.
- A Claude app chat finds answers it lost the ids for.

### v0.11: sessions you can name (released 2026-11-28)

- Session names from the agent's own title, `machine/repo@name`, and rename from the Claude app.

### v0.12: run the adapter in your own agent

- `adapter --agent other` with `KAMPUNG_SESSION_FILE`, so another agent host owns the session.

### v1.0: a tool trusted day to day

- [ ] Seven days in a row of daily use with no silent failure and no new problem (soak restarted 2026-11-17, earliest 2026-11-24).
- [x] A schema migration has run on a live server without losing mail.
- [x] Linux and Windows verified on real machines.
- [x] Idle wake without a flag labelled "dangerous".

## Easier to try, and real feedback

A separate track, worked on now. It ships in soak patches until it is stable, and then that release is v1.0.

- [x] A "first 5 minutes" quickstart on one machine.
- [x] Hosting on a home machine or NAS (a home-network section, and the Tailscale guide).
- [x] A "tell us what broke" link in the docs and in `doctor`.
- [x] `kampung-mcp try` for a one-command local trial.
- [ ] A pinned Discussion, now that `try` is released.
- [ ] One post where agent users are, now that `try` is released.

## Decided

- `/health` keeps returning the version.
- Failed sends keep counting toward the rate limit, and send errors keep naming unknown machines.

## Not scheduled

Each waits for a real need.

- Claude Code plugin.
- ChatGPT app and OpenCode as peers (spike first).
- Codex waker bound.
- Audit log hardening and key lifetime.
- CLI parity for the dashboard's session and message actions.
- A join code for adding a machine, once someone other than the owner sets up a second machine.
- Landing page that shows the Claude apps (phone, desktop, web, voice) as peers.
- On demand only: Postgres, OIDC, a broker for multiple replicas, live dashboard updates.
- Keep the FastMCP 3 pin until Claude Code registers channel servers on protocol 2026-07-28.
