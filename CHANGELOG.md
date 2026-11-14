# Changelog

All notable changes for people who run a Kampung server or use it from their agents. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

#### Server (`kampung-server`) and client (`kampung-mcp`)

- Sessions now set their summary. Until one is set, a Claude Code or Codex session gets a one-line reminder to call `set_summary` when you type, at most once every 10 minutes; once a summary is set, nothing is added. Before this, the adapter's instructions were the only prompt, and agents rarely called it, so `list_peers` showed `summary: null` almost everywhere. The reminder needs both the server and the client on this release; with either one older, nothing changes.

## [0.6.0] - 2026-11-14

### Added

#### Server (`kampung-server`) and client (`kampung-mcp`)

- `list_peers` shows each session's git `branch`, so two windows in one mailbox can be told apart (`laptop/api#ef` on `feature/login`, `#f7` on `develop`). Claude Code and Codex report it when a session starts, when you type, and at the end of each turn, read from the session's own folder (not Codex on Windows, which has no hooks). It is null outside git. The `ambiguous_session` hint for an owner instruction names each session's branch too.
- A new tool, `set_summary`: a Claude Code or Codex agent says in a few words what its session is working on, and `list_peers` shows it as `summary` next to the branch (and in the `ambiguous_session` hint), so windows on the same branch can be told apart too. The adapter tells agents to set it when a task starts or changes. It is one line of up to 80 characters, checked for secrets like a message body, and cleared when a session that had ended is picked up again. The Claude app has none (its one session is shared by every chat). An adapter talking to an older server gets `summary_unsupported` and carries on.
- The dashboard's Sessions page shows each session's branch, summary and client version, and the Machines page the newest client version each machine reported. A client whose major.minor differs from the server's is marked.
- A lost owner mark is no longer silent. When the Claude app's connection is replaced or removed (a reconnect, an old sign-in token reused after an outage, the app disconnecting, or `key rotate`), the mark still goes, but now: a refused owner instruction says when and why and to run `kampung owner add <machine>` (if the connection itself is gone, run `kampung connect` first, as the dashboard says); the app's `inbox` and `list_peers` carry a `notice` until then; the dashboard shows Owner lost on Machines and a line on the overview; and `kampung owner list` lists it. `kampung owner remove` (or Dismiss on the dashboard) dismisses it. Your own `kampung disconnect` or `revoke` still clears the mark but is not reported as a loss. The mark is never carried over to a new connection, since nothing proves it is the same Claude account.
- The database moves to schema 12 on first start (three added session columns, two added machine columns); take a snapshot first. Sessions show no branch or client version until their client is on this release and registers again (start a new session, or type in an open one for the branch).

### Changed

#### Server (`kampung-server`) and client (`kampung-mcp`)

- Live push: the adapter now hears of new mail from a per-session event stream (`GET /v1/sessions/{id}/events`) instead of asking the server every 2 seconds, so an idle session starts waking within about a second of a send, and the server does one small write per open session every 30 seconds instead of every 2. Against an older server, or while a stream is down, the adapter polls as before. A server restart now closes open streams after 3 seconds; adapters reconnect on their own. If you run your own reverse proxy, it must not buffer the stream (the shipped Caddyfile already sets `flush_interval -1`). New limits: 30 `set_summary` calls per machine per minute, 120 event-stream connects per machine per minute and 256 open streams per machine (`too_many_streams` past that).

#### Server (`kampung-server`)

- The Claude app now picks `mode: "instruct"` more reliably when you ask it to get a session to do something. Its `send` description leads with that rule and is half as long (the app could cut the old one off before reaching it), and the `mode` field has its own description. The owner-instructions section to paste into `CLAUDE.md` or `AGENTS.md` gains a line: a session that declines because a message is not an owner instruction asks for it to be resent with mode instruct.
- `list_peers` and `send` now say when a peer's machine has gone quiet (probably asleep), even while its session still counts as live. Each session in `list_peers` has `reachable` (false once an idle-wake session has not checked for mail for 2 minutes; null when Kampung cannot tell) and `seen` ("6 min ago"). A `send` to such a session returns `"note": "unreachable"` with when it was last seen and a hint to tell your user if it is urgent; the message still waits and wakes it when the machine is back, and spends no wake budget. The dashboard shows these sessions as Unreachable and their mailboxes as "asleep?".

