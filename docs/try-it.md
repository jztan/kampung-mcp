# One machine, five minutes

The quickest way to see Kampung work: the server in Docker and your agents on the same computer, with no domain, certificate or open port. Two of your repos message each other, with one agent (two Claude Code windows, or two Codex windows) or with Claude Code and Codex.

You need Docker, [uv](https://docs.astral.sh/uv/), and Claude Code 2.1.321 or later or Codex.

## 1. Start a trial server

```bash
uv tool install kampung-mcp
kampung-mcp try
```

`try` starts the server in Docker, listening on `127.0.0.1:8803` only, makes a key for this machine without showing it, and connects whichever of `claude` and `codex` it finds on your `PATH` (or name one with `--agent claude-code`, `--agent codex` or `--agent both`). It refuses to run on a machine already connected to another Kampung server, and changes nothing there.

For Codex, open it once and approve the Kampung hooks with `/hooks`. Restart any Claude Code or Codex session that was already open.

## 2. Send a message

Start your agent in two repos (say `~/src/api` and `~/src/blog`), or Claude Code in one and Codex in the other. In the one in `api`, ask:

> Use list_peers, then send "hello from api" to <machine>/blog.

`try` printed this machine's name. Each repo is a mailbox, named `<machine>/<repo folder>`. The session in `blog` wakes within a few seconds, even while idle, and reads it. Ask it to reply.

On one machine, two Claude Code windows can also talk through Claude Code's own `SendMessage`; Kampung is built for the next step, agents on different computers. See [Running the server](server.md) for a server your other machines can reach.

## If it stays quiet

Run `kampung-mcp doctor`. It checks the config, the server, the key and each agent's hooks, and prints a fix for each problem. If that doesn't help, [tell us what broke](https://github.com/jztan/kampung-mcp/issues/new?template=bug_report.yml).

## Clean up

```bash
kampung-mcp try --stop
```

It removes the trial server and its mail, and undoes the setup `try` made: Kampung's hooks, its MCP registration and the saved key. Settings of your own in those files stay. `uv tool uninstall kampung-mcp` then removes the client.

## Without `try`

The same steps by hand, in a clone of this repository. Compose needs Docker Engine 25 or later (see [Running the server](server.md#with-docker)).

```bash
git clone --branch master https://github.com/jztan/kampung-mcp.git
cd kampung-mcp
docker compose up -d --wait
docker compose exec -T kampung kampung key add laptop | kampung-mcp setup --server http://127.0.0.1:8803 --agent both --key -
```

The key goes straight from the server into setup. A machine can always message its own mailboxes, so there is nothing to pair. Plain `http://` is fine here because the address is loopback. To clean up: `kampung-mcp uninstall`, then `docker compose down --volumes`.
