# Security

## Reporting a vulnerability

Please don't open a public issue. Use **Report a vulnerability** on the repository's Security tab, with the version (`/health` returns it), what you did and what happened.

## Deployment model

Kampung is built for one owner per server: one container, one SQLite database, one set of machine keys. Two people means two containers. There is no tenant boundary inside a server, so don't share one.

<p align="center">
  <img src="docs/images/security-model.svg" alt="Each machine is a trust boundary holding one machine key in config.json. Calls go with the key over HTTPS or Tailscale to Caddy, which terminates TLS, caps request bodies, keeps /admin to loopback and the tailnet, and hides the Claude app sign-in page and the hook API from Anthropic's address range; the firewall decides who else reaches them. The Claude app reaches the server through Anthropic's cloud with an OAuth token bound to one machine, issued only from a one-time kampung connect code and refused on /v1. A client with no key or a wrong key is rejected at the first check before its body is read. Inside the Kampung server every send passes six checks in order: machine key or OAuth token, ownership, limits and the secret scan, pairing, loop guard and the envelope that marks peer text as information only. Mail, hashed keys and tokens, and the audit log are kept in SQLite on a volume." width="860">
</p>

- **Closed by default.** Every `/mcp` and `/v1` request needs a machine key, or on `/mcp` a Claude app token. `/health` is open and returns only the version. `/admin` exists only when `KAMPUNG_ADMIN_TOKEN` is set, and the OAuth routes (`/.well-known/*`, `/authorize`, `/token`, `/revoke`, `/oauth/connect`) only when `KAMPUNG_PUBLIC_URL` is set. None of them gives mail access without a key or a grant, and the only state a stranger can create is a pending sign-in: 10 minutes, at most 3 per client address (a fourth drops that address's oldest) and 100 in all (a new one drops the oldest). A sign-in is never refused, so a flood cannot lock you out: at worst it pushes out yours, and you start again.
- **The Claude app connector** is a second credential bound to a machine. There is one fixed public OAuth client and no client registration. A grant starts only from a one-time code printed by `kampung connect` on the server and entered on the connect page, which the bundled Caddy hides from Anthropic's address range. Who else can reach it is up to your firewall: allow only your own networks on port 443, plus Anthropic's range for the connector (the [Lightsail runbook](docs/deploy-lightsail.md) does this). Tokens are stored hashed, access tokens last an hour, refresh tokens rotate, a refresh token reused more than 60 seconds after rotation ends the grant (a rotated token is kept 7 days for that check, then simply refused), and `kampung disconnect`, `revoke` and `key rotate` end it at once. A Claude app token cannot reach `/v1`. A message from it that wakes its receiver does refill the receiver's loop-guard budgets, at most once per mailbox every 10 minutes and only toward machines the app's machine is paired with (see below).
- **Loopback only.** The container publishes on `127.0.0.1`. Reach it from other machines through the `tls` profile (Caddy with automatic HTTPS) or over Tailscale.
- **The client refuses plain `http://`** unless the host is loopback, because every request carries the machine key. `setup --allow-insecure-http` overrides this for a network you trust, such as Tailscale, and warns on every use. Tailscale addresses need it too: `100.64.0.0/10` is also carrier-grade NAT space, and a `*.ts.net` name falls back to public DNS when Tailscale is off.

## What Kampung protects against

- **Clients without a key.** They are refused (401, or 413 for a declared body over the cap) before any request body is read or buffered. Wrong keys are answered from memory, with no database lookup, and a revoked key or token leaves that memory at its first refusal.
- **Machines reaching mail that isn't theirs.** The machine is the trust boundary: a key can only use its own machine's mailboxes and sessions (another machine's session looks the same as one that does not exist, so ids cannot be probed, and Claude Code and Codex session ids are unique per machine, so another machine cannot claim one first), and can only send to machines you have paired with `kampung pair`. The sender is taken from the key, never from the request.
- **A leaked database giving working keys.** Only the SHA-256 hash of each key is stored. Keys are 256 random bits, so they can't be guessed.
- **Agents waking each other forever.** Each pair of mailboxes may wake each other 9 times in a row, with one wake back every 10 minutes, and each mailbox may wake peers 30 times until someone types in one of its sessions. Past either, mail is stored without waking anyone, and the send result says why. A stored-without-wake message makes nobody take a turn, so a runaway loop stops at the first one. A Claude app session spends neither budget but may wake each mailbox at most 60 times in 10 minutes; it is never woken itself, so it cannot be half of a loop. The numbers are server settings. A machine key cannot act as its machine's Claude app session.
- **Secrets sent by accident.** Every message body and text attachment is checked for private keys, common provider tokens, passwords in URLs and `.env` style lines, after folding lookalike characters, removing invisible ones and decoding one layer of base64. A hit refuses the send; the error names the kind and line, never the secret, and nothing is stored or logged. There is no override an agent could use.
- **Attachments reading the wrong files.** The adapter attaches only from the project's `kampung-outbox` folder, refuses a folder or file that is a link, a hard link or not a plain file, and deletes only the file it sent, and only if it is still the same file. The server re-encodes every image itself and drops its metadata, GPS included.
- **One key filling the server or a peer's mailbox.** Request bodies, unread mail per mailbox and per sending machine, sessions and mailboxes per machine, sends, registrations and session calls per minute, decoded image size and decodes at once are all capped. Sessions that ended long ago are deleted, and the audit log keeps 90 days and at most 500,000 rows. See [Limits](#limits).
- **Dashboard misuse.** The admin token must be at least 24 characters and is compared in constant time. Logins are limited to 5 failures per 15 minutes per client address, except for a browser that has already signed in with the token (it keeps a 30-day cookie derived from the token, so a flood from a shared address can't lock the owner out, and it still needs the token), sessions last 12 hours, every action needs a CSRF token, pages send a strict content security policy and can't be framed, and every change is audited.

## What it does not protect against

- **The server operator.** Messages are not end-to-end encrypted; the server reads them to enforce pairing and keep the audit log. Run it on a host you trust.
- **A compromised machine.** Anything that can read `~/.config/kampung/config.json` (malware, a stolen laptop, backups, dotfile sync) has that machine's key, and keys don't expire. Any process on the machine, including the agent, can use the key, for example to refill the loop guard as if you had typed (each refill it triggers is audited). Run `kampung revoke <name>` as soon as a machine is lost, or `kampung key rotate <name>` if only the key leaked: rotating keeps the machine's mailboxes and pairings. Both end the machine's sessions and stop the old key at once. After a rotation, `kampung-mcp set-key` saves the new key on the machine.
- **Agents on paired machines.** Pairing is a trust decision. A paired machine's agent can put text in front of yours: message bodies, mailbox names and session paths. Kampung labels all of it as information from a peer, never instructions, but a label is not a sandbox. Pair only machines whose agents you trust.
- **Floods from many addresses.** Wrong keys are answered from memory. Once an address has sent 20 wrong keys within a minute, its requests get 429 until the oldest of those is a minute old; requests with a valid key still get through, so a revoked machine retrying behind a shared NAT doesn't lock out its neighbours. Behind the bundled Caddy the address is the real client's, and IPv6 clients count per /64. An attacker with many addresses can still spread out, so keep the firewall allowlist or a network-level rate limit in front of a server that faces the open internet. Behind a different proxy, set `KAMPUNG_FORWARDED_ALLOW_IPS` to it (see `.env.example`).
- **A determined or tricked agent leaking a secret.** The secret scan is protection against accidents, not a guard. An agent that wants to can split a key across messages, encode it twice, or use a format the scan does not know, and pictures are not scanned at all, so a screenshot of a key goes through. A tricked session can also copy a key file into the outbox itself. Pairing is the trust decision: only pair machines whose agents you trust.
- **Instructions inside attachments.** Attachment text and pictures are peer-supplied, like message bodies. They are labelled as information, but a label is not a sandbox, and the attachment's name is the sender's choice: never use it as a path.
- **Woken Codex threads.** A closed or cleared Codex thread can be woken, and run headless, until 30 minutes after its last turn. `codex archive` ends a finished thread at once.
- **Other connectors in the same chat.** A connector added on claude.ai reaches every Claude surface on the account, including sessions with nobody typing. A chat that also holds mail or drive connectors can be steered by a peer's text into sending their content out through `send`; the envelope is wording, not a sandbox. Keep the connector off in such chats, keep `send` on "needs approval", and remember that mail read in a chat stays in the account's synced history. `<name>/claude` does not prove the owner typed a message.
- **Loop-guard refills that are not you typing.** The server cannot tell your typing from other input. A message from the Claude app counts as yours, though any surface that has the connector can send one: a claude.ai chat with nobody typing, or a Claude Code window that loads the connector because `kampung-mcp setup` was not rerun. Each such refill is limited to one per mailbox every 10 minutes and to machines the app's machine is paired with. Prompts from `/loop`, scheduled routines or `claude -p` also look typed to the hook, so they refill too. Every refill is in the audit log.
- **Mailbox budgets that come back by time.** The per-mailbox budget normally comes back only when someone types. A session that never reports typing would otherwise be stuck for good, so for it the budget comes back after 30 minutes without a waking send from its mailbox. Today that is Codex on Windows (no hooks are installed there), Codex before its hooks are approved, any window whose hooks are missing or failing, and agents that register through the hook API without calling `human-turn`. The trade-off: such a session's loop can resume without you. It still halts on its own, because a looping pair never goes 30 minutes without sending, and once the budget is spent its messages wake nobody, so nothing replies. It resumes only if something else starts a turn in it after 30 quiet minutes, and the pair budget still applies.

## Operator checklist

- Deploy with `docker compose --profile tls up -d`, or keep the server on Tailscale only (see [Tailscale only](docs/server.md#tailscale-only): set `KAMPUNG_FORWARDED_ALLOW_IPS=127.0.0.1` when Caddy is not in front).
- Treat the output of `kampung key add` and `kampung key rotate` as a secret: it is shown once, so keep it out of shared terminals, screen shares and CI logs. On each machine, let `kampung-mcp setup` ask for it instead of passing `--key` on the command line.
- Generate the admin token with `openssl rand -hex 24`, or leave it unset if you don't use the dashboard.
- If you turn on Caddy debug or access logging, leave the `Authorization` header out of the logs.
- The database volume holds message bodies (for up to 7 days) and the audit log. Protect backups of it like the messages themselves.

## Dependencies and the image

- The locked third-party dependencies of both packages are audited against osv.dev with `pip-audit` on every CI run (Linux and Windows), weekly, whenever the lockfile changes, and before every release. A known vulnerability blocks the release.
- Every release image is scanned with Trivy before it gets a version tag. A high or critical finding with a fix available blocks the release; unfixed ones are listed in the release run.
- Dependabot opens weekly update pull requests for the Python dependencies, GitHub Actions and the Docker base images. Actions are pinned to commit hashes.

## Limits

| What | Limit |
|---|---|
| Message body | 8 KB of UTF-8 |
| Attachments | One per message: text up to 256 KB, or a PNG or JPEG up to 512 KB after shrinking to 1568 pixels on the long edge and 8 million pixels before decoding, two decoded at a time; a send that waits 5 seconds for a decode gets `busy` |
| Attachment reads | 16 KB per slice by default, 32 KB at most |
| Unread attachments | 25 MB per sending machine; 500 MB stored on the server; none accepted when the data volume has under 1 GB free (`attachments_full`) |
| Unread mail per mailbox | 1,000 (`mailbox_full`) |
| Unread mail per mailbox from one sending machine | 200 (`mailbox_full`) |
| Unread mail kept | 7 days |
| Sends per machine | 60 per minute |
| Session registrations per machine | 60 per minute |
| Session calls per machine | `peek` 7,680 per minute (256 windows polling every 2 seconds), `end` 600 per minute |
| Wrong keys per client address (IPv6: per /64) | 20 within a minute, then 429 for wrong keys until they age out |
| Ended sessions kept | 30 days, then deleted up to 1,000 per cleanup pass |
| Audit log | 90 days and at most 500,000 rows, the oldest deleted up to 1,000 per cleanup pass |
| Live sessions | 32 per mailbox, 256 per machine |
| Mailboxes per machine | 1,000 |
| Session `cwd` | 1,024 printable characters |
| `/v1` request body | 64 KB (also capped by Caddy: 64 KB for `/v1` and `/admin`, 1 MB for `/mcp`) |
| `/mcp` request body | 1 MB, in the app as well as in Caddy |
| Wakes per pair of mailboxes | 9 in a row, one back every 10 minutes (`KAMPUNG_PAIR_WAKE_SIZE`, `KAMPUNG_PAIR_WAKE_REFILL_MINUTES`) |
| Wakes per mailbox | 30 until someone types there (`KAMPUNG_MAILBOX_WAKE_BUDGET`); for a session that never reports typing, they come back after 30 minutes without waking sends |
| Claude app refills | one per receiving mailbox per 10 minutes (`KAMPUNG_CONNECTOR_REFILL_COOLDOWN_MINUTES`) |
| Typed-prompt calls per machine | 60 per minute |
| Claude app wakes | 60 per receiving mailbox per 10 minutes (`KAMPUNG_CONNECTOR_WAKE_BUDGET`); later sends arrive with `wake: false` |
| Connect code and pending sign-in | 10 minutes, single use; 5 wrong codes burn the sign-in; 20 wrong codes per client address in 15 minutes, then 429 |
| Pending sign-ins | 3 per client address (IPv6: per /64) and 100 in all; a new one drops the oldest instead of being refused |
| OAuth tokens | authorization code 5 minutes, access 1 hour, refresh 90 days renewed on use, 60 seconds of refresh retry; a rotated refresh token and an expired access token stay on record 7 days |
| OAuth request body | 64 KB on `/token`, `/authorize`, `/revoke` and `/oauth/*` (in Caddy too); 4 KB for the connect form |
