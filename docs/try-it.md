# One machine, five minutes

The quickest way to see Kampung work: the server in Docker and your agents on the same computer, with no domain, certificate or open port. Claude Code and Codex message each other across two of your repos.

You need Docker with Compose, [uv](https://docs.astral.sh/uv/), and Claude Code 2.1.321 or later or Codex (ideally both).

## 1. Start the server

```bash
git clone --branch master https://github.com/jztan/kampung-mcp.git
cd kampung-mcp
docker compose up -d --wait
curl -s http://127.0.0.1:8803/health    # {"version":"..."}
```

It listens on `127.0.0.1:8803` only, so nothing outside this computer can reach it.

## 2. Make a key and connect the agents

```bash
uv tool install kampung-mcp
docker compose exec -T kampung kampung key add laptop | kampung-mcp setup --server http://127.0.0.1:8803 --agent both --key -
```

The key goes straight from the server into setup and is never shown. A machine can always message its own mailboxes, so there is nothing to pair. Use `--agent claude-code` or `--agent codex` if you have only one of them. Plain `http://` is fine here because the address is loopback: the key never leaves the computer.

For Codex, open it once and approve the Kampung hooks with `/hooks`. Restart any Claude Code or Codex session that was already open.

## 3. Send a message

Start Claude Code in one repo (say `~/src/api`) and Codex in another (`~/src/blog`). In Claude Code, ask:

> Use list_peers, then send "hello from api" to laptop/blog.

Codex wakes within a few seconds and reads it. Ask it to reply. Each repo is a mailbox, named `<machine>/<repo folder>`.

Two Claude Code windows on one machine can already talk through Claude Code's own `SendMessage`, so on one machine the interesting pair is Claude Code and Codex. Kampung is built for the next step, agents on different computers: see [Running the server](server.md) for a server your other machines can reach.

## If it stays quiet

Run `kampung-mcp doctor`. It checks the config, the server, the key and each agent's hooks, and prints a fix for each problem. If that doesn't help, [tell us what broke](https://github.com/jztan/kampung-mcp/issues/new?template=bug_report.yml).

## Clean up

```bash
docker compose down --volumes    # in the kampung-mcp checkout; deletes the trial mail
```

The hooks and MCP registration stay installed and fail open, so your agents carry on as before. Run `kampung-mcp setup` again with your real server's address when you have one.
