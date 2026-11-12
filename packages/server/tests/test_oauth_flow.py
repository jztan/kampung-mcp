"""The Claude app connector end to end over real HTTP: discovery, authorize,
connect page, token, tool calls, refresh and revoke."""

import base64
import hashlib
import secrets
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from kampung_server import db
from kampung_server.admin import AdminService
from kampung_server.app import create_app
from kampung_server.clock import utcnow
from kampung_server.deps import ServerDeps
from kampung_server.oauth import CLAUDE_CALLBACK, CLIENT_ID
from kampung_server.ratelimit import RateLimiter
from server_testlib import FakeClock


class Server:
    def __init__(
        self, url: str, keys: dict[str, str], deps: ServerDeps, clock: FakeClock
    ) -> None:
        self.url, self.keys, self.deps, self.clock = url, keys, deps, clock
        self.admin = AdminService(lambda: db.connect(deps.db_path), clock.now)


def _start(tmp_path: Path, public: bool) -> Iterator[Server]:
    path = tmp_path / "kampung.db"
    conn = db.connect(path)
    db.migrate(conn)
    conn.close()
    # The MCP SDK checks code and token expiry against the real clock, so the
    # fake one starts at now and only moves forward.
    clock = FakeClock(utcnow())
    admin = AdminService(lambda: db.connect(path), clock.now)
    keys = {n: admin.add_machine(n, "admin:cli")[1] for n in ("laptop", "desktop")}
    admin.pair("laptop", "desktop", "admin:cli")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    url = f"http://127.0.0.1:{port}"
    deps = ServerDeps(
        db_path=path,
        clock=clock.now,
        limiter=RateLimiter(),
        public_url=url if public else None,
    )
    srv = uvicorn.Server(
        uvicorn.Config(create_app(deps), host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    while not srv.started:
        time.sleep(0.02)
    yield Server(url, keys, deps, clock)
    srv.should_exit = True
    thread.join(5)


@pytest.fixture
def server(tmp_path: Path) -> Iterator[Server]:
    yield from _start(tmp_path, public=True)


@pytest.fixture
def server_without_url(tmp_path: Path) -> Iterator[Server]:
    yield from _start(tmp_path, public=False)


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(43)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


def connect_code(server: Server, name: str = "phone") -> str:
    code = server.admin.connect(name, "admin:cli")
    server.admin.pair(name, "desktop", "admin:cli")
    return code


def start_authorize(server: Server, challenge: str, resource: str | None) -> str:
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": CLAUDE_CALLBACK,
        "state": "st",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": "kampung offline_access",
    }
    if resource is not None:
        params["resource"] = resource
    r = httpx.get(f"{server.url}/authorize", params=params, follow_redirects=False)
    assert r.status_code in (302, 307), r.text
    location = r.headers["location"]
    assert location.startswith(f"{server.url}/oauth/connect?request=")
    return parse_qs(urlsplit(location).query)["request"][0]


def post_code(server: Server, request_id: str, code: str) -> str:
    r = httpx.post(
        f"{server.url}/oauth/connect",
        data={"request": request_id, "code": code},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    location = r.headers["location"]
    assert location.startswith(f"{CLAUDE_CALLBACK}?")
    query = parse_qs(urlsplit(location).query)
    assert query["state"] == ["st"]
    return query["code"][0]


def exchange(server: Server, code: str, verifier: str) -> httpx.Response:
    return httpx.post(
        f"{server.url}/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CLAUDE_CALLBACK,
            "client_id": CLIENT_ID,
            "code_verifier": verifier,
            "resource": f"{server.url}/mcp",
        },
    )


def refresh(server: Server, token: str) -> httpx.Response:
    return httpx.post(
        f"{server.url}/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": token,
            "client_id": CLIENT_ID,
        },
    )


def connect(server: Server, name: str = "phone") -> dict[str, Any]:
    code = connect_code(server, name)
    verifier, challenge = pkce()
    request_id = start_authorize(server, challenge, f"{server.url}/mcp")
    auth_code = post_code(server, request_id, code)
    r = exchange(server, auth_code, verifier)
    assert r.status_code == 200, r.text
    tokens: dict[str, Any] = r.json()
    return tokens


@asynccontextmanager
async def client_for(server: Server, token: str, **extra: str) -> AsyncIterator[Client]:
    headers = {"Authorization": f"Bearer {token}", **extra}
    async with Client(
        StreamableHttpTransport(f"{server.url}/mcp", headers=headers)
    ) as c:
        yield c


def probe(server: Server, token: str | None = None) -> httpx.Response:
    """What Claude's client sends first: a POST to /mcp. GET is 405 regardless."""
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "probe", "version": "0"},
        },
    }
    return httpx.post(f"{server.url}/mcp", headers=headers, json=body)


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


