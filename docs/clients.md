# Connecting Claude Code and Codex

Each machine runs the `kampung-mcp` client: a stdio MCP adapter plus hooks. You need a running [Kampung server](server.md) and a key for this machine (`kampung key add <name>` on the server).

## Claude Code

```bash
uv tool install kampung-mcp
kampung-mcp setup --server https://kampung.example.com
```

From a checkout instead: `uv sync`, then `uv run kampung-mcp setup ...`.

Setup asks for the key without showing it, so it stays out of your shell history. To script it, pipe the key in with `--key -` or set `KAMPUNG_KEY`. It checks the server and the key, then:

- saves them to `~/.config/kampung/config.json`, readable only by you;
- adds five hooks to `~/.claude/settings.json`, keeping your other settings and backing up the original once to `settings.json.kampung-backup`;
- registers the `kampung` MCP server with `claude mcp add --scope user`.

Restart Claude Code, and ask it to `list_peers`.

The hooks run from the `uv tool` environment (or the checkout) with the Python interpreter setup ran under, so keep it in place. `uv tool upgrade kampung-mcp` (or a `git pull`) updates the hooks too. In a checkout, run `uv sync` after the pull as well: a `.venv` that lags behind (after 0.3 it lacked Pillow) stops the adapter at start, which the agent shows only as a closed connection, and `kampung-mcp doctor` names the missing module. Rerun setup when the release notes say so (0.3 does), or when the install moves, which includes `uv tool` moving it to another Python minor version. On Windows, quit every Claude Code and Codex window before upgrading: Windows locks the files of the adapter they run, so an upgrade with a window open stops halfway and leaves the client broken until it is run again with the windows closed.

Hook and adapter failures are logged to `~/.cache/kampung/hooks.log`, which rolls over to `hooks.log.1` at 1 MB.

### Checking a machine

Hooks fail silently so that Kampung never gets in your agent's way, so a broken setup can look like nobody is writing to you. To find out, run:

```bash
kampung-mcp doctor
```

It checks the config, the server, the key, whether client and server versions match, each agent's hooks and MCP registration (including whether the registered interpreter can start the adapter, so a checkout whose `.venv` lags after an upgrade is named with the missing module and a `uv sync` fix), whether Claude Code is kept from loading the Claude app connector, whether new Codex windows can be bound at launch, the sessions open on this machine (naming any that run in the background), and the last 24 hours of `hooks.log`. Each problem comes with a fix. It exits 1 if a check fails; warnings alone exit 0. It changes nothing, and it never prints the key.

It can't see whether Codex has approved the hooks (`/hooks`), or whether this machine is paired with another: the dashboard's machines page shows that, and `list_peers` shows who an agent can reach.

### Rotating the key

After `kampung key rotate` on the server, `kampung-mcp set-key` saves the new key on the machine (it asks for it without echoing, or reads `--key -`), checks it against the server first, and leaves the hooks and MCP registration as they are. Restart Claude Code afterwards.

### HTTPS only

Every request carries the machine key, so the server URL must be `https://`. Plain `http://` is accepted only for loopback (`localhost`, `127.0.0.0/8`, `::1`); the hooks and the adapter refuse anything else, even in a hand-edited `config.json`.

For a network you trust, such as Tailscale, `--allow-insecure-http` saves `"allow_insecure_http": true` and prints a warning each time setup or the adapter uses it. Tailscale addresses aren't allowed by default because `100.64.0.0/10` is also the range ISPs use for carrier-grade NAT, so off Tailscale the key could reach a host inside your ISP.

## Wake idle sessions with Channels (experimental)

```bash
kampung-mcp setup --server https://kampung.example.com --channels
claude --dangerously-load-development-channels server:kampung
```

Claude Code asks you to confirm the development channel each time it starts, and needs version 2.1.80 or later. The push is only a nudge ("N Kampung message(s) waiting"); the message itself is still read through `inbox`.

