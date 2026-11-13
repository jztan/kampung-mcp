# Roadmap

Drafted 2026-10-30 from the backlog, the design spec, the changelog and the current Claude Code docs. Order is by priority; there are no target dates except the launch post hold.

Reliability comes first, then waking idle sessions, then more clients, then operator hardening. 1.0 follows a real upgrade cycle.

## Findings that shape the plan

- **A Claude Code plugin will not remove the development flag.** Claude Code's docs say a channel in your own marketplace still needs `--dangerously-load-development-channels`, because only `claude-plugins-official` is on the default allowlist. The plugin is worth doing for easier install only.
- **Channels has limits beyond the flag.** It is a research preview, so its contract may change. It is off by default on claude.ai Team and Enterprise, and unavailable on Amazon Bedrock, Google Cloud's Agent Platform and Microsoft Foundry. Idle Claude Code wake-up should not rely on it alone.

## Releases

### v0.1.1: small fixes (released 2026-10-31)

- Cap hook API path ids at 18 digits, so a 20-digit id cannot overflow SQLite.
- Answer connection errors in `tools.call` as `busy`.
- Treat `hook.LookupFailed` as alive in the adapter's `_alive`.
- Log the exception behind `server_unreachable`, and give a non-401 HTTP answer its own error code.
- Show a friendly `KAMPUNG_DB_PATH` hint instead of a traceback when `/data` is missing.
- Cap `hooks.log` by size.
- Align the dashboard's Add-a-machine help text with `validate_name`.
- Fix the login limiter: owner lockout under a wrong-token flood, and `_failures` never pruned.
- Reword the Stop hook block reason, which Claude Code labels "Stop hook error".
- README: rerun setup if the `uv tool` install moves to another Python minor version.
- README: correct the Roadmap section's promise that the plugin removes the development flag.

### v0.2: know it's working (released 2026-11-01)

- `kampung-mcp doctor` (new). Hooks fail open by design, so every breakage is silent.
- Client and server version check (new), as part of `doctor`.
- Repeat the ancestor-matching spike on Linux, and confirm how Claude Code spawns hooks there.
- Finish the Windows checks on Windows Server 2025: two windows in one repo, `/clear`, hook quoting without Git Bash, config file ACLs, Codex on Windows.
- Write `docs/tool-reference.md`.
- Add "back up first" to the upgrade docs.

### v0.3: the Claude app (released 2026-11-10)

- Claude app connector (new): the Claude app and claude.ai join as a peer over OAuth, with a `<name>/claude` mailbox.
- Attachments and the `read_attachment` tool (new); the server re-encodes images.
- A secret check on every body and text attachment (new).
- Loop guard v2: wake budgets per pair of mailboxes and per mailbox; `send` takes `fyi`.
- Codex windows are listed and wakeable at launch, and the wake text names the session.

### v0.4: send modes and background sessions (released 2026-11-11)

- `send` mode (new): `ask`, `tell` or `fyi`, so a message can wake its receiver without asking for a reply.
- Background sessions (new): Claude Code sessions in the background (`claude --bg`, `/background`, background agents) are tagged, leave mailbox mail to a live window, and a send that only reaches them says to expect a late reply.
- Treat a non-finite `checked` in the nudge state as 0.

### v0.5: wake idle sessions (released 2026-11-12)

- Idle wake without Channels (new): a file each session watches and a `FileChanged` hook wake an idle Claude Code session, in a window or in the background, with no flag to launch. Needs Claude Code 2.1.321 or later. On Windows it is verified for a session in a window, not yet for a background session. Channels stays as an optional fallback.
- Owner instructions (new): the owner's Claude app can send `mode: "instruct"`, a server-stamped message an agent may act on as the owner typing, within limits the owner sets in his own instruction file. Off by default (`kampung owner add`).

### v0.6: more clients

- Reachable sessions (done on develop): `list_peers`, `send` and the dashboard say when a peer's machine has gone quiet (probably asleep) instead of calling it live.
- SSE `/v1/events` to replace the adapter's `peek` polling, after idle wake. It also ends the database write that every polling session makes every 2 seconds.
- Session labels in `list_peers` and the dashboard (new), automatic only: the session's git branch, plus a one-line summary the agent sets and keeps up to date itself (never prompt text). Two live windows in one mailbox can then be told apart.
- A lost owner mark is no longer silent (new): when the Claude app's connection is replaced (a reconnect, a reused refresh token, a revoke) and the owner mark goes with it, the server records why, the `owner_only` refusal says when and how to restore it (`kampung owner add`), the dashboard flags it, and the app's mailbox gets a note. The mark is never carried over to a new connection automatically, since nothing proves it is the owner's account.

### v0.7: operator hardening and more clients

- Package the client as a Claude Code plugin, for easier install only.
- ChatGPT app as a peer (spike first): whether its custom connectors accept Kampung's OAuth flow (one fixed public client, a one-time code, no dynamic client registration), which plans allow connectors with write tools, and OpenAI's outbound IP ranges for the firewall and Caddy.
- OpenCode as a client (spike first): how it registers stdio MCP servers, whether it has hooks for session start, prompt submit and stop, and how to wake an idle session. OpenCode runs many models, so one adapter brings many vendors.
- Codex waker bound, if headless turns or stolen mail show up in daily use.
- Enforce append-only audit with a SQLite trigger.
- Audit retention: keep notable rows past the 90-day, 500,000-row limit that v0.3 applies to all rows.
- Write notable audit events as JSON lines to stdout.
- Key lifetime: optional expiry, a last-used column, and a Rotate button on the dashboard.
- CLI parity: `session end`, `session refill`, `message delete`.
- Decide whether `/health` keeps returning the version.
- Decide whether the rate limiter counts failed sends, and whether send errors hide machine names from unpaired keys.

### v1.0: stable contract

Freeze the five MCP tools and `/v1` once these hold:

- Daily use across the owner's machines for several weeks with no silent failures.
- At least one schema migration has run on a live server without losing mail.
- Linux and Windows are verified on real machines.
- Idle wake-up has a decided answer that works without a flag labelled "dangerous": idle wake (v0.5), once it has held up in daily use.

## Not scheduled

- **Landing page shows the Claude apps as peers:** the page is almost all about Claude Code and Codex. Show the Claude app on the phone, desktop and web (voice mode too) as a Kampung peer: checking the inbox by voice and sending work to sessions on other machines.
- **On demand only:** Postgres, OIDC, a broker for multiple replicas, live dashboard updates.
- **Keep the FastMCP 3 pin:** Claude Code still does not register a channel server on protocol revision 2026-07-28.
