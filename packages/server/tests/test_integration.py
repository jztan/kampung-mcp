import socket
import sqlite3
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from kampung_server import db, mail
from kampung_server.admin import AdminService
from kampung_server.app import create_app
from kampung_server.clock import utcnow
from kampung_server.deps import ServerDeps
from kampung_server.ratelimit import RateLimiter
from kampung_server.mail import PEERS_KIND
from kampung_server.tools import SERVER_INSTRUCTIONS, session_id_from_header


class Server:
    def __init__(self, url: str, keys: dict[str, str], deps: ServerDeps) -> None:
        self.url, self.keys, self.deps = url, keys, deps


@pytest.fixture
def server(tmp_path: Path) -> Iterator[Server]:
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
    yield Server(f"http://127.0.0.1:{port}", keys, deps)
    srv.should_exit = True
    thread.join(5)


def register(server: Server, machine: str, repo: str) -> str:
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


@asynccontextmanager
async def client_for(
    server: Server, key: str | None, session: str | None
) -> AsyncIterator[Client]:
    headers = {}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if session:
        headers["x-kampung-session"] = session
    async with Client(
        StreamableHttpTransport(f"{server.url}/mcp", headers=headers)
    ) as c:
        yield c


async def test_two_machines_exchange(server: Server) -> None:
    a = register(server, "laptop", "api")
    b = register(server, "desktop", "blog")
    async with client_for(server, server.keys["laptop"], a) as ca:
        r = await ca.call_tool("send", {"to": "desktop/blog", "body": "hello from api"})
        assert r.data["depth"] == 0 and r.data["wake"] is True
    async with client_for(server, server.keys["desktop"], b) as cb:
        r = await cb.call_tool("inbox", {})
        msg = r.data["messages"][0]
        assert msg["body"] == "hello from api"
        assert msg["from"].startswith("laptop/api#")
        assert r.data["more"] == 0
        peers = await cb.call_tool("list_peers", {})
        assert [p["mailbox"] for p in peers.data["peers"]] == [
            "desktop/blog",
            "laptop/api",
        ]
        assert peers.data["kind"] == PEERS_KIND


async def test_session_header_spoof_is_not_owner(server: Server) -> None:
    b = register(server, "desktop", "blog")
    async with client_for(server, server.keys["laptop"], b) as c:
        r = await c.call_tool("inbox", {})
        assert r.data["error"] == "not_owner"


async def test_missing_session_header(server: Server) -> None:
    async with client_for(server, server.keys["laptop"], None) as c:
        r = await c.call_tool("inbox", {})
        assert r.data["error"] == "missing_session"
    async with client_for(server, server.keys["laptop"], "abc") as c:
        r = await c.call_tool("inbox", {})
        assert r.data["error"] == "missing_session"


def test_session_header_parser() -> None:
    assert session_id_from_header("42") == 42
    for raw in ("", "abc", "\u00b2", "\u0661\u0662", "-1", " 4"):
        assert session_id_from_header(raw) is None


async def test_locked_database_is_busy(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = register(server, "laptop", "api")

    def locked(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(mail, "inbox", locked)
    async with client_for(server, server.keys["laptop"], a) as c:
        r = await c.call_tool("inbox", {})
        assert r.data == {
            "error": "busy",
            "hint": "The server is busy. Try again in a moment.",
        }


async def test_no_key_is_rejected(server: Server) -> None:
    with pytest.raises(Exception):
        async with client_for(server, None, None):
            pass


async def test_instructions_sent(server: Server) -> None:
    a = register(server, "laptop", "api")
    async with client_for(server, server.keys["laptop"], a) as c:
        assert c.initialize_result.instructions == SERVER_INSTRUCTIONS


async def test_rate_limited_inline(server: Server) -> None:
    server.deps.limiter = RateLimiter(limit=1)
    a = register(server, "laptop", "api")
    register(server, "desktop", "blog")
    async with client_for(server, server.keys["laptop"], a) as c:
        first = await c.call_tool("send", {"to": "desktop/blog", "body": "one"})
        assert "error" not in first.data
        second = await c.call_tool("send", {"to": "desktop/blog", "body": "two"})
        assert second.data["error"] == "rate_limited"


async def test_tool_errors_are_inline(server: Server) -> None:
    a = register(server, "laptop", "api")
    async with client_for(server, server.keys["laptop"], a) as c:
        r = await c.call_tool("send", {"to": "nope", "body": "x"})
        assert r.data == {
            "error": "invalid_address",
            "hint": "Use machine/repo or machine/repo#xx; call list_peers to see "
            "addresses.",
        }


async def test_revoked_key_rejected_on_mcp(server: Server) -> None:
    a = register(server, "laptop", "api")
    async with client_for(server, server.keys["laptop"], a) as c:
        assert "peers" in (await c.call_tool("list_peers", {})).data
    admin = AdminService(lambda: db.connect(server.deps.db_path), utcnow)
    admin.revoke("laptop", "admin:cli")
    with pytest.raises(Exception):
        async with client_for(server, server.keys["laptop"], a):
            pass
    r = httpx.post(
        f"{server.url}/mcp",
        headers={
            "Authorization": f"Bearer {server.keys['laptop']}",
            "accept": "application/json, text/event-stream",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert r.status_code == 401
