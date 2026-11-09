#!/usr/bin/env python3
"""Rehearse the upgrade from the last release to HEAD, the way an operator does it.

Starts the previous release from its published image with Docker Compose,
creates two machines, pairs them, registers a session on each and leaves
one unread message. Then it upgrades that same deployment to this checkout
by the documented path (check out the new code, build, `up -d --wait`) and
checks that nothing was lost: /health reports the new version, the schema
is current and on incremental vacuum, both keys still work and the message
arrives intact. It always tears down its containers, volume and worktree.

On success it records `upgrade-rehearsal` for HEAD (see release_checks.py).
It runs before release.py bumps the version, so /health may report the old
number for both; what proves the new code ran is that the container runs the
image just built from HEAD.
It does not exercise the `tls` profile: Caddy needs a public domain.

Usage:
    uv run python scripts/upgrade_rehearsal.py [--from vX.Y.Z] [--port 18803]
"""

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import release  # noqa: E402
import release_checks  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PROJECT = "kampung-rehearsal"
REHEARSAL_IMAGE = "ghcr.io/jztan/kampung-mcp:rehearsal"
RELEASE_TAG = re.compile(r"v(\d+)\.(\d+)\.(\d+)")
KEY_LINE = re.compile(r"kmp_[A-Za-z0-9_-]+")
BODY = "Rehearsal message: it must survive the upgrade unchanged. 0123456789"
# The schema the candidate should reach, read from its own db module.
SCHEMA_CHECK = (
    "import sqlite3; c = sqlite3.connect('/data/kampung.db'); "
    "print(c.execute('PRAGMA user_version').fetchone()[0], "
    "c.execute('PRAGMA auto_vacuum').fetchone()[0])"
)


class RehearsalError(Exception):
    """One step failed; the message says which and why."""


def previous_tag(tags: list[str]) -> str:
    releases = {}
    for tag in tags:
        found = RELEASE_TAG.fullmatch(tag)
        if found:
            releases[tag] = tuple(int(n) for n in found.groups())
    if not releases:
        raise ValueError("no vX.Y.Z release tag found")
    return max(releases, key=lambda t: releases[t])


def version_of(tag: str) -> str:
    return tag.removeprefix("v")


def parse_key(output: str) -> str:
    keys = KEY_LINE.findall(output)
    if not keys:
        raise ValueError("no machine key in the output")
    return keys[-1]


def _run(cmd: list[str], cwd: Path, env: dict[str, str] | None = None) -> str:
    done = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, env=env, check=False
    )
    if done.returncode != 0:
        raise RehearsalError(
            f"{' '.join(cmd)} failed ({done.returncode}):\n"
            f"{done.stdout[-1500:]}{done.stderr[-1500:]}"
        )
    return done.stdout