def test_unset_public_url_mounts_no_oauth_routes(server_without_url: Server) -> None:
    url = server_without_url.url
    for path in (
        "/.well-known/oauth-authorization-server",
        "/authorize",
        "/oauth/connect",
        "/token",
        "/register",
    ):
        assert httpx.get(f"{url}{path}").status_code == 404, path
    assert httpx.get(f"{url}/health").status_code == 200


def test_401_points_at_resource_metadata(server: Server) -> None:
    r = probe(server)
    assert r.status_code == 401
    challenge = r.headers["www-authenticate"]
    assert 'resource_metadata="' in challenge
    metadata_url = challenge.split('resource_metadata="')[1].split('"')[0]
    metadata = httpx.get(metadata_url).json()
    assert metadata["resource"] == f"{server.url}/mcp"
    assert metadata["authorization_servers"] == [f"{server.url}/"] or metadata[
        "authorization_servers"
    ] == [server.url]


def test_metadata_has_scopes_and_no_registration(server: Server) -> None:
    metadata = httpx.get(f"{server.url}/.well-known/oauth-authorization-server").json()
    assert metadata["scopes_supported"] == ["kampung", "offline_access"]
    assert "registration_endpoint" not in metadata
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert metadata["revocation_endpoint"].endswith("/revoke")
    r = httpx.post(f"{server.url}/register", json={"redirect_uris": [CLAUDE_CALLBACK]})
    assert r.status_code == 404


def test_full_flow_issues_tokens(server: Server) -> None:
    tokens = connect(server)
    assert tokens["access_token"].startswith("kpa_")
    assert tokens["refresh_token"].startswith("kpr_")
    assert tokens["expires_in"] == 3600 and tokens["token_type"] == "Bearer"
    assert tokens["scope"] == "kampung offline_access"


async def test_full_flow_then_tools(server: Server) -> None:
    tokens = connect(server)
    desktop = register(server, "desktop", "blog")
    async with client_for(server, tokens["access_token"]) as phone:
        peers = await phone.call_tool("list_peers", {})
        mine = [p for p in peers.data["peers"] if p["mailbox"] == "phone/claude"]
        assert mine and mine[0]["sessions"][0]["agent"] == "claude-app"
    async with Client(
        StreamableHttpTransport(
            f"{server.url}/mcp",
            headers={
                "Authorization": f"Bearer {server.keys['desktop']}",
                "x-kampung-session": desktop,
            },
        )
    ) as d:
        r = await d.call_tool("send", {"to": "phone/claude", "body": "hi phone"})
        # The Claude app reads mail when its user asks; nothing to wake.
        assert r.data["wake"] is False and r.data["reason"] == "cannot_wake"
    async with client_for(server, tokens["access_token"]) as phone:
        r = await phone.call_tool("inbox", {})
        assert r.data["messages"][0]["body"] == "hi phone"
        assert r.data["messages"][0]["from"].startswith("desktop/blog#")


async def test_connector_image_is_refused_and_nothing_stored(server: Server) -> None:
    # Review Focus 4, end to end: the Claude app may attach text only.
    tokens = connect(server)
    register(server, "desktop", "blog")
    image = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 64).decode()
    async with client_for(server, tokens["access_token"]) as phone:
        r = await phone.call_tool(
            "send",
            {
                "to": "desktop/blog",
                "body": "look",
                "attachment": {"name": "s.png", "image": image, "type": "image/png"},
            },
        )
    assert r.data["error"] == "invalid_attachment"
    conn = db.connect(server.deps.db_path)
    try:
        counts = conn.execute(
            "SELECT (SELECT count(*) FROM messages), (SELECT count(*) FROM attachments)"
        ).fetchone()
    finally:
        conn.close()
    assert tuple(counts) == (0, 0)


def test_refresh_works_and_old_refresh_dies(server: Server) -> None:
    tokens = connect(server)
    r = refresh(server, tokens["refresh_token"])
    assert r.status_code == 200, r.text
    fresh = r.json()
    assert fresh["refresh_token"] != tokens["refresh_token"]
    assert probe(server, fresh["access_token"]).status_code == 200
    server.clock.advance(seconds=61)
    r = refresh(server, tokens["refresh_token"])
    # FastMCP answers invalid_grant with 401, as the MCP spec asks for dead tokens.
    assert r.status_code == 401 and r.json()["error"] == "invalid_grant"
    # Reuse past the window ends the grant: the new access token is dead too.
    assert probe(server, fresh["access_token"]).status_code == 401


def test_oauth_token_is_refused_on_v1(server: Server) -> None:
    tokens = connect(server)
    r = httpx.post(
        f"{server.url}/v1/sessions",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
        json={"agent": "other", "cwd": "x", "repo": "x", "hooks": False},
    )
    assert r.status_code == 401


async def test_session_header_is_ignored_for_connector_tokens(server: Server) -> None:
    tokens = connect(server)
    desktop = register(server, "desktop", "blog")
    async with client_for(
        server, tokens["access_token"], **{"x-kampung-session": desktop}
    ) as phone:
        r = await phone.call_tool("inbox", {})
        assert "error" not in r.data
        peers = await phone.call_tool("list_peers", {})
        phone_sessions = [
            s
            for p in peers.data["peers"]
            if p["mailbox"] == "phone/claude"
            for s in p["sessions"]
        ]
        assert len(phone_sessions) == 1


