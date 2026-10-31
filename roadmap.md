# Roadmap

Drafted 2026-10-30 from the backlog, the design spec, the changelog and the current Claude Code docs. Order is by priority; there are no target dates except the launch post hold.

Reliability comes first, then waking idle sessions, then operator hardening. 1.0 follows a real upgrade cycle.

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

### v0.2: know it's working

- Done, unreleased: `kampung-mcp doctor` (new). Hooks fail open by design, so every breakage is silent.
- Done, unreleased: client and server version check (new), as part of `doctor`.
- Find the flaky test. It failed once and did not come back in 9 full runs, 3 of them under CPU load, so this waits until it fails again with a name.
- Repeat the ancestor-matching spike on Linux, and confirm how Claude Code spawns hooks there.
- Finish the Windows checks: two windows in one repo, `/clear`, hook quoting without Git Bash, config file ACLs, Codex on Windows.
- Done, unreleased: write `docs/tool-reference.md`.
- Done, unreleased: add "back up first" to the upgrade docs.

### v0.3: wake idle sessions

- `asyncRewake` spike (new), run before the SSE work. The docs describe a background hook that can wake Claude on exit code 2, which might wake an idle session without Channels. Untested, and which events support it is still unconfirmed.
- SSE `/v1/events` to replace the adapter's `peek` polling.
- Have `peek` return the newest wakeable id, so a read plus a new arrival between polls still pushes.
- Fix the double nudge from parallel tool calls, and treat a NaN `checked` as 0.
- Package the client as a Claude Code plugin, for easier install only.
- Codex waker bound, if headless turns or stolen mail show up in daily use.

### v0.4: operator hardening

- Enforce append-only audit with a SQLite trigger.
- Audit retention: drop routine rows after 30 days, keep notable ones.
- Write notable audit events as JSON lines to stdout.
- Key lifetime: optional expiry, a last-used column, and a Rotate button on the dashboard.
- CLI parity: `session end`, `session reset-depth`, `message delete`.
- Decide whether `/health` keeps returning the version.
- Decide whether the rate limiter counts failed sends, and whether send errors hide machine names from unpaired keys.

### v1.0: stable contract

Freeze the three MCP tools and `/v1` once these hold:

- Daily use across the owner's machines for several weeks with no silent failures.
- At least one schema migration has run on a live server without losing mail.
- Linux and Windows are verified on real machines.
- Idle wake-up has a decided answer that works without a flag labelled "dangerous".

## Not scheduled

- **Claude app connector:** planned after v0.4, if real use shows the phone needs to read and leave mail. Decide before v1.0, because it would change the frozen tool set.
- **On demand only:** Postgres, OIDC, a broker for multiple replicas, live dashboard updates, session summaries for `list_peers`.
- **Remove `claim_task`:** it contradicts the task-board non-goal and the rejected claim and ack design.
- **Keep the FastMCP 3 pin:** Claude Code still does not register a channel server on protocol revision 2026-07-28.