class Deployment:
    """One Compose deployment in a throwaway worktree, on a loopback port."""

    def __init__(self, folder: Path, port: int) -> None:
        self.folder = folder
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self.env = {**os.environ, "COMPOSE_PROJECT_NAME": PROJECT}

    def compose(self, *args: str) -> str:
        return _run(["docker", "compose", *args], self.folder, self.env)

    def write_env(self, image_tag: str) -> None:
        (self.folder / ".env").write_text(
            f"KAMPUNG_IMAGE_TAG={image_tag}\nKAMPUNG_HOST_PORT={self.port}\n",
            encoding="utf-8",
        )

    def image_id(self) -> str:
        """The image the last `compose build` produced."""
        return _run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", REHEARSAL_IMAGE],
            self.folder,
        ).strip()

    def running_image(self) -> str:
        """The image the kampung container is actually running."""
        container = self.compose("ps", "-q", "kampung").strip()
        return _run(
            ["docker", "inspect", "--format", "{{.Image}}", container], self.folder
        ).strip()

    def kampung(self, *args: str) -> str:
        return self.compose("exec", "-T", "kampung", "kampung", *args)

    def http(
        self, method: str, path: str, key: str = "", body: Any = None
    ) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(f"{self.base}{path}", data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if key:
            req.add_header("Authorization", f"Bearer {key}")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, None

    def version(self) -> str:
        status, body = self.http("GET", "/health")
        if status != 200 or not isinstance(body, dict):
            raise RehearsalError(f"/health answered {status}")
        return str(body.get("version", ""))

    def register(self, key: str, repo: str) -> str:
        status, body = self.http(
            "POST",
            "/v1/sessions",
            key,
            {
                "agent": "claude-code",
                "cwd": f"/rehearsal/{repo}",
                "repo": repo,
                "hooks": True,
            },
        )
        if status != 200:
            raise RehearsalError(f"registering a session answered {status}")
        return str(body["session"])

    def tool(self, key: str, session: str, name: str, args: dict[str, Any]) -> Any:
        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport

        async def call() -> Any:
            transport = StreamableHttpTransport(
                f"{self.base}/mcp",
                headers={
                    "Authorization": f"Bearer {key}",
                    "x-kampung-session": session,
                },
            )
            async with Client(transport) as client:
                return (await client.call_tool(name, args)).data

        return asyncio.run(call())


def rehearse(from_tag: str, port: int, keep: bool) -> str:
    head = release_checks.head_sha(ROOT)
    target = release.get_current_version(ROOT)
    tmp = Path(tempfile.mkdtemp(prefix="kampung-rehearsal-"))
    tree = tmp / "kampung-mcp"
    # A run that died without its teardown (closed terminal, --keep) leaves
    # its worktree and a volume that may already be upgraded: clear both.
    _run(["git", "worktree", "prune"], ROOT)
    for line in _run(["git", "worktree", "list", "--porcelain"], ROOT).splitlines():
        old = line.removeprefix("worktree ")
        if line.startswith("worktree ") and "/kampung-rehearsal-" in old:
            _run(["git", "worktree", "remove", "--force", old], ROOT)
    _run(["git", "worktree", "add", "--detach", str(tree), from_tag], ROOT)
    d = Deployment(tree, port)
    try:
        d.compose("down", "-v", "--remove-orphans")
        step(f"Starting {from_tag} from its published image")
        d.write_env(version_of(from_tag))
        d.compose("pull", "kampung")
        d.compose("up", "-d", "--wait", "kampung")
        expect(d.version() == version_of(from_tag), f"/health says {d.version()}")

        step("Seeding two paired machines, a session on each and one unread message")
        laptop = parse_key(d.kampung("key", "add", "laptop"))
        desktop = parse_key(d.kampung("key", "add", "desktop"))
        d.kampung("pair", "laptop", "desktop")
        laptop_session = d.register(laptop, "api")
        desktop_session = d.register(desktop, "api")
        sent = d.tool(
            laptop, laptop_session, "send", {"to": "desktop/api", "body": BODY}
        )
        expect(isinstance(sent, dict) and "error" not in sent, f"send gave {sent}")

        step(f"Upgrading to {head[:7]} ({target}) by the documented steps")
        _run(["git", "checkout", "--detach", head], tree)
        d.write_env("rehearsal")
        d.compose("build", "kampung")
        d.compose("up", "-d", "--wait", "kampung")

        step("Checking that nothing was lost")
        expect(d.running_image() == d.image_id(), "the container is not the new build")
        expect(d.version() == target, f"/health says {d.version()}, not {target}")
        schema = d.compose("exec", "-T", "kampung", "python", "-c", SCHEMA_CHECK)
        expect(
            schema.split() == [str(current_schema()), "2"],
            f"schema and auto_vacuum are {schema.strip()}",
        )
        for name, key in (("laptop", laptop), ("desktop", desktop)):
            status, _ = d.http("GET", "/v1/sessions/0/peek", key)
            expect(status == 404, f"{name}'s key now gets HTTP {status}")
        inbox = d.tool(desktop, desktop_session, "inbox", {})
        bodies = [m.get("body") for m in (inbox or {}).get("messages", [])]
        expect(bodies == [BODY], f"desktop's inbox holds {bodies}")
    finally:
        if keep:
            print(f"  Kept for debugging: {tree} (project {PROJECT})")
        else:
            down = subprocess.run(
                ["docker", "compose", "down", "-v", "--remove-orphans"],
                cwd=tree,
                env=d.env,
                capture_output=True,
                text=True,
                check=False,
            )
            if down.returncode != 0:
                print(
                    f"  WARNING: teardown failed; run `docker compose -p {PROJECT} "
                    f"down -v` yourself:\n{down.stderr[-500:]}"
                )
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(tree)],
                cwd=ROOT,
                capture_output=True,
                check=False,
            )
    return head


def current_schema() -> int:
    text = (ROOT / "packages/server/src/kampung_server/db.py").read_text()
    found = re.search(r"^SCHEMA_VERSION = (\d+)", text, re.M)
    if found is None:
        raise RehearsalError("cannot read SCHEMA_VERSION from db.py")
    return int(found.group(1))


def step(text: str) -> None:
    print(f"  {text}...", flush=True)


def expect(ok: bool, problem: str) -> None:
    if not ok:
        raise RehearsalError(problem)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from", dest="from_tag", help="release to start from")
    parser.add_argument("--port", type=int, default=18803)
    parser.add_argument("--keep", action="store_true", help="skip the teardown")
    args = parser.parse_args(argv)
    tags = _run(["git", "tag"], ROOT).split()
    from_tag = args.from_tag or previous_tag(tags)
    print(f"Upgrade rehearsal: {from_tag} -> HEAD")
    try:
        head = rehearse(from_tag, args.port, args.keep)
    except (RehearsalError, ValueError) as e:
        print(f"FAILED: {e}")
        return 1
    release_checks.record(ROOT, "upgrade-rehearsal", head, release_checks.today())
    print(f"Passed. Recorded upgrade-rehearsal for {head[:7]}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
