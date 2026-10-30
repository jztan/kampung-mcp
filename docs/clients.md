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

The hooks run from the `uv tool` environment (or the checkout) with the Python interpreter setup ran under, so keep it in place. `uv tool upgrade kampung-mcp` (or a `git pull`) updates the hooks too; rerun setup only if the install moves.

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

## Codex

```bash
kampung-mcp setup --server https://kampung.example.com --agent codex
```

Setup adds the same five hooks to `~/.codex/hooks.json` (keeping your own hooks, backing up the original once) and registers the `kampung` MCP server with `codex mcp add`. Codex runs a hook only after you approve it, so open Codex once and approve the Kampung hooks with `/hooks`. Until then Codex reads mail only when it calls `inbox`.

With the hooks approved, Codex behaves like Claude Code: mail is announced between tool calls, read at the end of a turn, and the loop guard resets when you type. An idle Codex session also wakes within a few seconds of new mail, through `codex queue`; setup turns this on when it finds `codex` on your `PATH`. On Windows, Codex stays polling only for now.

Use `--agent both` on a machine that runs both. A Claude Code session and a Codex session in the same repo share one mailbox, and plain mail to `laptop/api` goes to whichever reads first (often Codex, which wakes on its own). Address one session with its suffix, such as `laptop/api#3f`, to reach that one.

### Closed Codex windows can still be woken

Codex keeps a thread alive in the background after you close its window or run `/clear`, and only ends it after 30 minutes without a turn. Until then:

- new mail wakes that thread, and Codex reads it and may act on it with nobody watching (you see what happened when you resume the thread);
- each such wake restarts the 30 minutes, so a thread that keeps getting mail may never end;
- it can read mailbox mail before the window you are actually using.

The loop guard still stops a chain of replies after three. To end a finished thread right away, archive it with `codex archive <session id>`.

## When mail is seen

| Session is | Claude Code without Channels | Claude Code with Channels, or Codex |
|---|---|---|
| Working on a turn | Told between tool calls (at most every 30 seconds, only about new mail); reads it when it chooses | Same |
| Finishing a turn | Keeps going once to read the mail | Same |
| Idle, waiting for you | Only when you next type | Wakes within a few seconds and reads it |

If the server is slow or down, the hooks give up within seconds and the agent carries on.
