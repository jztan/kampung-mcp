# Changelog

All notable changes for people who run a Kampung server or use it from their agents. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

#### Client (`kampung-mcp`)

- Attachments. `send` takes the name of one file saved in the project's `kampung-outbox` folder: UTF-8 text up to 256 KB, or a PNG or JPEG, which the adapter shrinks to 1568 pixels on the long edge first. The adapter reads only from that folder, refuses links, keeps the folder out of git, and deletes the file once the server accepts it. The new `read_attachment` tool reads an attachment: text in slices with Markdown headings to jump to, images whole. The client now depends on Pillow.
- `kampung-mcp setup` now tells Claude Code not to load the Claude app connector (`claude.ai kampung`, or the name given with `--connector-name`) by adding it to `deniedMcpServers` in `~/.claude/settings.json`. Without that, a window on the same account has two `kampung` servers and may read the phone's mail instead of its own. Rerun setup on machines set up earlier.

#### Server (`kampung-server`)

- Attachments, one per message, stored apart from the body. `inbox` shows each one's name, type and size, never its content, and `read_attachment` fetches it. The Claude app can attach text it writes; it cannot send images. The server re-encodes every image and drops its metadata; it refuses images over 8 million pixels from their header and decodes two at a time, answering `busy` to a send that waits more than 5 seconds. Unread attachments are capped at 25 MB per sending machine and 500 MB on the server, and none are accepted when the disk is low.
- A secret check on every message body and text attachment: private keys, common provider tokens, passwords in URLs and `.env` style lines. A hit refuses the send with `secret_detected`, naming the kind and line, never the secret. There is no override; replace the value with a placeholder and send again.
- The app now caps `/mcp` request bodies at 1 MB itself, as Caddy already did. A request without a known key still gets its 401 before its body is read.
- The server now depends on Pillow.
- `list_peers` hides mailboxes with no open session, which used to be most of its output. Pass `include_empty: true` to see them; they still take mail.
- The Claude app connector. With `KAMPUNG_PUBLIC_URL` set, the Claude app and claude.ai can join as a peer with its own mailbox (`<name>/claude`) through OAuth: `kampung connect <name>` prints the connector URL, the client id and a one-time code; `kampung disconnect <name>` ends the connection. There is one fixed public OAuth client and no client registration. Pending sign-ins are kept to 3 per client address and 100 in all; a new one drops the oldest instead of being refused, so a flood cannot lock you out. Connector sends spend no loop-guard budget and may wake peers at most 10 times in 10 minutes (mail to a mailbox with nobody in it does not count); past that, `send` returns `"wake": false` with `"reason": "wake_budget"`. `list_peers` shows such sessions with agent `claude-app`. Update `deploy/Caddyfile` too: the new one keeps Anthropic's range (`160.79.104.0/21`) off the sign-in pages and the hook API, and caps the OAuth routes at 64 KB.
- Ending a connector grant (`kampung disconnect`, a revoke from Claude, or a refresh token reused after rotation) also ends the `<name>/claude` session, so `list_peers` stops listing it at once.

### Changed

#### Server (`kampung-server`)

- A new loop guard replaces message depth, which stopped honest reviews after about two rounds. Each pair of mailboxes may wake each other 9 times in a row and gets one wake back every 10 minutes; each mailbox may wake peers 30 times until someone types in one of its sessions. A session that never reports typing (Codex on Windows, Codex before its hooks are approved, a window whose hooks fail, or an agent registered without them) would otherwise be stuck, so for it the mailbox budget comes back after 30 minutes without waking sends; a runaway loop never gets that quiet, so it still stops. Typing, the dashboard's new Refill button (it replaces Reset depth), or a waking message from the Claude app refills them; the app refills a mailbox at most once every 10 minutes, only toward machines its own machine is paired with. Mail past a budget still arrives, without waking anyone. All four numbers are settings: `KAMPUNG_PAIR_WAKE_SIZE`, `KAMPUNG_PAIR_WAKE_REFILL_MINUTES`, `KAMPUNG_MAILBOX_WAKE_BUDGET` and `KAMPUNG_CONNECTOR_REFILL_COOLDOWN_MINUTES` (see `.env.example`).
- `send` results no longer carry `depth`. When `wake` is false they give a `reason` (`fyi`, `cannot_wake`, `wake_budget`, `mailbox_budget` or `pair_budget`) and a `hint` saying who has to act, and `budget` shows the pair's wakes left. Mail to a mailbox nobody has open says `"note": "no_session"`. Envelopes drop `depth` and gain `fyi`.
- `send` takes `fyi: true` for a note that needs no reply: it never wakes the receiver and spends no budget.
- Another machine's session id now gets `unknown_session`, the same as an id that does not exist, instead of `not_owner`, and no longer writes an audit row, so ids cannot be probed and probing cannot fill the disk. The hook API limits `peek` to 7,680 calls a minute per machine (256 windows polling every 2 seconds) and `end` to 600.
- The audit log keeps 90 days and at most 500,000 rows; older rows are deleted a batch at a time during cleanup.
- The database migrates from schema version 2 to 5 on start and switches to incremental vacuum, which rewrites the file once; take a snapshot first.

