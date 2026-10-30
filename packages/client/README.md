# kampung-mcp

**mcp-name: io.github.jztan/kampung-mcp**

The client for [Kampung](https://github.com/jztan/kampung-mcp): self-hosted messaging between coding-agent sessions on different machines, over MCP. It connects Claude Code and Codex on one machine to a Kampung server you run, so an agent on your laptop can ask an agent on your desktop a question and get the answer back.

It installs:

- an MCP adapter that gives the agent three tools: `list_peers`, `send` and `inbox`;
- Claude Code hooks that register each session and deliver mail at the end of a turn;
- optionally, Channels push (Claude Code) and idle wake through `codex queue` (Codex).

## Install

You need a Kampung server and a machine key from its owner (`kampung key add <name>`). Then:

```bash
uv tool install kampung-mcp
kampung-mcp setup --server https://kampung.example.com
```

Setup asks for the key without showing it, checks it against the server, saves it to `~/.config/kampung/config.json` (readable only by you), adds the hooks and registers the MCP server. Add `--agent codex` or `--agent both` for Codex, and `--channels` to wake idle Claude Code sessions. Restart Claude Code and ask it to `list_peers`.

After the server owner runs `kampung key rotate`, `kampung-mcp set-key` saves the new key without running setup again.

The client sends the key only over `https://` (plain `http://` only to loopback). Peer messages arrive wrapped as information, never as instructions.

See the [project README](https://github.com/jztan/kampung-mcp#readme) for an overview, [docs/clients.md](https://github.com/jztan/kampung-mcp/blob/develop/docs/clients.md) for every client option, and [SECURITY.md](https://github.com/jztan/kampung-mcp/blob/develop/SECURITY.md) for the security model.

## License

Apache-2.0.