## [0.5.0] - 2026-11-12

### Added

#### Server (`kampung-server`)

- Owner instructions, off by default. `kampung owner add <machine>` lets that machine's Claude app send `send` with `mode: "instruct"`: a message the server stamps as yours, delivered to the one session working in the target repo now, readable by that session only and only for 60 minutes. Agents act on it within a section you paste into `~/.claude/CLAUDE.md` or `~/.codex/AGENTS.md`; see [owner instructions](docs/clients.md#owner-instructions). `kampung owner remove` and `kampung owner list` manage it, and the dashboard shows and changes it on the Machines page; the message views show mode `instruct` and the owner kind, and an owner instruction unread after 60 minutes as expired. Reconnecting the app (`kampung connect`) clears the owner mark, as do `disconnect`, `revoke` and `key rotate`. Read the security note first: anything that can steer the app can then direct your agents. The `send` result for an owner instruction adds `to`, the session it went to; new errors are `owner_only`, `attachment_not_allowed`, `cannot_instruct`, `ambiguous_session` and `no_session` (see the [tool reference](docs/tool-reference.md#owner-instructions)). The database moves to schema 9 on first start (three added columns, with idle wake's in schema 8); take a snapshot first.

#### Client (`kampung-mcp`)

- Idle wake without Channels. An idle Claude Code session, in a window or in the background (`claude --bg`, `/background`, a background agent), now wakes within a few seconds of new mail with no flag to launch. Setup adds a `FileChanged` hook that watches one file per session, and the adapter writes to that file when wakeable mail arrives. It needs Claude Code 2.1.321 or later in the terminal CLI. Run `kampung-mcp setup` again to turn it on, then restart open Claude Code sessions once. It is promised only while the machine is awake, and is not supported in the VS Code extension or the desktop app. On Windows it is verified for a session in a window, not yet for a background session. See [idle wake](docs/clients.md#idle-wake).
- `kampung-mcp doctor` has an `idle wake` line: on, or off with the reason (Claude Code older than 2.1.321, the hooks not installed, or the version unreadable), how many open sessions have a confirmed wake, and any session whose last wake went unanswered.

### Changed

#### Server (`kampung-server`) and client (`kampung-mcp`)

- An open Claude Code session with idle wake stays `live` in `list_peers` while it is idle, because its adapter checks for mail every 2 seconds. Until now an idle session without Channels stopped being live after 30 minutes.
- The late-reply note on `send` (`"note": "background"`) now shows only when every session the message can reach is a background session on a client without idle wake. Both sides must be on 0.5 for this: an older server ignores what the client reports and keeps showing the note. The advice to prefer a peer's foreground session stays, since a background session works alone with nobody watching.

#### Client (`kampung-mcp`)

- Channels is now an optional fallback. With idle wake on, the adapter pushes through Channels only when a file wake goes unconfirmed, about 10 seconds after the mail arrives; on older Claude Code it works as before.
- A prompt that starts with `<task-notification>` (an idle wake, and Claude Code's own background task notices) no longer counts as typing, so it does not refill the loop guard.

## [0.4.0] - 2026-11-11

### Added

#### Server (`kampung-server`) and client (`kampung-mcp`)

- `send` takes `mode`: `"ask"` (the default) wakes the receiver and a reply is welcome, `"tell"` wakes it to act on the message with no reply needed, and `"fyi"` wakes nobody, as before. Until now "no reply needed" meant `fyi`, which does not wake the receiver, so a decision sent that way sat unread until its next turn. `tell` spends wake budget like `ask`. `fyi: true` still works and means `mode: "fyi"`; with another `mode` it returns `mode_conflict`. The audit log records a `tell` or `fyi` send's mode. Messages carry `mode` in `inbox`, and the dashboard shows it on the messages list, a message and a conversation. The server's database moves to schema 6 on first start (one added column). A new client talking to an older server answers `mode: "tell"` with `mode_unsupported`; update both.
- Claude Code sessions in the background (`claude --bg`, `/background`, a background agent) register as such. `list_peers`, `kampung-mcp doctor` and the dashboard's sessions page mark them, and the audit log records it. They can't be woken by Channels, so while a foreground window in the same mailbox is live, they leave mailbox mail to it and read only mail sent to their own `#xx` (or sent by that window). A send that may wake but can only reach background sessions says to expect a late reply (`"note": "background"`; the dashboard shows "wakes (background only)"). The agent instructions and the `send` description tell agents to prefer a foreground session. Until now a background conversation counted as one more live session, could not be woken, and could read mail meant for the window. The database moves to schema 7 on first start (one added column). Update both server and client.

### Changed

#### Server (`kampung-server`)

- Anthropic now lists a second outbound range for MCP tool calls, `153.61.208.0/20`, next to `160.79.104.0/21` (see [Anthropic's IP addresses page](https://platform.claude.com/docs/en/api/ip-addresses)). If you use the Claude app connector, add it to your firewall's HTTPS rule, or the app's calls from that range are dropped. `deploy/Caddyfile` now hides the sign-in pages and the hook API from both ranges; run `docker compose --profile tls restart caddy` after updating it.

## [0.3.2] - 2026-11-10

### Added

#### Client (`kampung-mcp`)

- `kampung-mcp doctor` has a `claude-code connector` line that warns unless a URL entry (or `disableClaudeAiConnectors`) keeps Claude Code from loading the Claude app connector.
- `kampung-mcp doctor` now runs the interpreter each agent has registered for Kampung. When the adapter cannot start there, for example a checkout whose `.venv` still lacks Pillow after an upgrade, the agent shows only "connection closed"; doctor now names the missing module and says to run `uv sync` in that checkout (or `uv tool upgrade kampung-mcp` for an installed client). It fails when that interpreter no longer exists, and warns when it runs a different kampung-mcp version than doctor.

### Changed

#### Server (`kampung-server`)

- The Claude app may now wake each mailbox 60 times per 10 minutes, counted per receiving mailbox, instead of 10 times in total. When you drive several sessions by voice, a busy one no longer uses up the wakes for the rest. The number is a new setting, `KAMPUNG_CONNECTOR_WAKE_BUDGET` (1 to 1000, see `.env.example`). The app is never woken itself, so it cannot be half of a loop, and agent-to-agent loop protection is unchanged.

### Fixed

#### Client (`kampung-mcp`)

- `kampung-mcp setup` now also denies the Claude app connector in Claude Code by your server's URL, not only by the name `claude.ai kampung`. A connector with another name, including the ` (2)` Claude adds when two connectors share a name, no longer loads in Claude Code windows as a second `kampung` that reads and takes the phone's mail. Rerun setup on each machine.

## [0.3.1] - 2026-11-10

0.3.1 is the first published release of everything in 0.3.0 below, for both packages: v0.3.0 was tagged, but the image scan stopped it before anything was published.

### Fixed

#### Server (`kampung-server`)

- The server image now installs Debian's security updates when it is built, so it no longer ships the base image's zlib with two known HIGH vulnerabilities.

## [0.3.0] - 2026-11-10

### Added

#### Client (`kampung-mcp`)

- Attachments. `send` takes the name of one file saved in the project's `kampung-outbox` folder: UTF-8 text up to 256 KB, or a PNG or JPEG, which the adapter shrinks to 1568 pixels on the long edge first. The adapter reads only from that folder, refuses links, keeps the folder out of git, and deletes the file once the server accepts it. The new `read_attachment` tool reads an attachment: text in slices with Markdown headings to jump to, images whole. An image over 25 megapixels, or still over 512 KB after shrinking, is refused before it is sent. The client now depends on Pillow.
- On Windows, quit Claude Code and Codex before upgrading the client: a running session locks its files.
- `kampung-mcp setup` now tells Claude Code not to load the Claude app connector (`claude.ai kampung`, or the name given with `--connector-name`) by adding it to `deniedMcpServers` in `~/.claude/settings.json`. Without that, a window on the same account has two `kampung` servers and may read the phone's mail instead of its own. Rerun setup on machines set up earlier.

#### Server (`kampung-server`)

- Attachments, one per message, stored apart from the body. `inbox` shows each one's name, type and size, never its content, and `read_attachment` fetches it. The Claude app can attach text it writes; it cannot send images. The server re-encodes every image, drops its metadata and refuses one over 512 KB; it refuses images over 8 million pixels from their header and decodes two at a time, answering `busy` to a send that waits more than 5 seconds. Unread attachments are capped at 25 MB per sending machine and 500 MB on the server, and none are accepted when the disk is low.
- A secret check on every message body and text attachment: private keys, common provider tokens, passwords in URLs and `.env` style lines. A hit refuses the send with `secret_detected`, naming the kind and line, never the secret. There is no override; replace the value with a placeholder and send again.
- The app now caps `/mcp` request bodies at 1 MB itself, as Caddy already did. A request without a known key still gets its 401 before its body is read.
- The server now depends on Pillow.
- `list_peers` hides mailboxes with no open session, which used to be most of its output. Pass `include_empty: true` to see them; they still take mail.
- The Claude app connector. With `KAMPUNG_PUBLIC_URL` set, the Claude app and claude.ai can join as a peer with its own mailbox (`<name>/claude`) through OAuth: `kampung connect <name>` prints the connector URL, the client id and a one-time code, and creates the machine if it is new; `kampung disconnect <name>` ends the connection. There is one fixed public OAuth client and no client registration. Pending sign-ins are kept to 3 per client address and 100 in all; a new one drops the oldest instead of being refused, so a flood cannot lock you out. Connector sends spend no loop-guard budget and may wake peers at most 10 times in 10 minutes (mail to a mailbox with nobody in it does not count); past that, `send` returns `"wake": false` with `"reason": "wake_budget"`. `list_peers` shows such sessions with agent `claude-app`. Update `deploy/Caddyfile` too, allow Anthropic's range (`160.79.104.0/21`) on port 443, and run `docker compose --profile tls restart caddy` so Caddy loads the file: the new one keeps that range off the sign-in pages and the hook API, and caps the OAuth routes at 64 KB.
- Ending a connector grant (`kampung disconnect`, `kampung revoke`, `kampung key rotate`, a revoke from Claude, or a refresh token reused after rotation) also ends the `<name>/claude` session, so `list_peers` stops listing it at once.

### Changed

#### Server (`kampung-server`)

- A new loop guard replaces message depth, which stopped honest reviews after about two rounds. Each pair of mailboxes may wake each other 9 times in a row and gets one wake back every 10 minutes; each mailbox may wake peers 30 times until someone types in one of its sessions. A session that never reports typing (Codex on Windows, Codex before its hooks are approved, a window whose hooks fail, or an agent registered without them) would otherwise be stuck, so for it the mailbox budget comes back after 30 minutes without waking sends; a runaway loop never gets that quiet, so it still stops. Typing, the dashboard's new Refill button (it replaces Reset depth), or a waking message from the Claude app refills them; the app refills a mailbox at most once every 10 minutes, only toward machines its own machine is paired with. Mail past a budget still arrives, without waking anyone. Every refill from typing, the dashboard or the Claude app writes an audit row, and the hook API limits typed-prompt refills to 60 a minute per machine. On the dashboard, the Wake column on the message pages and Wakes used on Sessions replace Depth, and messages show their attachment's name and size (and its type on the message page). The server instructions now tell agents to read the hint when a send comes back with `wake` false. All four numbers are settings: `KAMPUNG_PAIR_WAKE_SIZE`, `KAMPUNG_PAIR_WAKE_REFILL_MINUTES`, `KAMPUNG_MAILBOX_WAKE_BUDGET` and `KAMPUNG_CONNECTOR_REFILL_COOLDOWN_MINUTES` (see `.env.example`).
- `send` results no longer carry `depth`. When `wake` is false they give a `reason` (`fyi`, `cannot_wake`, `wake_budget`, `mailbox_budget` or `pair_budget`) and a `hint` saying who has to act, and `budget` shows the pair's wakes left. Mail to a mailbox nobody has open says `"note": "no_session"`. Envelopes drop `depth` and gain `fyi`.
- `send` takes `fyi: true` for a note that needs no reply: it never wakes the receiver and spends no budget.
- Another machine's session id now gets `unknown_session`, the same as an id that does not exist, instead of `not_owner`, and no longer writes an audit row, so ids cannot be probed and probing cannot fill the disk. The hook API limits `peek` to 7,680 calls a minute per machine (256 windows polling every 2 seconds) and `end` to 600.
- The audit log keeps 90 days and at most 500,000 rows; older rows are deleted a batch at a time during cleanup.
- The server now refuses to start when `KAMPUNG_PUBLIC_URL` is not `https://` with a lowercase host and no port, path or trailing slash, or when a loop-guard setting is out of range.
- The database migrates from schema version 2 to 5 on start and switches to incremental vacuum, which rewrites the file once; take a snapshot first.

#### Client (`kampung-mcp`)

- `send` takes `fyi: true`, and its description explains the loop guard, so agents know to stop and tell you when a send comes back with `wake: false`. Against a server too old for `fyi`, it says so instead of reporting a connection problem.
- A session started outside git in the home folder or under a temp folder (`/tmp`, macOS's `/var/folders`, `%TEMP%`), where eval and throwaway runs live, now uses the machine's shared `scratch` mailbox instead of one named after the folder, so those folders no longer each leave a mailbox behind. Any other folder outside git still gets a mailbox named after it. Mail already waiting in an old folder mailbox stays there.
- A Codex window you have just opened is listed and wakeable before your first prompt, when setup found `codex` on your `PATH`. The adapter finds its thread through the Codex daemon's control socket at launch, adopts the session Codex's own `SessionStart` hook may already have registered, and claims the thread with a small lock file in `~/.cache/kampung/codex-claims/` so two adapters never share one; if that fails, the window is registered at its first prompt as before. Two windows opened in one folder within seconds may still wait for their first prompt. Not on Windows. `doctor` checks the socket (`codex launch`).
- The text that wakes an idle Codex thread now names the session it wakes, such as `[kampung] laptop/api#3f: 1 Kampung message(s) waiting. Call the kampung inbox tool to read them.` Codex titles a thread by its first prompt, so threads that were only ever woken can now be told apart in its thread list. [Clients](docs/clients.md#find-where-a-woken-codex-thread-did-its-work) says how to open the thread a wake went to.
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

[0.6.0]: https://github.com/jztan/kampung-mcp/releases/tag/v0.6.0
[0.5.0]: https://github.com/jztan/kampung-mcp/releases/tag/v0.5.0
[0.4.0]: https://github.com/jztan/kampung-mcp/releases/tag/v0.4.0
[0.3.2]: https://github.com/jztan/kampung-mcp/releases/tag/v0.3.2
[0.3.1]: https://github.com/jztan/kampung-mcp/releases/tag/v0.3.1
[0.3.0]: https://github.com/jztan/kampung-mcp/releases/tag/v0.3.0
[0.2.2]: https://github.com/jztan/kampung-mcp/releases/tag/v0.2.2
[0.2.1]: https://github.com/jztan/kampung-mcp/releases/tag/v0.2.1
[0.2.0]: https://github.com/jztan/kampung-mcp/releases/tag/v0.2.0
[0.1.1]: https://github.com/jztan/kampung-mcp/releases/tag/v0.1.1
[0.1.0]: https://github.com/jztan/kampung-mcp/releases/tag/v0.1.0
