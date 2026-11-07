import sqlite3
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastmcp import FastMCP
from mcp.server.auth.provider import AuthorizationParams
from pydantic import AnyUrl
from starlette.testclient import TestClient

from kampung_server import db, oauth_store
from kampung_server.deps import ServerDeps
from kampung_server.models import Machine
from kampung_server.oauth import CLAUDE_CALLBACK, CLIENT_ID, SCOPES, KampungOAuth
from kampung_server.oauth_connect import ConnectLimiter, register_connect_routes
from kampung_server.ratelimit import RateLimiter
from server_testlib import FakeClock

URL = "https://kampung.test"
Machines = dict[str, tuple[Machine, str]]


@pytest.fixture
def deps(db_path: Path, conn: sqlite3.Connection, clock: FakeClock) -> ServerDeps:
    return ServerDeps(
        db_path=db_path, clock=clock.now, limiter=RateLimiter(), public_url=URL
    )


@pytest.fixture
def provider(deps: ServerDeps) -> KampungOAuth:
    return KampungOAuth(deps, URL)


@pytest.fixture
def client(
    deps: ServerDeps, provider: KampungOAuth, clock: FakeClock
) -> Iterator[TestClient]:
    mcp = FastMCP("t")
    register_connect_routes(mcp, deps, provider, ConnectLimiter(clock.now))
    with TestClient(mcp.http_app(), base_url=URL) as c:
        yield c


@pytest.fixture
def pending(provider: KampungOAuth) -> str:
    import asyncio

    async def make() -> str:
        client = await provider.get_client(CLIENT_ID)
        assert client is not None
        url = await provider.authorize(
            client,
            AuthorizationParams(
                state="s",
                scopes=SCOPES,
                code_challenge="a" * 43,
                redirect_uri=AnyUrl(CLAUDE_CALLBACK),
                redirect_uri_provided_explicitly=True,
                resource=f"{URL}/mcp",
            ),
        )
        return parse_qs(urlsplit(url).query)["request"][0]

    return asyncio.run(make())


@pytest.fixture
def code(conn: sqlite3.Connection, machines: Machines, clock: FakeClock) -> str:
    with db.transaction(conn):
        return oauth_store.new_connect_code(conn, machines["laptop"][0].id, clock.now())


HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


def test_get_shows_the_page_with_fixed_copy_and_headers(
    client: TestClient, pending: str
) -> None:
    r = client.get(f"/oauth/connect?request={pending}")
    assert r.status_code == 200
    assert "Connect Claude" in r.text and "claude.ai" in r.text
    assert "<script" not in r.text and 'name="code"' in r.text
    for name, value in HEADERS.items():
        assert r.headers[name] == value
    csp = r.headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp and "form-action" not in csp


def test_unknown_request_is_400(client: TestClient) -> None:
    assert client.get("/oauth/connect").status_code == 400
    assert client.get("/oauth/connect?request=nope").status_code == 400
    r = client.post("/oauth/connect", data={"request": "nope", "code": "x"})
    assert r.status_code == 400 and r.headers["Cache-Control"] == "no-store"


def test_right_code_redirects_to_claude_with_a_code(
    client: TestClient,
    pending: str,
    code: str,
    conn: sqlite3.Connection,
    machines: Machines,
) -> None:
    r = client.post(
        "/oauth/connect",
        data={"request": pending, "code": code},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    location = r.headers["location"]
    assert location.startswith(f"{CLAUDE_CALLBACK}?")
    query = parse_qs(urlsplit(location).query)
    assert query["code"][0].startswith("kpc_") and query["state"] == ["s"]
    assert oauth_store.grant_for_machine(conn, machines["laptop"][0].id) is not None


def test_code_with_whitespace_still_works(
    client: TestClient, pending: str, code: str
) -> None:
    r = client.post(
        "/oauth/connect",
        data={"request": pending, "code": f"  {code}\n"},
        follow_redirects=False,
    )
    assert r.status_code == 303


def test_wrong_code_is_401_and_keeps_the_request(
    client: TestClient, pending: str, conn: sqlite3.Connection, clock: FakeClock
) -> None:
    r = client.post("/oauth/connect", data={"request": pending, "code": "wrong"})
    assert r.status_code == 401 and "Wrong code" in r.text
    assert oauth_store.load_request(conn, pending, clock.now()) is not None


def test_fifth_wrong_code_burns_the_request(
    client: TestClient, pending: str, conn: sqlite3.Connection, clock: FakeClock
) -> None:
    for _ in range(4):
        r = client.post("/oauth/connect", data={"request": pending, "code": "w"})
        assert r.status_code == 401
    r = client.post("/oauth/connect", data={"request": pending, "code": "w"})
    assert r.status_code == 400
    assert oauth_store.load_request(conn, pending, clock.now()) is None
    actions = [row[0] for row in conn.execute("SELECT action FROM audit")]
    assert actions.count("connect_burned") == 1


def test_expired_request_on_post_is_a_plain_400(
    client: TestClient, pending: str, code: str, clock: FakeClock
) -> None:
    clock.advance(minutes=11)
    r = client.post("/oauth/connect", data={"request": pending, "code": code})
    assert r.status_code == 400 and "expired" in r.text


def test_used_code_is_refused(
    client: TestClient,
    pending: str,
    code: str,
    conn: sqlite3.Connection,
    clock: FakeClock,
) -> None:
    with db.transaction(conn):
        oauth_store.take_connect_code(conn, code, clock.now())
    r = client.post("/oauth/connect", data={"request": pending, "code": code})
    assert r.status_code == 401


def test_body_over_4kb_is_400(client: TestClient, pending: str) -> None:
    r = client.post(
        "/oauth/connect",
        content=f"request={pending}&code=" + "x" * 5000,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 400


def test_address_lockout_after_twenty_failures(
    client: TestClient, provider: KampungOAuth, clock: FakeClock
) -> None:
    for _ in range(20):
        r = client.post("/oauth/connect", data={"request": "nope", "code": "w"})
        assert r.status_code == 400
    # Unknown requests do not count; wrong codes against live ones do.
    import asyncio

    async def make() -> str:
        c = await provider.get_client(CLIENT_ID)
        assert c is not None
        url = await provider.authorize(
            c,
            AuthorizationParams(
                state=None,
                scopes=None,
                code_challenge="a" * 43,
                redirect_uri=AnyUrl(CLAUDE_CALLBACK),
                redirect_uri_provided_explicitly=True,
            ),
        )
        return parse_qs(urlsplit(url).query)["request"][0]

    for i in range(20):
        request_id = asyncio.run(make())
        r = client.post("/oauth/connect", data={"request": request_id, "code": "w"})
        assert r.status_code == 401, i
    form = {"request": asyncio.run(make()), "code": "w"}
    r = client.post("/oauth/connect", data=form)
    assert r.status_code == 429
    clock.advance(minutes=16)
    form = {"request": asyncio.run(make()), "code": "w"}
    r = client.post("/oauth/connect", data=form)
    assert r.status_code == 401
