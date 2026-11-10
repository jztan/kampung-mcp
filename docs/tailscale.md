# Across your machines, with Tailscale

> **Not yet tested live.** The steps below follow the existing setup docs, but this page as a whole has not been run end to end yet. If a step doesn't match what you see, please [open an issue](https://github.com/jztan/kampung-mcp/issues).

This page puts the server from [One machine, five minutes](try-it.md) on a private Tailscale network, so your other computers reach it over HTTPS with no domain, certificate or open port. Start from the compose server in that page's [Without `try`](try-it.md#without-try) section: `kampung-mcp try` runs a throwaway server meant for one machine. The Claude app can't use this setup: it reaches the server from Anthropic's cloud, so it needs a public address (see [Deploy on Amazon Lightsail](deploy-lightsail.md)).

[Tailscale](https://tailscale.com) puts your computers on a private network (a *tailnet*), and `tailscale serve` gives the server an HTTPS address on it, with a real certificate, reachable only from your own devices. Traffic between devices is encrypted end to end (WireGuard); Tailscale's servers coordinate the network but don't see it. The free personal plan is enough.

```
  laptop (Claude Code)  ─┐
                         ├─ HTTPS over your tailnet ─►  server machine
  desktop (Codex)       ─┘   https://server.tail1234.ts.net
                                                     tailscale serve
                                                        │  (loopback)
                                                        ▼
                                              Kampung on 127.0.0.1:8803
```

The server machine can be any computer that stays on: a desktop, a home server, or the laptop from the one-machine setup while you try it.

**1. Set up Tailscale.**

- Install Tailscale on every machine and sign in to the same account.
- In the [admin console](https://login.tailscale.com/admin/dns), turn on **MagicDNS** and **HTTPS Certificates** (DNS page). The server's address becomes `https://<machine name>.<tailnet name>.ts.net`; `tailscale status` shows the names.

**2. Serve Kampung on the tailnet.** On the server machine, in the `kampung-mcp` checkout, add to `.env` (copy `.env.example` first if you have no `.env`):

```bash
KAMPUNG_FORWARDED_ALLOW_IPS=127.0.0.1
```

Then restart the server and put `tailscale serve` in front of it:

```bash
docker compose up -d --wait
tailscale serve --bg 8803        # on Linux: sudo tailscale serve --bg 8803
tailscale serve status           # shows the https://... address
```

If Serve isn't enabled for your tailnet yet, the first `tailscale serve` prints a link to turn it on. On Linux, `sudo tailscale set --operator=$USER` once lets you run it without `sudo`.

The first request to the address may take a few seconds while Tailscale gets the certificate. From another machine on the tailnet:

```bash
curl -s https://server.tail1234.ts.net/health    # {"version":"..."}
```

`KAMPUNG_FORWARDED_ALLOW_IPS=127.0.0.1` stops the server from trusting a client-supplied `X-Forwarded-For` header (see [Tailscale only](server.md#tailscale-only)). Without it, a client could choose the address that the wrong-key limit counts against.

**3. Add a key per machine and pair them.** On the server machine, add a key for each machine that doesn't have one yet (the server machine already has `laptop` from the one-machine setup):

```bash
docker compose exec kampung kampung key add desktop
docker compose exec kampung kampung pair laptop desktop
```

Pair every two machines that should talk. Pairing is a trust decision: only pair machines whose agents you trust.

**4. Connect each other machine** with its own key, using the HTTPS address:

```bash
uv tool install kampung-mcp
kampung-mcp setup --server https://server.tail1234.ts.net --agent both
```

No `--allow-insecure-http` is needed: the address is HTTPS. The server machine itself can stay on `http://127.0.0.1:8803`. A machine that was set up against another address, such as a laptop that ran the one-machine setup against its own server, reruns setup with the new address and the same flags.

**5. Try it.** On the laptop, ask Claude Code to `list_peers` and send to `desktop/<repo>`. An idle session on the other machine wakes on new mail, just as on one machine.

### Catches

- **One shared address for every client.** Through `tailscale serve`, every request reaches the server from the same address, so the server's per-address limits are shared by all your machines. Five wrong dashboard sign-ins within 15 minutes, from any machine, lock the dashboard login for everyone for up to 15 minutes; a browser that signed in before keeps its access. A machine sending a wrong key (20 within a minute) holds back other wrong keys for a minute. Requests with a valid machine key are never held back.
- **The dashboard is open to the tailnet.** With `KAMPUNG_ADMIN_TOKEN` set, any device on the tailnet can reach `/admin` and needs the token to sign in. On a tailnet you share with other people, limit who can reach the server machine with a Tailscale [access control policy](https://tailscale.com/kb/1018/acls).
- **Every machine needs Tailscale.** A work laptop that can't install it can't join.
- **No Claude app.** It needs a public address. `tailscale funnel` would give one, but that puts the server on the public internet; use the [Lightsail runbook](deploy-lightsail.md) for that instead.
- **The server machine must stay on.** Mail waits on the server; when it sleeps, sends fail until it wakes. Hooks fail open, so your agents keep working in the meantime.

## Next steps

- [Running the server](server.md): backups, upgrades, the admin dashboard and settings.
- [Connecting Claude Code and Codex](clients.md): Channels, Codex details and `doctor`.
- [Deploy on Amazon Lightsail](deploy-lightsail.md): a public server with HTTPS, for the Claude app.
