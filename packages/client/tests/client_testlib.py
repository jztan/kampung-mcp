"""Helpers shared by client tests (uniquely named; see server_testlib)."""

import json
import queue
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import uvicorn

from kampung_client import hook
from kampung_server import db
from kampung_server.admin import AdminService
from kampung_server.app import create_app
from kampung_server.clock import utcnow
from kampung_server.deps import ServerDeps
from kampung_server.ratelimit import RateLimiter


def home_env(home: Path) -> dict[str, str]:
    """The variables that pick the home folder: HOME on Unix, USERPROFILE on
    Windows, where Path.home() ignores HOME."""
    return {"HOME": str(home), "USERPROFILE": str(home)}


def set_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    for name, value in home_env(home).items():
        monkeypatch.setenv(name, value)


def dead_pid() -> int:
    """A pid that existed and has exited: what a stale session file records."""
    child = subprocess.Popen([sys.executable, "-c", ""])
    child.wait()
    return child.pid


def nearest_kept(pid: int) -> int:
    """`pid`, or on Windows its nearest ancestor that the chain keeps.

    On Windows the chain leaves out shells and the Python launchers below the
    first kept process (a venv's python.exe starts the real interpreter as its
    child), so the first entry is not simply the parent."""
    if sys.platform != "win32":
        return pid
    while True:
        info = hook._lookup(pid)
        assert info is not None, pid
        name = os.path.basename(info[2]).lower()
        if name not in hook.SHELLS and name not in hook.LAUNCHERS:
            return pid
        pid = info[0]


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


def scripted_server(peek_status: int) -> tuple[ThreadingHTTPServer, list[str]]:
    """Answers every peek with `peek_status` and every register with a session;
    records each request as "METHOD path"."""
    hits: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status: int, body: dict[str, object]) -> None:
            hits.append(f"{self.command} {self.path}")
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            self._reply(peek_status, {"error": "scripted", "hint": ""})

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self._reply(200, {"session": "2", "suffix": "cd", "mailbox": "laptop/api"})

        def log_message(self, *args: object) -> None:
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, hits


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
        env={**os.environ, **home_env(home)},
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

    def __init__(
        self,
        home: Path,
        cwd: Path,
        *flags: str,
        agent: str = "claude-code",
        path: str | None = None,
    ) -> None:
        env = {**os.environ, **home_env(home)}
        if path is not None:
            env["PATH"] = path
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "kampung_client",
                "adapter",
                "--agent",
                agent,
                *flags,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=cwd,
            env=env,
            text=True,
            bufsize=1,
        )
        self.lines: list[dict[str, object]] = []
        # A reader thread, not select(): Windows can only select on sockets.
        self._out: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._out.put(line)
        self._out.put("")  # end of output

    def send(self, message: dict[str, object]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def read(self, seconds: float, until: str | None = None) -> list[dict[str, object]]:
        """Collect messages for `seconds`, or until one with method/id `until`."""
        got: list[dict[str, object]] = []
        end = time.time() + seconds
        while time.time() < end:
            try:
                line = self._out.get(timeout=0.1)
            except queue.Empty:
                continue
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
        """End stdin like a real client, so the adapter exits on its own."""
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
            raise AssertionError("adapter did not exit after stdin closed") from None
