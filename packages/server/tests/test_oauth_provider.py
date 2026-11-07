import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from mcp.server.auth.provider import (
    AuthorizationParams,
    AuthorizeError,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from kampung_server import db, oauth_store
from kampung_server.admin import AdminService
from kampung_server.deps import ServerDeps
from kampung_server.keys import hash_key
from kampung_server.models import Machine
from kampung_server.oauth import (
    CLAUDE_CALLBACK,
    CLIENT_ID,
    SCOPES,
    KampungOAuth,
    TokenIndex,
)
from kampung_server.ratelimit import RateLimiter
from server_testlib import FakeClock

URL = "https://kampung.test"
CHALLENGE = "a" * 43
Machines = dict[str, tuple[Machine, str]]


@pytest.fixture
def deps(db_path: Path, conn: sqlite3.Connection, clock: FakeClock) -> ServerDeps:
    return ServerDeps(
        db_path=db_path, clock=clock.now, limiter=RateLimiter(), public_url=URL
    )


@pytest.fixture
def provider(deps: ServerDeps) -> KampungOAuth:
    return KampungOAuth(deps, URL)


async def _client(provider: KampungOAuth) -> OAuthClientInformationFull:
    client = await provider.get_client(CLIENT_ID)
    assert client is not None
    return client


async def pending(provider: KampungOAuth, resource: str | None = None) -> str:
    url = await provider.authorize(
        await _client(provider),
        AuthorizationParams(
            state="s",
            scopes=SCOPES,
            code_challenge=CHALLENGE,
            redirect_uri=AnyUrl(CLAUDE_CALLBACK),
            redirect_uri_provided_explicitly=True,
            resource=resource,
        ),
    )
    assert url.startswith(f"{URL}/oauth/connect?request=")
    return parse_qs(urlsplit(url).query)["request"][0]


async def connected(
    provider: KampungOAuth, machines: Machines, clock: FakeClock
) -> OAuthToken:
    request_id = await pending(provider, f"{URL}/mcp")
    url = provider.complete_connect(request_id, machines["laptop"][0].id, clock.now())
    code = parse_qs(urlsplit(url).query)["code"][0]
    client = await _client(provider)
    loaded = await provider.load_authorization_code(client, code)
    assert loaded is not None
    return await provider.exchange_authorization_code(client, loaded)


async def test_only_the_fixed_client_exists(provider: KampungOAuth) -> None:
    client = await _client(provider)
    assert [str(u) for u in client.redirect_uris or []] == [CLAUDE_CALLBACK]
    assert client.token_endpoint_auth_method == "none"
    assert client.client_secret is None
    assert client.scope == "kampung offline_access"
    assert await provider.get_client("x") is None
    with pytest.raises(RegistrationError):
        await provider.register_client(client)


async def test_authorize_redirects_to_connect_page(
    provider: KampungOAuth, conn: sqlite3.Connection, clock: FakeClock
) -> None:
    request_id = await pending(provider)
    assert oauth_store.load_request(conn, request_id, clock.now()) is not None


@pytest.mark.parametrize(
    "resource", [None, URL, f"{URL}/mcp", f"{URL}/mcp/", f"{URL}/"]
)
async def test_authorize_accepts_this_resource(
    provider: KampungOAuth, resource: str | None
) -> None:
    await pending(provider, resource)


async def test_authorize_refuses_other_resource_and_redirect(
    provider: KampungOAuth,
) -> None:
    client = await _client(provider)
    with pytest.raises(AuthorizeError) as e:
        await pending(provider, "https://other.test/mcp")
    assert e.value.error == "invalid_request"
    with pytest.raises(AuthorizeError):
        await provider.authorize(
            client,
            AuthorizationParams(
                state=None,
                scopes=None,
                code_challenge=CHALLENGE,
                redirect_uri=AnyUrl("https://evil.test/cb"),
                redirect_uri_provided_explicitly=True,
            ),
        )
    with pytest.raises(AuthorizeError):
        await provider.authorize(
            client,
            AuthorizationParams(
                state="x" * 1025,
                scopes=None,
                code_challenge=CHALLENGE,
                redirect_uri=AnyUrl(CLAUDE_CALLBACK),
                redirect_uri_provided_explicitly=True,
            ),
        )


async def test_complete_connect_issues_a_single_use_code(
    provider: KampungOAuth,
    conn: sqlite3.Connection,
    machines: Machines,
    clock: FakeClock,
) -> None:
    request_id = await pending(provider)
    url = provider.complete_connect(request_id, machines["laptop"][0].id, clock.now())
    assert url.startswith(f"{CLAUDE_CALLBACK}?")
    query = parse_qs(urlsplit(url).query)
    assert query["state"] == ["s"] and query["code"][0].startswith("kpc_")
    actions = [r[0] for r in conn.execute("SELECT action FROM audit")]
    assert "connect" in actions
    client = await _client(provider)
    loaded = await provider.load_authorization_code(client, query["code"][0])
    assert loaded is not None and loaded.code_challenge == CHALLENGE
    tokens = await provider.exchange_authorization_code(client, loaded)
    assert tokens.access_token.startswith("kpa_")
    assert tokens.refresh_token is not None and tokens.refresh_token.startswith("kpr_")
    assert tokens.expires_in == 3600 and tokens.scope == "kampung offline_access"
    with pytest.raises(TokenError) as e:
        await provider.exchange_authorization_code(client, loaded)
    assert e.value.error == "invalid_grant"
    with pytest.raises(ValueError):
        provider.complete_connect(request_id, machines["laptop"][0].id, clock.now())


async def test_access_token_carries_machine_claims(
    provider: KampungOAuth, machines: Machines, clock: FakeClock
) -> None:
    tokens = await connected(provider, machines, clock)
    access = await provider.load_access_token(tokens.access_token)
    assert access is not None
    assert access.claims == {"machine_id": machines["laptop"][0].id, "connector": True}
    assert access.scopes == ["kampung"] and access.resource == f"{URL}/mcp"
    clock.advance(hours=2)
    assert await provider.load_access_token(tokens.access_token) is None


async def test_access_token_dies_with_the_machine(
    provider: KampungOAuth, admin: AdminService, machines: Machines, clock: FakeClock
) -> None:
    tokens = await connected(provider, machines, clock)
    admin.revoke("laptop", "admin:cli")
    assert await provider.load_access_token(tokens.access_token) is None


async def test_refresh_rotates_and_renews_90_days(
    provider: KampungOAuth,
    conn: sqlite3.Connection,
    machines: Machines,
    clock: FakeClock,
) -> None:
    tokens = await connected(provider, machines, clock)
    client = await _client(provider)
    assert tokens.refresh_token is not None
    clock.advance(days=1)
    old = await provider.load_refresh_token(client, tokens.refresh_token)
    assert old is not None
    fresh = await provider.exchange_refresh_token(client, old, ["kampung"])
    assert fresh.refresh_token not in (None, tokens.refresh_token)
    assert fresh.access_token != tokens.access_token
    used = oauth_store.load_token(conn, "refresh", tokens.refresh_token, clock.now())
    assert used is not None and used[2] is not None
    assert fresh.refresh_token is not None
    row = conn.execute(
        "SELECT expires_at FROM oauth_tokens WHERE token_hash = ?",
        (hash_key(fresh.refresh_token),),
    ).fetchone()
    assert row[0].startswith("2027-01-26T09:00")


async def test_refresh_retry_inside_60s_returns_same_pair(
    provider: KampungOAuth, machines: Machines, clock: FakeClock
) -> None:
    tokens = await connected(provider, machines, clock)
    client = await _client(provider)
    assert tokens.refresh_token is not None
    old = await provider.load_refresh_token(client, tokens.refresh_token)
    assert old is not None
    first = await provider.exchange_refresh_token(client, old, ["kampung"])
    clock.advance(seconds=30)
    again = await provider.load_refresh_token(client, tokens.refresh_token)
    assert again is not None
    second = await provider.exchange_refresh_token(client, again, ["kampung"])
    assert second.access_token == first.access_token
    assert second.refresh_token == first.refresh_token
    assert second.expires_in == 3570


async def test_refresh_reuse_after_60s_kills_the_grant(
    provider: KampungOAuth,
    conn: sqlite3.Connection,
    machines: Machines,
    clock: FakeClock,
) -> None:
    tokens = await connected(provider, machines, clock)
    client = await _client(provider)
    assert tokens.refresh_token is not None
    old = await provider.load_refresh_token(client, tokens.refresh_token)
    assert old is not None
    await provider.exchange_refresh_token(client, old, ["kampung"])
    clock.advance(seconds=61)
    again = await provider.load_refresh_token(client, tokens.refresh_token)
    assert again is not None
    with pytest.raises(TokenError) as e:
        await provider.exchange_refresh_token(client, again, ["kampung"])
    assert e.value.error == "invalid_grant"
    assert oauth_store.grant_for_machine(conn, machines["laptop"][0].id) is None
    actions = [r[0] for r in conn.execute("SELECT action FROM audit")]
    assert "token_reuse" in actions


async def test_refresh_from_another_client_refused(
    provider: KampungOAuth, machines: Machines, clock: FakeClock
) -> None:
    tokens = await connected(provider, machines, clock)
    assert tokens.refresh_token is not None
    other = (await _client(provider)).model_copy(update={"client_id": "other"})
    assert await provider.load_refresh_token(other, tokens.refresh_token) is None


async def test_wrong_prefix_never_touches_the_store(
    provider: KampungOAuth, deps: ServerDeps, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> sqlite3.Connection:
        raise AssertionError("store touched")

    monkeypatch.setattr(deps, "connect", boom)
    assert await provider.load_access_token("kmp_x") is None


async def test_revoke_deletes_the_grant(
    provider: KampungOAuth,
    conn: sqlite3.Connection,
    machines: Machines,
    clock: FakeClock,
) -> None:
    tokens = await connected(provider, machines, clock)
    access = await provider.load_access_token(tokens.access_token)
    assert access is not None
    await provider.revoke_token(access)
    assert oauth_store.grant_for_machine(conn, machines["laptop"][0].id) is None
    assert await provider.load_access_token(tokens.access_token) is None


def test_token_index_knows_minted_and_reloaded_tokens(
    db_path: Path, conn: sqlite3.Connection, machines: Machines, clock: FakeClock
) -> None:
    ticks = [0.0]
    index = TokenIndex(lambda: db.connect(db_path), clock.now, lambda: ticks[0])
    assert not index.is_unknown("kpa_nope")  # nothing loaded yet: must look
    assert not index.check("kpa_nope")  # loads the index
    assert index.is_unknown("kpa_nope")
    index.add("kpa_minted")
    assert index.is_known("kpa_minted") and not index.is_unknown("kpa_minted")
    with db.transaction(conn):
        g = oauth_store.replace_grant(conn, machines["laptop"][0].id, clock.now())
        oauth_store.save_token(
            conn, g, "access", "kpa_db", "{}", clock.now() + db_hour()
        )
        oauth_store.save_token(
            conn, g, "refresh", "kpr_db", "{}", clock.now() + db_hour()
        )
    assert index.is_unknown("kpa_db")  # loaded less than a second ago
    ticks[0] = 2.0
    assert not index.is_unknown("kpa_db") and index.check("kpa_db")
    assert index.is_known("kpa_db")


def db_hour():  # type: ignore[no-untyped-def]
    from datetime import timedelta

    return timedelta(hours=1)
