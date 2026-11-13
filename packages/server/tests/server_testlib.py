"""Helpers shared by server tests.

A uniquely named module, because `from conftest import` breaks once a second
package's conftest.py is in the same pytest run.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from kampung_server.models import Session

if TYPE_CHECKING:
    from kampung_server.deps import ServerDeps


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 10, 27, 9, 0, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs: float) -> None:
        self._now += timedelta(**kwargs)


MakeSession = Callable[..., Session]


class LiveServer:
    def __init__(self, url: str, keys: dict[str, str], deps: "ServerDeps") -> None:
        self.url, self.keys, self.deps = url, keys, deps


@contextmanager
def live_server(tmp_path: Path) -> Iterator[LiveServer]:
    """A real uvicorn server on a free port, with machines laptop and desktop
    paired, for tests that need HTTP (streams, the MCP endpoint)."""
    import socket
    import threading
    import time

    import uvicorn

    from kampung_server import db
    from kampung_server.admin import AdminService
    from kampung_server.app import create_app
    from kampung_server.clock import utcnow
    from kampung_server.deps import ServerDeps
    from kampung_server.ratelimit import RateLimiter

    path = tmp_path / "kampung.db"
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
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    while not srv.started:
        time.sleep(0.02)
    try:
        yield LiveServer(f"http://127.0.0.1:{port}", keys, deps)
    finally:
        srv.should_exit = True
        thread.join(5)


def register_session(server: LiveServer, machine: str, repo: str) -> str:
    import httpx

    r = httpx.post(
        f"{server.url}/v1/sessions",
        headers={"Authorization": f"Bearer {server.keys[machine]}"},
        json={
            "agent": "claude-code",
            "cwd": f"/home/u/{repo}",
            "repo": repo,
            "hooks": True,
        },
    )
    r.raise_for_status()
    return str(r.json()["session"])