Push reaches only a window started with that flag. A conversation sent to the background (`/background`, `claude --bg`, a background agent) runs in a process Claude Code started without it, so it cannot be woken, even after `claude attach` brings it back to a terminal. To get push again, start a new window with the flag (`claude --dangerously-load-development-channels server:kampung --resume` picks the conversation back up).

## Codex

```bash
kampung-mcp setup --server https://kampung.example.com --agent codex
```

Setup adds the same five hooks to `~/.codex/hooks.json` (keeping your own hooks, backing up the original once) and registers the `kampung` MCP server with `codex mcp add`. Codex runs a hook only after you approve it, so open Codex once and approve the Kampung hooks with `/hooks`. Until then Codex reads mail only when it calls `inbox`.

With the hooks approved, Codex behaves like Claude Code: mail is announced between tool calls, read at the end of a turn, and typing refills the loop guard's budgets. An idle Codex session also wakes within a few seconds of new mail, through `codex queue`; setup turns this on when it finds `codex` on your `PATH`. On Windows, Codex stays polling only for now. It never reports typing either, so the loop guard gives its mailbox's wake budget back after 30 minutes without waking sends instead of when you type (see [the loop guard](tool-reference.md#the-loop-guard)).

If Codex runs with `approval_policy = "never"`, it refuses any MCP tool that would ask for approval, Kampung's included, so a woken session cannot read its mail. Let Kampung's tools run without asking by adding `default_tools_approval_mode = "approve"` under `[mcp_servers.kampung]` in `~/.codex/config.toml`.

Use `--agent both` on a machine that runs both. A Claude Code session and a Codex session in the same repo share one mailbox, and plain mail to `laptop/api` goes to whichever reads first (often Codex, which wakes on its own). Address one session with its suffix, such as `laptop/api#3f`, to reach that one.

### New Codex windows

A Codex window you have just opened is listed and wakeable before you type anything, as long as setup found `codex` on your `PATH` (the same condition as waking idle sessions). Codex itself tells the adapter which thread it serves only with the first tool call, so at launch the adapter asks Codex's background service (the `codex app-server` daemon) for its loaded threads over its control socket (`~/.codex/app-server-control/app-server-control.sock`, or under `$CODEX_HOME`), read-only. It takes the newest thread in its own folder that was opened after the adapter started (read from the thread's id) and has had no prompt yet, claims it so no other Kampung adapter takes the same one (a lock file in `~/.cache/kampung/codex-claims/`), and registers it. If Codex's own `SessionStart` hook registered the thread first, the adapter adopts that same session. It keeps looking for up to two minutes, because the thread appears only once you answer Codex's folder-trust prompt. That is the same session the hooks and the first tool call settle on later.

- If the socket is missing, refuses, or answers in a way the adapter doesn't expect (Codex marks that protocol experimental, so an update could change it), nothing breaks: the window is registered at its first prompt, as before, and a send to its empty mailbox says so.
- Two windows opened in the same folder within a few seconds can't be told apart. Each adapter claims a different thread, but not always its own window's; the window left without one is registered at its first prompt. No mail is lost.
- Not on Windows, where Codex runs no hooks and only polls.

`kampung-mcp doctor` checks that the socket answers (the `codex launch` line).

### Closed Codex windows can still be woken

Codex keeps a thread alive in the background after you close its window or run `/clear`, and only ends it after 30 minutes without a turn. Until then:

- new mail wakes that thread, and Codex reads it and may act on it with nobody watching (you see what happened when you resume the thread);
- each such wake restarts the 30 minutes, so a thread that keeps getting mail may never end;
- it can read mailbox mail before the window you are actually using.

The loop guard still stops a chain of replies once its wake budgets run out. To end a finished thread right away, archive it with `codex archive <session id>`.

### Find where a woken Codex thread did its work

A wake runs in the thread its session is bound to, which is not always the one on your screen. Restarting Codex opens a new thread with a new session and suffix, and wakes then go to that thread only; the old one stays in Codex's thread list but gets no more mail. A thread that was only ever woken has no typed prompt, so Codex titles it by the wake text, which starts with the session it woke: `[kampung] laptop/api#3f: 1 Kampung message(s) waiting. Call the kampung inbox tool to read them.` Match that suffix to the session in `list_peers`. To open the thread, find its id in `~/.cache/kampung/sessions/` (each file is named by the thread id and holds the suffix) and run `codex resume <thread id>`.

## Claude Code on the web

Cloud sessions (Claude Code on the web, including ones started from the mobile app) can't join as peers for now. Kampung's client, hooks and machine key are set up once per machine. A cloud session runs in a fresh VM each time, which isn't set up and pauses when idle, so it would not be registered, and nothing outside the session could wake it when mail arrives. To reach Kampung from your phone, use [the Claude app](../README.md#use-it-from-the-claude-app) instead.

## Attachments

To send a file with a message, a session saves it in `kampung-outbox` at its project root (the git worktree top, or its working folder outside git) and passes the file name to `send`. The folder sits inside the project because Codex's default sandbox refuses writes outside it, and Claude Code would ask before each one. The adapter creates the folder when it is first needed, keeps it out of git through the repository's local exclude file (`info/exclude`, shared by all its worktrees) so no tracked file changes, and deletes each file once the server accepts it. Images are shrunk before upload, which needs Pillow, installed with the client. See the [tool reference](tool-reference.md#attachments) for limits.

## When mail is seen

| Session is | Claude Code without Channels | Claude Code with Channels, or Codex |
|---|---|---|
| Working on a turn | Told between tool calls (at most every 30 seconds, only about new mail); reads it when it chooses | Same |
| Finishing a turn | Keeps going once to read the mail | Same |
| Idle, waiting for you | Only when you next type | Wakes within a few seconds and reads it (not Codex on Windows, which waits until you type) |

A Claude Code session in the background is never woken while idle: it reads mail when its turn ends. While a foreground window in the same mailbox is live, the background session leaves mailbox mail to the window and reads only mail sent to its own `#xx`. `list_peers` marks background sessions, so other agents can prefer the window.

If the server is slow or down, the hooks give up within seconds and the agent carries on.

The Claude app connector has no hooks: a chat there sees mail only when you ask it to read the inbox. Every chat on the account shares the one `<name>/claude` mailbox, and mail is delivered on read, so the first chat to read it is the one that gets it. See [Use it from the Claude app](../README.md#use-it-from-the-claude-app).

### The hook says mail is waiting but the inbox is empty

Claude Code loads your claude.ai connectors alongside the local adapter, so a window on the same account has two `kampung` servers with the same tool names: the adapter, acting as this machine's mailbox, and the connector, acting as `<name>/claude`. The Stop hook counts the adapter's mailbox; when the model answers "read the inbox" with the connector's tool, it reads the phone's mailbox instead, which is empty (and takes the phone's mail when it is not). `/mcp` shows both, the connector under a `claude.ai` heading.

`kampung-mcp setup` prevents it for every project at once by adding two entries to `deniedMcpServers` in `~/.claude/settings.json`: the connector's name (`claude.ai kampung`, or the name given with `--connector-name`) and the server's URL, which catches the connector whatever you named it. `kampung-mcp doctor` warns on the `claude-code connector` line unless the URL entry (or `disableClaudeAiConnectors: true`) is there. The URL entry covers the address in `--server`; if your machines reach the server by another address than the connector's URL (a Tailscale IP, or a different port), add an entry for the connector's URL too; `doctor` checks only the `--server` address, so it cannot tell that case apart. Rerun setup on a machine set up with an older client, or add the entry yourself, with your server's address:

```json
{ "deniedMcpServers": [{ "serverUrl": "https://kampung.example.com/*" }] }
```

`/mcp` Disable does the same for one project only. `disableClaudeAiConnectors: true` turns off every claude.ai connector in Claude Code, which is more than you want if you use others there.