def test_wrong_pkce_and_reused_code(server: Server) -> None:
    code = connect_code(server)
    verifier, challenge = pkce()
    request_id = start_authorize(server, challenge, None)
    auth_code = post_code(server, request_id, code)
    bad = exchange(server, auth_code, "not-the-verifier")
    assert bad.status_code == 401 and bad.json()["error"] == "invalid_grant"
    good = exchange(server, auth_code, verifier)
    assert good.status_code == 200
    again = exchange(server, auth_code, verifier)
    assert again.status_code == 401 and again.json()["error"] == "invalid_grant"


async def test_origin_claude_ai_reaches_the_server(server: Server) -> None:
    tokens = connect(server)
    async with client_for(
        server, tokens["access_token"], Origin="https://claude.ai"
    ) as phone:
        await phone.ping()


def test_revoke_endpoint_ends_the_grant(server: Server) -> None:
    tokens = connect(server)
    r = httpx.post(
        f"{server.url}/revoke",
        # The SDK's revoke form has a client_secret field; a public client
        # sends it blank.
        data={
            "token": tokens["refresh_token"],
            "client_id": CLIENT_ID,
            "client_secret": "",
        },
    )
    assert r.status_code == 200, r.text
    assert probe(server, tokens["access_token"]).status_code == 401


def test_body_cap_on_token_and_authorize(server: Server) -> None:
    big = "grant_type=refresh_token&refresh_token=" + "x" * (65 * 1024)
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    for path in ("/token", "/revoke"):
        r = httpx.post(f"{server.url}{path}", content=big, headers=form)
        assert r.status_code == 413, path
    r = httpx.post(f"{server.url}/oauth/connect", content=big, headers=form)
    assert r.status_code == 413
    small = "request=x&code=" + "x" * 5000
    r = httpx.post(f"{server.url}/oauth/connect", content=small, headers=form)
    assert r.status_code == 400


def _parallel(calls: list[Any]) -> list[httpx.Response]:
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(len(calls)) as pool:
        return list(pool.map(lambda f: f(), calls))


def test_parallel_exchanges_of_one_code_issue_one_pair(server: Server) -> None:
    code = connect_code(server)
    verifier, challenge = pkce()
    request_id = start_authorize(server, challenge, None)
    auth_code = post_code(server, request_id, code)
    results = _parallel([lambda: exchange(server, auth_code, verifier)] * 4)
    assert sorted(r.status_code for r in results) == [200, 401, 401, 401]


def test_parallel_refreshes_of_one_token_return_one_pair(server: Server) -> None:
    tokens = connect(server)
    results = _parallel([lambda: refresh(server, tokens["refresh_token"])] * 4)
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    pairs = {(r.json()["access_token"], r.json()["refresh_token"]) for r in results}
    assert len(pairs) == 1


def test_parallel_refreshes_of_different_tokens_each_keep_their_retry(
    server: Server,
) -> None:
    # Two grants refreshing at the same instant must not prune each other's
    # retry entry; each one's own retry inside the window still works.
    a = connect(server, "phone")
    b = connect(server, "tablet")
    first = _parallel(
        [lambda: refresh(server, a["refresh_token"])] * 2
        + [lambda: refresh(server, b["refresh_token"])] * 2
    )
    assert all(r.status_code == 200 for r in first), [r.text for r in first]
    again = _parallel(
        [lambda: refresh(server, a["refresh_token"])]
        + [lambda: refresh(server, b["refresh_token"])]
    )
    assert all(r.status_code == 200 for r in again), [r.text for r in again]


async def test_tool_send_instruct_needs_an_owner_grant(server: Server) -> None:
    tokens = connect(server)
    register(server, "desktop", "blog")
    args = {"to": "desktop/blog", "body": "merge it", "mode": "instruct"}
    async with client_for(server, tokens["access_token"]) as phone:
        r = await phone.call_tool("send", args)
        assert r.data["error"] == "not_owner"
        server.admin.owner_add("phone", "admin:cli")
        r = await phone.call_tool("send", args)
        assert r.data["wake"] is True and r.data["to"].startswith("desktop/blog#")
    desktop = register(server, "desktop", "api")
    async with Client(
        StreamableHttpTransport(
            f"{server.url}/mcp",
            headers={
                "Authorization": f"Bearer {server.keys['desktop']}",
                "x-kampung-session": desktop,
            },
        )
    ) as d:
        r = await d.call_tool("send", {**args, "to": "phone/claude"})
        assert r.data["error"] == "not_owner"


def test_send_description_mentions_instruct() -> None:
    from kampung_server.guard import GuardSettings
    from kampung_server.tools import send_description

    text = send_description(GuardSettings())
    assert 'mode "instruct"' in text and "only when the owner" in text
