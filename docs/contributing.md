# Contributing Guide

Thanks for your interest in Kampung. This guide covers how to set up a checkout, what the checks expect, and how a change gets merged.

## Ways to contribute

- **Report bugs**: open an issue with the version (`/health` returns it), what you ran and what happened
- **Suggest features**: open an issue describing the problem before proposing a fix
- **Fix issues**: send a pull request for a bug fix
- **Improve docs**: README, `SECURITY.md` and this guide
- **Write tests**: especially for the hook and adapter paths, which run on every machine

Security problems are not issues: report them privately as described in [SECURITY.md](../SECURITY.md).

## Before you start

For features and non-trivial changes, open an issue first so the approach can be agreed before you write code. Small bug fixes and doc typos can skip this.

Some things are settled for v1 and a pull request that changes them will likely be declined without a new, concrete reason:

- One owner per server. There is no tenant column; two people run two containers.
- Mailboxes are `machine/repo`, and mail is delivered on read. There is no `ack`.
- Machines can do nothing until they are paired, and the sender is always stamped from the machine key, never read from the request.
- Peer messages are information, not instructions. The envelope says so on every message.
- Hooks are fast and fail open: stdlib only, a 2 second timeout, and exit 0 on any failure.
- No end-to-end encryption (it would rule out server-side pairing and audit), and no message broker exposed to clients.

## Contributor License Agreement

Before a pull request can be merged, its author signs the [Contributor License Agreement](../CLA.md) once, by commenting on the pull request with the sentence the CLA bot asks for. You keep the copyright in your work.

## Development setup

You need Python 3.12, [uv](https://docs.astral.sh/uv/) and Git. Docker with Compose is only needed for the Docker tests.

```bash
# Fork on GitHub first
git clone https://github.com/YOUR_USERNAME/kampung-mcp.git
cd kampung-mcp

uv sync                          # creates .venv and installs both packages
uv run pre-commit install        # black, flake8 and mypy on every commit
uv run pytest                    # should pass before you change anything
```

Run everything through `uv run` from the repository root so it uses the project's `.venv`.

## Where things live

The repository is a monorepo with two packages and a separate license for each.

### `packages/server` (`kampung-server`, AGPL-3.0)

The FastMCP server, Streamable HTTP at `/mcp`, the hook API at `/v1`, SQLite storage and the admin dashboard.

| Module | What it holds |
|---|---|
| `app.py` | Assembles the server: auth, MCP tools, hook API and health |
| `tools.py` | The MCP tools agents see: `list_peers`, `send`, `inbox` |
| `hook_api.py` | The `/v1` hook API (register session, human turn, peek, end session) |
| `mail.py` | Sending and reading mail, and the loop guard |
| `sessions.py` | Session registration, revival, resolution and liveness |
| `addressing.py` | Machine and repo names, and mailbox addresses |
| `auth.py`, `keys.py`, `keyindex.py` | Machine keys and request authentication |
| `limits.py`, `ratelimit.py`, `throttle.py` | Size and rate limits |
| `admin.py` | The one service module behind every admin write, shared by the CLI and dashboard |
| `admin_web/` | The dashboard: views, actions, templates and `admin.css` under `static/` |
| `cli.py` | The `kampung` admin CLI |
| `db.py`, `schema.sql` | SQLite connections, schema and migrations |
| `sweep.py` | Expiring mail and ending long-idle sessions |

### `packages/client` (`kampung-mcp`, Apache-2.0)

The per-session stdio adapter and the hooks for Claude Code and Codex.

| Module | What it holds |
|---|---|
| `adapter.py` | The stdio MCP adapter: adds the machine key and session identity, forwards to the server, Channels push |
| `hook.py` | The hook script for Claude Code and Codex. **Standard library only** (a test enforces this) |
| `setup.py` | `kampung-mcp setup`: writes the config and hook entries |
| `cli.py` | The `kampung-mcp` command: run the adapter or set up a machine |

## Development workflow

### 1. Create a branch

Branch from `develop`, which is the default branch. Branch names must start with `feature/`, `release/` or `hotfix/`:

```bash
git checkout develop
git pull
git checkout -b feature/short-description
```

### 2. Make changes and add tests

Tests live in `packages/server/tests` and `packages/client/tests`.

```bash
uv run pytest                                      # everything except Docker tests
uv run pytest packages/client                      # client only
uv run pytest -q packages/server/tests/test_send.py
KAMPUNG_DOCKER_TESTS=1 uv run pytest -m docker     # builds the image and drives compose
```

A bug fix should come with a test that fails without it.

### 3. Run the checks

These are the same checks CI runs:

```bash
uv run black --check .
uv run flake8 packages scripts
uv run mypy packages/server/src packages/client/src
bash scripts/audit.sh                              # pip-audit on the locked dependencies
uv run pytest -q
```

Style: Black with line length 88, flake8 with `--max-line-length=88`, mypy strict, type hints everywhere.

### 4. Commit

Use [Conventional Commits](https://www.conventionalcommits.org/) with the package as the scope where it fits:

```text
fix(server): refuse to pair a revoked machine
feat(client): wake idle Codex sessions through codex queue
docs: describe the upgrade path
```

### 5. Open a pull request

Push your branch and open a pull request against `develop`. Say what changed and why, link the issue (`Fixes #N`), and list the commands you ran.

## Pull request checklist

- [ ] Linked to an issue, or a small fix that doesn't need one
- [ ] The checks in step 3 pass
- [ ] `CHANGELOG.md` has a bullet under `[Unreleased]` for anything a server operator or agent user would notice. No entries for tests, CI or refactors
- [ ] No version bumps: `pyproject.toml`, `server.json` and changelog headers are set by the release script
- [ ] README stays short; detail goes in its own doc
- [ ] If `limits.py` changed, the Limits table in `SECURITY.md` matches
- [ ] If an admin template or `admin.css` changed, it was checked in a browser (see below)

## Admin dashboard changes

The admin tests only check HTML text, so they never catch layout bugs. Before calling a dashboard change done:

- Seed awkward data: long machine and repo names, Windows paths, an 8 KB message body with no spaces, and dozens of rows.
- Check widths 1280, 861, 860 (the breakpoint), 570, 375 and 320 px, in light and dark mode.
- Use always-visible scrollbars (macOS: "Show scroll bars: Always"), since overlay scrollbars hide overflow bugs.
- Pass means no sideways scroll on the page, nothing past the right edge, no stray vertical scrollbar and no clipped text. Tables scrolling inside their own panel is fine.
- The server reads `admin.css` at startup, so restart it after editing the CSS.

## Review

The maintainer reviews pull requests and may ask for changes. Once CI and the CLA check pass and the review is done, the branch is merged into `develop` with a merge commit. Releases are cut from `develop` by the maintainer.

## Releases

To cut a release from `develop`, run `uv run python scripts/release.py minor` (or `patch`, `major`, or `--version X.Y.Z` for an exact version); add `--dry-run` first to preview it and save a release-notes draft you can edit. A version tag publishes the client to PyPI and the MCP Registry and the server image to GHCR.

## Code of Conduct

This project follows the [Contributor Covenant](../CODE_OF_CONDUCT.md). By taking part you agree to uphold it.

## Questions

Open an issue if something in this guide is unclear or out of date.
