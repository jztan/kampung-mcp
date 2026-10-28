"""Helpers shared by client tests (uniquely named; see server_testlib)."""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import uvicorn

from kampung_client import hook
from kampung_server import db
from kampung_server.admin import AdminService
from kampung_server.app import create_app
from kampung_server.clock import utcnow
from kampung_server.deps import ServerDeps
from kampung_server.ratelimit import RateLimiter


@dataclass
class LiveServer:
    url: str
    keys: dict[str, str]
    db_path: Path
    admin: AdminService
    uvicorn: uvicorn.Server


def start_server(tmp: Path) -> LiveServer:
    path = tmp / "server.db"
    conn = db.connect(path)
    db.migrate(conn)
    conn.close()
    admin = AdminService(lambda: db.connect(path), utcnow)
    keys = {n: admin.add_machine(n, "admin:cli")[1] for n in ("laptop", "desktop")}
    admin.pair("laptop", "desktop", "admin:cli")
    deps = ServerDeps(db_path=path, clock=utcnow, limiter=RateLimiter())
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    srv = uvicorn.Server(
        uvicorn.Config(create_app(deps), host="127.0.0.1", port=port, log_level="error")
    )
    threading.Thread(target=srv.run, daemon=True).start()
    while not srv.started:
        time.sleep(0.02)
    return LiveServer(f"http://127.0.0.1:{port}", keys, path, admin, srv)


def write_config(home: Path, server: str, key: str) -> None:
    d = home / ".config" / "kampung"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(json.dumps({"server": server, "key": key}))


def git_repo(parent: Path, name: str) -> Path:
    repo = parent / name
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def run_hook(
    home: Path, event: dict[str, object], *args: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, hook.__file__, *args],
        input=json.dumps(event),
        text=True,
        capture_output=True,
        env={**os.environ, "HOME": str(home)},
        timeout=20,
    )


def peer_send(live: LiveServer, to: str, body: str, depth_seen: int = -1) -> None:
    """Send `body` to `to` from a desktop/blog session, straight through the store."""
    from kampung_server import mail, sessions
    from kampung_server.models import load_machine

    conn = db.connect(live.db_path)
    try:
        desktop = load_machine(conn, "desktop")
        assert desktop is not None
        s = sessions.register(
            conn,
            desktop,
            utcnow(),
            agent="other",
            cwd="/w/blog",
            repo="blog",
            hooks=True,
        )
        if depth_seen != -1:
            conn.execute(
                "UPDATE sessions SET max_depth_seen = ? WHERE id = ?",
                (depth_seen, s.id),
            )
            s = sessions.resolve(conn, desktop, s.id, utcnow())
        mail.send(conn, s, to, body, None, utcnow())
    finally:
        conn.close()


def db_query(live: LiveServer, sql: str, *params: object) -> list[tuple[object, ...]]:
    conn = db.connect(live.db_path)
    try:
        return [tuple(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def hook_chain() -> list[list[object]]:
    """Ancestors as if this process were a hook run by its parent (the agent).

    An in-process resolver's chain starts at that same parent, so the file's
    first ancestor is the shared agent process, as in a real window.
    """
    return [list(a) for a in hook.ancestors()]


class RawMCP:
    """Speak line-delimited JSON-RPC to an adapter subprocess (stdio)."""

    def __init__(self, home: Path, cwd: Path, *flags: str) -> None:
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "kampung_client",
                "adapter",
                "--agent",
                "claude-code",
                *flags,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=cwd,
            env={**os.environ, "HOME": str(home)},
            text=True,
            bufsize=1,
        )
        self.lines: list[dict[str, object]] = []

    def send(self, message: dict[str, object]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def read(self, seconds: float, until: str | None = None) -> list[dict[str, object]]:
        """Collect messages for `seconds`, or until one with method/id `until`."""
        import select

        assert self.proc.stdout is not None
        got: list[dict[str, object]] = []
        end = time.time() + seconds
        while time.time() < end:
            ready, _, _ = select.select([self.proc.stdout], [], [], 0.1)
            if not ready:
                continue
            line = self.proc.stdout.readline()
            if not line:
                break
            msg = json.loads(line)
            got.append(msg)
            if until is not None and until in (msg.get("method"), msg.get("id")):
                break
        self.lines.extend(got)
        return got

    def initialize(self) -> dict[str, object]:
        self.send(
            {
                "jsonrpc": "2.0",
                "id": "init",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            }
        )
        reply = [m for m in self.read(15, until="init") if m.get("id") == "init"]
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return reply[0] if reply else {}

    def close(self) -> None:
        self.proc.kill()
        self.proc.wait()