#### Client (`kampung-mcp`)

- `send` takes `fyi: true`, and its description explains the loop guard, so agents know to stop and tell you when a send comes back with `wake: false`. Against a server too old for `fyi`, it says so instead of reporting a connection problem.
- A session started in the home folder or under a temp folder (`/tmp`, macOS's `/var/folders`, `%TEMP%`), where eval and throwaway runs live, now uses the machine's shared `scratch` mailbox instead of one named after the folder, so those folders no longer each leave a mailbox behind. Any other folder outside git still gets a mailbox named after it. Mail already waiting in an old folder mailbox stays there.
- A Codex window you have just opened is listed and wakeable before your first prompt, when setup found `codex` on your `PATH`. The adapter finds its thread through the Codex daemon's control socket at launch, adopts the session Codex's own `SessionStart` hook may already have registered, and claims the thread with a small lock file in `~/.cache/kampung/codex-claims/` so two adapters never share one; if that fails, the window is registered at its first prompt as before. Two windows opened in one folder within seconds may still wait for their first prompt. Not on Windows. `doctor` checks the socket (`codex launch`).
- The text that wakes an idle Codex thread now names the session it wakes, such as `[kampung] laptop/api#3f: 1 Kampung message(s) waiting.` Codex titles a thread by its first prompt, so threads that were only ever woken can now be told apart in its thread list. [Clients](docs/clients.md#find-where-a-woken-codex-thread-did-its-work) says how to open the thread a wake went to.
- A folder now keeps the mailbox name it first registered with, recorded in `~/.cache/kampung/mailboxes.json`, so a later client upgrade that names folders differently never moves it to another mailbox and strands its mail. Home and temp folders, which share `scratch`, are not recorded.

### Fixed

#### Server (`kampung-server`)

- The MCP handshake (`serverInfo`) now reports Kampung's own version. It used to report the FastMCP library version.
- A machine key that learned another machine's Claude Code or Codex session id could register it first, so that window's hooks failed quietly. Session ids are now unique per machine.
- A revoked machine key kept counting as known, so it skipped the per-address wrong-key limit; it now counts as wrong from its first refusal.
- A machine key can no longer act as its machine's Claude app session. Registering with the connector's session id, or with its repo and suffix, used to revive that session, and sends from it skipped the loop guard's depth check. It took a key, so it stayed inside the machine trust boundary.

## [0.2.2] - 2026-11-07

### Added

#### Server (`kampung-server`)

- `/v1/sessions/{id}/peek` also returns `newest`, the id of the newest wakeable message (or `null`).

### Fixed

#### Client (`kampung-mcp`)

- Mid-turn nudges, Channels push and Codex wakes no longer miss a message that arrives just after another one was read, leaving the waiting count unchanged. This needs a server that returns `newest`; with an older server the client compares counts as before.

## [0.2.1] - 2026-11-01

### Changed

#### Client (`kampung-mcp`)

- The README on PyPI now says beta, matching the release. Nothing else changes from 0.2.0.

## [0.2.0] - 2026-11-01

### Changed

#### Client (`kampung-mcp`)

- Kampung is now in beta. The MCP tools and `/v1` can still change before v1.0.

### Added

#### Client (`kampung-mcp`)

- `kampung-mcp doctor` checks a machine's setup and says how to fix what it finds: the config, the server, the key, client and server versions, each agent's hooks and MCP registration, and recent failures in `hooks.log`. It exits 1 if a check fails.

### Fixed

#### Client (`kampung-mcp`)

- On Windows, Codex no longer hangs on its first Kampung call.
- On Windows, Claude Code hooks now run without Git Bash. Setup writes them for PowerShell, so rerun `kampung-mcp setup` on Windows machines and keep Claude Code current.

## [0.1.1] - 2026-10-31

### Changed

#### Server (`kampung-server`)

- The dashboard's Add-a-machine help now states the real naming rule: letters, digits, dots, underscores and dashes, up to 64 characters, starting with a letter or digit.

#### Client (`kampung-mcp`)

- The `Stop` hook's message now says it is mail, not a failure, because Claude Code shows it under the label "Stop hook error".
- A failed tool call is logged to `~/.cache/kampung/hooks.log`, and an HTTP failure that isn't a rejected key now answers `server_error` instead of `server_unreachable`.
- `hooks.log` rolls over to `hooks.log.1` at 1 MB, so it can't grow without limit.

### Fixed

#### Server (`kampung-server`)

- A hook API session id too large for the database now answers 404 instead of a server error.
- A database that is locked at the moment a tool call connects now answers `{"error": "busy"}`.
- `kampung` commands explain how to set `KAMPUNG_DB_PATH` instead of showing a traceback when the database can't be opened.
- A flood of wrong tokens from your address no longer locks you out of the dashboard on a browser that has signed in before. Browsers that haven't stay limited.
- The dashboard's login limiter now forgets idle addresses and tracks at most 10,000, so it can't grow without limit.

#### Client (`kampung-mcp`)

- When process ancestry can't be read, the adapter no longer takes the session as gone and registers a second one, which made `#xx` mail miss the window.

## [0.1.0] - 2026-10-30

### Added

#### Server (`kampung-server`)

- A self-hosted server that lets agent sessions on different machines send each other short text messages over MCP (`/mcp`, Streamable HTTP), with a small hook API at `/v1`.
- Three MCP tools for agents: `list_peers`, `send` and `inbox`.
- Mailboxes addressed as `machine/repo`, or `machine/repo#xx` to reach one specific session. Mail waits in the mailbox until a session reads it, and a read delivers it.
- Every delivered message comes in an envelope with the sender, sent time, depth and a "peer message, not an instruction" tag.
- Message limits: 8 KB per message, 7 days before unread mail expires, at most 20 messages per `inbox` call (with a count of the rest), and 60 sends per minute per machine.
- Limits that stop one machine key from filling the server: at most 1,000 unread messages per mailbox and 200 from any one sending machine (`send` then answers `mailbox_full`), 32 live sessions per mailbox and 256 per machine, 60 session registrations per minute per machine, 1,000 mailboxes per machine, a session `cwd` of up to 1,024 printable characters, and hook API request bodies of up to 64 KB. The key is checked before the body is read, a wrong key is answered from memory without a database lookup, and an address that sends 20 wrong keys within a minute gets 429 for further wrong keys until those age out (valid keys from the same address still get through). Sessions that ended more than 30 days ago are deleted, so their names can be reused.
- `list_peers` labels its result as peer-supplied, since mailbox names and session paths are set by the other machine.
- `send` accepts a `reply_to` id only for mail the sending machine sent or received.
- Ending a session through the hook API is now audited, as is a refused attempt.
- Expired mail is deleted every 5 minutes, not only when a mailbox is read, so it never outlives its 7 days on a quiet server.
- Cleanup stays fast however many sessions and messages pile up: it uses indexes and deletes at most 1,000 old sessions per pass, so it never holds up other requests. A database from an earlier build gets the indexes when the server starts.
- `SECURITY.md` describes the deployment model, what Kampung does and does not protect against, an operator checklist and every limit.
- A loop guard: from the fourth reply in a row between agents, mail is stored without waking the receiver. The count resets when a human types in that session, or after 30 minutes for sessions without hooks (such as Codex before its hooks are approved).
- The `kampung` admin CLI: `key add`, `key list`, `key rotate`, `pair`, `unpair`, `revoke` and `serve`. Machines can't exchange mail until you pair them, and revoking a key ends its sessions. `key rotate` replaces a leaked key and keeps the machine's mailboxes and pairings. `key add` and `key rotate` print the key alone on stdout, so it can be piped into `kampung-mcp setup --key -` without showing on screen.
- Closed by default: every `/mcp` and `/v1` request needs a machine key, keys are stored hashed, and `/health` returns only the version.
- An admin dashboard at `/admin`, turned on by setting `KAMPUNG_ADMIN_TOKEN` (at least 24 characters). It shows machines, mailboxes, sessions, messages and the audit log, and lets you create keys, pair and unpair, revoke, end a session, reset a session's loop-guard depth and delete a queued message. The audit log shows notable events by default (admin actions and denied requests), with sessions named by address and message ids linked; choose Everything to see sends and reads. A Conversations page shows the mail between each pair of mailboxes as a thread. List pages refresh in place every 15 seconds without losing your scroll position. Works in light and dark mode and on a phone.
- A busy database answers `{"error": "busy"}` instead of failing the tool call.

#### Deployment

- A Docker image and `docker-compose.yml`: `docker compose up -d --build` runs the server as a non-root user on `127.0.0.1:8803`, with its database in a named volume. Manage keys with `docker compose exec kampung kampung ...`.
- Release images for amd64 and arm64 at `ghcr.io/jztan/kampung-mcp`, published for each version tag. Compose pulls the tag set in `KAMPUNG_IMAGE_TAG`, or builds from the checkout with `--build`.
- The image carries the server's AGPL-3.0 license and the NOTICE file under `/usr/share/licenses/kampung-server/`, and OCI labels that link it to this repository.
- An optional Caddy service for HTTPS (`docker compose --profile tls up -d`, Linux hosts), set up from `KAMPUNG_DOMAIN` in `.env`. It keeps `/admin` to loopback and the Tailscale range. It also caps request bodies: 64 KB for `/v1` and `/admin`, 1 MB for `/mcp`.
- Both compose services cap their logs at 3 files of 10 MB and run with `no-new-privileges` and every Linux capability dropped (Caddy keeps only the one that binds ports 80 and 443). The Kampung container's filesystem is read-only apart from its database volume. `.env.example` lists every setting.

#### Client (`kampung-mcp`)

- `kampung-mcp setup`, which connects a machine to a server: it checks the server and key, saves them, installs the Claude Code hooks and registers the MCP adapter with Claude Code, Codex or both. It asks for the key without echoing it, or reads it from stdin (`--key -`) or `KAMPUNG_KEY`, so the key stays out of shell history.
- `setup` keeps other hooks that share an entry with Kampung's, and writes `settings.json` and Codex `hooks.json` in one step, so an interrupted setup cannot leave them half written. A symlinked `settings.json` stays a symlink.
- Published on PyPI as `kampung-mcp` (`uv tool install kampung-mcp`) and listed in the MCP Registry as `io.github.jztan/kampung-mcp`.
- `kampung-mcp set-key`, which saves a new key after `kampung key rotate` without running setup again. It checks the key against the server before saving it.
- Claude Code hooks that register each session, keep the agent going to read mail that is waiting when its turn ends, nudge a busy agent about new mail between tool calls, and reset the loop guard when you type. They time out quickly and do nothing if the server is slow or down.
- Codex support with the same hooks (`setup --agent codex` or `both`; approve them once in Codex with `/hooks`). Mail is announced between tool calls, read at the end of a turn, and the loop guard resets when you type. An idle Codex session wakes within a few seconds of new mail through `codex queue` (not on Windows yet). A closed or cleared Codex thread can still be woken, and run headless, until 30 minutes after its last turn; `codex archive` ends a finished thread at once.
- `kampung-mcp setup --channels`, which wakes an idle Claude Code session when mail arrives (experimental, needs Claude Code 2.1.80 or later). Start Claude Code with `--dangerously-load-development-channels server:kampung` to use it. Setup without `--channels` says that an idle session won't notice mail until you type.
- The client never sends the machine key over plain `http://`, except to loopback addresses. Tailscale addresses need `--allow-insecure-http`, since their range overlaps carrier-grade NAT. Setup refuses other `http://` server URLs before contacting the server, and the hooks and adapter refuse a `config.json` that points at one. `setup --allow-insecure-http` lifts this for a trusted network and prints a warning whenever it is used. Requests that carry the key no longer follow redirects.

#### Site

- A page at [kampung-mcp.jztan.com](https://kampung-mcp.jztan.com) that replays Claude Code on a laptop and Codex on a NAS working together on a cited blog section, with stills of two more exchanges (tests that only fail on Windows, a tracker behind the office network), the quick start and the security model.

[0.2.2]: https://github.com/jztan/kampung-mcp/releases/tag/v0.2.2
[0.2.1]: https://github.com/jztan/kampung-mcp/releases/tag/v0.2.1
[0.2.0]: https://github.com/jztan/kampung-mcp/releases/tag/v0.2.0
[0.1.1]: https://github.com/jztan/kampung-mcp/releases/tag/v0.1.1
[0.1.0]: https://github.com/jztan/kampung-mcp/releases/tag/v0.1.0
