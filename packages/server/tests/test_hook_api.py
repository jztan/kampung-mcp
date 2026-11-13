import sqlite3
from collections.abc import Iterator
from pathlib import Path
from datetime import timedelta
from typing import Any

import pytest
from starlette.testclient import TestClient

import kampung_server
from server_testlib import FakeClock
from kampung_server.admin import AdminService
from kampung_server.app import create_app
from kampung_server.clock import utcnow
from kampung_server.deps import ServerDeps
from kampung_server.limits import AUTH_FAILURE_LIMIT, MAX_REQUEST_BYTES
from kampung_server.models import Machine, load_session
from kampung_server.ratelimit import RateLimiter

Machines = dict[str, tuple[Machine, str]]
BODY = {"agent": "claude-code", "cwd": "/home/u/api", "repo": "api", "hooks": True}


@pytest.fixture
def deps(db_path: Path, clock: FakeClock) -> ServerDeps:
    return ServerDeps(db_path=db_path, clock=clock.now, limiter=RateLimiter())


@pytest.fixture
def client(deps: ServerDeps, machines: Machines) -> Iterator[TestClient]:
    with TestClient(create_app(deps)) as c:
        yield c


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _register(client: TestClient, key: str, **kw: Any) -> str:
    r = client.post("/v1/sessions", headers=bearer(key), json={**BODY, **kw})
    assert r.status_code == 200, r.text
    return str(r.json()["session"])


def test_health_no_auth(client: TestClient) -> None:
    assert client.get("/health").json() == {"version": kampung_server.__version__}


def test_v1_requires_key(client: TestClient) -> None:
    r = client.post("/v1/sessions", json=BODY)
    assert r.status_code == 401 and r.json()["error"] == "unauthorized"
    r = client.post("/v1/sessions", json=BODY, headers=bearer("kmp_wrong"))
    assert r.status_code == 401


def test_v1_revoked_key_401(
    client: TestClient, admin: AdminService, machines: Machines
) -> None:
    admin.revoke("laptop", "admin:cli")
    r = client.post("/v1/sessions", json=BODY, headers=bearer(machines["laptop"][1]))
    assert r.status_code == 401


def test_register_then_peek(client: TestClient, machines: Machines) -> None:
    key = machines["laptop"][1]
    r = client.post(
        "/v1/sessions", headers=bearer(key), json={**BODY, "agent_session_id": "cc-1"}
    )
    assert r.status_code == 200
    data = r.json()
    assert data["mailbox"] == "laptop/api" and isinstance(data["session"], str)
    assert len(data["suffix"]) == 2
    peek = client.get(f"/v1/sessions/{data['session']}/peek", headers=bearer(key))
    assert peek.json() == {"waiting": 0, "wakeable": 0, "newest": None}


def test_register_takes_background(
    client: TestClient, machines: Machines, conn: sqlite3.Connection
) -> None:
    key = machines["laptop"][1]
    r = client.post(
        "/v1/sessions", headers=bearer(key), json={**BODY, "background": True}
    )
    assert r.status_code == 200
    row = conn.execute("SELECT background FROM sessions").fetchone()
    assert row[0] == 1
    bad = client.post(
        "/v1/sessions", headers=bearer(key), json={**BODY, "background": 1}
    )
    assert bad.status_code == 400


def test_register_takes_idle_wake(
    client: TestClient, machines: Machines, conn: sqlite3.Connection
) -> None:
    key = machines["laptop"][1]
    r = client.post(
        "/v1/sessions", headers=bearer(key), json={**BODY, "idle_wake": True}
    )
    assert r.status_code == 200
    assert conn.execute("SELECT idle_wake FROM sessions").fetchone()[0] == 1
    bad = client.post(
        "/v1/sessions", headers=bearer(key), json={**BODY, "idle_wake": 1}
    )
    assert bad.status_code == 400


def test_other_machines_session_looks_missing(
    client: TestClient, machines: Machines, conn: sqlite3.Connection
) -> None:
    sid = _register(client, machines["laptop"][1])
    desktop = bearer(machines["desktop"][1])
    missing = client.get("/v1/sessions/9999/peek", headers=desktop)
    r = client.get(f"/v1/sessions/{sid}/peek", headers=desktop)
    assert r.status_code == missing.status_code == 404
    assert r.json() == missing.json()
    r = client.post(f"/v1/sessions/{sid}/end", headers=desktop)
    assert (
        r.status_code == 404
        and r.json() == client.post("/v1/sessions/9999/end", headers=desktop).json()
    )
    denied = conn.execute("SELECT count(*) FROM audit WHERE action = 'denied'")
    assert denied.fetchone()[0] == 0


def test_unknown_session_404(client: TestClient, machines: Machines) -> None:
    r = client.get("/v1/sessions/9999/peek", headers=bearer(machines["laptop"][1]))
    assert r.status_code == 404 and r.json()["error"] == "unknown_session"
    r = client.get("/v1/sessions/abc/peek", headers=bearer(machines["laptop"][1]))
    assert r.status_code == 404


def test_human_turn_refills_pairs_and_mailbox_budget(
    client: TestClient, conn: sqlite3.Connection, machines: Machines
) -> None:
    from kampung_server import guard

    key = machines["laptop"][1]
    sid = _register(client, key)
    s = load_session(conn, int(sid))
    assert s is not None
    peer = load_session(conn, int(_register(client, machines["desktop"][1])))
    assert peer is not None
    g = guard.GuardSettings()
    now = utcnow()
    for _ in range(9):
        guard.spend_pair(conn, s.mailbox_id, peer.mailbox_id, now, g)
        guard.spend_mailbox(conn, s.mailbox_id)
    r = client.post(f"/v1/sessions/{sid}/human-turn", headers=bearer(key))
    assert r.status_code == 200 and r.json() == {}
    assert guard.pair_left(conn, s.mailbox_id, peer.mailbox_id, now, g) == 9
    assert guard.mailbox_left(conn, s.mailbox_id, g) == 30
    row = conn.execute(
        "SELECT actor, detail FROM audit WHERE action = 'refill'"
    ).fetchone()
    assert row[0] == "machine:laptop" and '"pairs": 1' in row[1]


def test_human_turn_rate_limited(client: TestClient, machines: Machines) -> None:
    key = machines["laptop"][1]
    sid = _register(client, key)
    codes = [
        client.post(f"/v1/sessions/{sid}/human-turn", headers=bearer(key)).status_code
        for _ in range(61)
    ]
    assert codes[:60] == [200] * 60 and codes[60] == 429


def test_ended_session_409_then_revive(client: TestClient, machines: Machines) -> None:
    key = machines["laptop"][1]
    sid = _register(client, key, agent_session_id="cc-1")
    assert client.post(f"/v1/sessions/{sid}/end", headers=bearer(key)).status_code == (
        200
    )
    assert client.post(f"/v1/sessions/{sid}/end", headers=bearer(key)).status_code == (
        200
    )
    r = client.get(f"/v1/sessions/{sid}/peek", headers=bearer(key))
    assert r.status_code == 409 and r.json()["error"] == "session_ended"
    assert _register(client, key, agent_session_id="cc-1") == sid
    assert client.get(f"/v1/sessions/{sid}/peek", headers=bearer(key)).status_code == (
        200
    )


def test_bad_json_400(client: TestClient, machines: Machines) -> None:
    key = machines["laptop"][1]
    r = client.post("/v1/sessions", headers=bearer(key), content=b"{not json")
    assert r.status_code == 400 and r.json()["error"] == "bad_request"
    r = client.post("/v1/sessions", headers=bearer(key), json={"agent": "codex"})
    assert r.status_code == 400 and r.json()["error"] == "bad_request"
    r = client.post("/v1/sessions", headers=bearer(key), json={**BODY, "repo": "../x"})
    assert r.status_code == 400 and r.json()["error"] == "invalid_name"


def test_register_checks_key_before_body(client: TestClient) -> None:
    r = client.post("/v1/sessions", content=b"{not json")
    assert r.status_code == 401 and r.json()["error"] == "unauthorized"


def test_register_never_reads_body_without_key(client: TestClient) -> None:
    read = []

    def chunks() -> Iterator[bytes]:
        read.append(1)
        yield b"{}"

    r = client.post("/v1/sessions", content=chunks())
    assert r.status_code == 401 and read == []


def test_register_body_over_limit_413(client: TestClient, machines: Machines) -> None:
    key = machines["laptop"][1]
    big = b" " * (MAX_REQUEST_BYTES + 1)
    r = client.post("/v1/sessions", headers=bearer(key), content=big)
    assert r.status_code == 413 and r.json()["error"] == "request_too_large"


def test_register_streamed_body_over_limit_413(
    client: TestClient, machines: Machines
) -> None:
    def chunks() -> Iterator[bytes]:
        for _ in range(MAX_REQUEST_BYTES // 1024 + 2):
            yield b" " * 1024

    key = machines["laptop"][1]
    r = client.post("/v1/sessions", headers=bearer(key), content=chunks())
    assert r.status_code == 413 and r.json()["error"] == "request_too_large"


def test_register_deep_json_400(client: TestClient, machines: Machines) -> None:
    key = machines["laptop"][1]
    r = client.post("/v1/sessions", headers=bearer(key), content=b"[" * 50_000)
    assert r.status_code == 400 and r.json()["error"] == "bad_request"


def test_another_machines_agent_session_id_does_not_block_registration(
    client: TestClient, machines: Machines
) -> None:
    theirs = _register(client, machines["desktop"][1], agent_session_id="cc-9")
    r = client.post(
        "/v1/sessions",
        headers=bearer(machines["laptop"][1]),
        json={**BODY, "agent_session_id": "cc-9"},
    )
    assert r.status_code == 200 and r.json()["session"] != theirs


def test_wrong_keys_skip_database(
    client: TestClient, deps: ServerDeps, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.post("/v1/sessions", json=BODY, headers=bearer("kmp_warm"))
    opened = []
    real = ServerDeps.connect

    def counting(self: ServerDeps) -> sqlite3.Connection:
        opened.append(1)
        return real(self)

    monkeypatch.setattr(ServerDeps, "connect", counting)
    # With the warm-up, 19 wrong keys in all: under the per-IP limit of 20.
    for i in range(9):
        r = client.post("/v1/sessions", json=BODY, headers=bearer(f"kmp_bad{i}"))
        assert r.status_code == 401
        r = client.post("/mcp", json={}, headers=bearer(f"kmp_bad{i}"))
        assert r.status_code == 401
    assert opened == []


def test_end_is_audited(
    client: TestClient, machines: Machines, conn: sqlite3.Connection
) -> None:
    sid = _register(client, machines["laptop"][1])
    r = client.post(f"/v1/sessions/{sid}/end", headers=bearer(machines["desktop"][1]))
    assert r.status_code == 404
    r = client.post(f"/v1/sessions/{sid}/end", headers=bearer(machines["laptop"][1]))
    assert r.status_code == 200
    rows = conn.execute(
        "SELECT actor, action FROM audit WHERE action IN ('denied', 'end_session') "
        "ORDER BY id"
    ).fetchall()
    assert [tuple(r) for r in rows] == [("machine:laptop", "end_session")]


def test_register_is_rate_limited(
    db_path: Path, clock: FakeClock, machines: Machines
) -> None:
    deps = ServerDeps(
        db_path=db_path,
        clock=clock.now,
        limiter=RateLimiter(),
        register_limiter=RateLimiter(limit=2),
    )
    key = machines["laptop"][1]
    with TestClient(create_app(deps)) as c:
        for _ in range(2):
            _register(c, key)
        r = c.post("/v1/sessions", headers=bearer(key), json=BODY)
        assert r.status_code == 429 and r.json()["error"] == "rate_limited"
        _register(c, machines["desktop"][1])


def test_repeated_bad_keys_from_one_ip_get_429(client: TestClient) -> None:
    codes = [
        client.post(
            "/v1/sessions", json=BODY, headers=bearer(f"kmp_bad{i}")
        ).status_code
        for i in range(AUTH_FAILURE_LIMIT + 1)
    ]
    assert codes[:-1] == [401] * AUTH_FAILURE_LIMIT and codes[-1] == 429
    assert client.get("/health").status_code == 200


def test_a_valid_key_gets_through_a_throttled_address(
    client: TestClient, machines: Machines
) -> None:
    for i in range(AUTH_FAILURE_LIMIT):
        client.post("/mcp", json={}, headers=bearer(f"kmp_bad{i}"))
    assert client.post("/mcp", json={}, headers=bearer("kmp_more")).status_code == 429
    _register(client, machines["laptop"][1])


@pytest.mark.parametrize("raw", ["9" * 19, "9" * 20, "9" * 5000, "-1"])
def test_session_id_out_of_range_is_404(
    client: TestClient, machines: Machines, raw: str
) -> None:
    headers = bearer(machines["laptop"][1])
    assert client.get(f"/v1/sessions/{raw}/peek", headers=headers).status_code == 404
    assert client.post(f"/v1/sessions/{raw}/end", headers=headers).status_code == 404


def test_v1_refuses_claude_app_agent(client: TestClient, machines: Machines) -> None:
    r = client.post(
        "/v1/sessions",
        json={**BODY, "agent": "claude-app"},
        headers=bearer(machines["laptop"][1]),
    )
    assert r.status_code == 400 and r.json()["error"] == "invalid_agent"


# A machine key must never act as its Claude app connector session: those
# sends skip the loop guard's depth check.


def _connector_id(deps: ServerDeps, machines: Machines) -> int:
    from kampung_server import db, sessions

    conn = db.connect(deps.db_path)
    try:
        return sessions.connector_session(conn, machines["laptop"][0], deps.clock()).id
    finally:
        conn.close()


def test_machine_key_cannot_revive_the_connector_session(
    client: TestClient, deps: ServerDeps, machines: Machines
) -> None:
    key = machines["laptop"][1]
    cid = _connector_id(deps, machines)
    r = client.post(
        "/v1/sessions",
        headers=bearer(key),
        json={**BODY, "agent_session_id": f"connector:{machines['laptop'][0].id}"},
    )
    assert r.status_code == 400 and r.json()["error"] == "invalid_agent_session"
    conn = sqlite3.connect(deps.db_path)
    suffix = conn.execute("SELECT suffix FROM sessions WHERE id = ?", (cid,)).fetchone()
    conn.close()
    r = client.post(
        "/v1/sessions",
        headers=bearer(key),
        json={**BODY, "repo": "claude", "suffix": suffix[0]},
    )
    assert r.status_code == 400 and r.json()["error"] == "invalid_agent_session"


def test_machine_key_cannot_use_the_connector_session(
    client: TestClient, deps: ServerDeps, machines: Machines
) -> None:
    key = machines["laptop"][1]
    cid = _connector_id(deps, machines)
    for method, path in (("GET", "peek"), ("POST", "human-turn"), ("POST", "end")):
        r = client.request(method, f"/v1/sessions/{cid}/{path}", headers=bearer(key))
        assert r.status_code == 404 and r.json()["error"] == "unknown_session", path


def test_machine_key_cannot_claim_the_connector_id_first(
    client: TestClient, machines: Machines
) -> None:
    r = client.post(
        "/v1/sessions",
        headers=bearer(machines["laptop"][1]),
        json={**BODY, "agent_session_id": "connector:999"},
    )
    assert r.status_code == 400 and r.json()["error"] == "invalid_agent_session"


def test_human_turn_refill_rolls_back_when_the_audit_fails(
    client: TestClient,
    conn: sqlite3.Connection,
    machines: Machines,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kampung_server import guard, hook_api

    key = machines["laptop"][1]
    s = load_session(conn, int(_register(client, key)))
    assert s is not None
    guard.spend_mailbox(conn, s.mailbox_id)

    def broken(*_: object, **__: object) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(hook_api.audit, "record", broken)
    with pytest.raises(sqlite3.OperationalError):
        client.post(f"/v1/sessions/{s.id}/human-turn", headers=bearer(key))
    assert guard.mailbox_left(conn, s.mailbox_id, guard.GuardSettings()) == 29
    typed = conn.execute(
        "SELECT typed_at FROM sessions WHERE id = ?", (s.id,)
    ).fetchone()[0]
    assert typed is None


def test_server_start_caps_pair_debt_to_the_settings(
    deps: ServerDeps, conn: sqlite3.Connection, machines: Machines, clock: FakeClock
) -> None:
    from kampung_server import app, guard

    slow = guard.GuardSettings(pair_refill=timedelta(minutes=1440))
    a, b = machines["laptop"][0].id, machines["desktop"][0].id
    conn.execute(
        "INSERT INTO mailboxes (id, machine_id, repo, created_at) VALUES "
        "(1, ?, 'api', '2026-10-27T09:00:00.000000Z'), "
        "(2, ?, 'blog', '2026-10-27T09:00:00.000000Z')",
        (a, b),
    )
    for _ in range(9):
        guard.spend_pair(conn, 1, 2, clock.now(), slow)
    app.cap_pair_debt(deps)
    clock.advance(minutes=10)
    assert guard.pair_left(conn, 1, 2, clock.now(), deps.guard) == 1


@pytest.mark.parametrize("method,path", [("GET", "peek"), ("POST", "end")])
def test_session_routes_are_rate_limited_per_machine(
    db_path: Path, clock: FakeClock, machines: Machines, method: str, path: str
) -> None:
    from kampung_server.deps import end_limiter, peek_limiter

    # Peek covers 256 sessions polling every 2 seconds; end has its own.
    assert peek_limiter()._limit == 7680 and end_limiter()._limit == 600
    deps = ServerDeps(
        db_path=db_path,
        clock=clock.now,
        limiter=RateLimiter(),
        peek_limiter=RateLimiter(5, timedelta(seconds=60)),
        end_limiter=RateLimiter(5, timedelta(seconds=60)),
    )
    other_method, other = ("POST", "end") if path == "peek" else ("GET", "peek")
    with TestClient(create_app(deps)) as c:
        desktop = bearer(machines["desktop"][1])
        codes = [
            c.request(method, f"/v1/sessions/9999/{path}", headers=desktop).status_code
            for _ in range(6)
        ]
        assert codes[:-1] == [404] * 5 and codes[-1] == 429
        # The other route and another machine keep their own allowance.
        r = c.request(other_method, f"/v1/sessions/9999/{other}", headers=desktop)
        assert r.status_code == 404
        laptop = bearer(machines["laptop"][1])
        r = c.request(method, f"/v1/sessions/9999/{path}", headers=laptop)
        assert r.status_code == 404


def _labels(conn: sqlite3.Connection, sid: str) -> tuple[str | None, str | None]:
    s = load_session(conn, int(sid))
    assert s is not None
    return s.branch, s.client_version


def test_register_stores_branch_and_client_version(
    client: TestClient, machines: Machines, conn: sqlite3.Connection
) -> None:
    key = machines["laptop"][1]
    sid = _register(client, key, branch="feature/x", client_version="0.6.0")
    assert _labels(conn, sid) == ("feature/x", "0.6.0")


def test_revive_keeps_branch_when_missing_and_clears_version(
    client: TestClient, machines: Machines, conn: sqlite3.Connection
) -> None:
    key = machines["laptop"][1]
    first = _register(
        client, key, agent_session_id="a1", branch="feature/x", client_version="0.6.0"
    )
    again = _register(client, key, agent_session_id="a1")
    assert again == first
    assert _labels(conn, first) == ("feature/x", None)


def test_revive_with_empty_branch_clears_it(
    client: TestClient, machines: Machines, conn: sqlite3.Connection
) -> None:
    key = machines["laptop"][1]
    sid = _register(client, key, agent_session_id="a1", branch="feature/x")
    _register(client, key, agent_session_id="a1", branch="")
    assert _labels(conn, sid) == (None, None)


@pytest.mark.parametrize("branch", ["x" * 101, "a\x00b", 7])
def test_register_rejects_bad_branch(
    client: TestClient, machines: Machines, branch: object
) -> None:
    r = client.post(
        "/v1/sessions",
        headers=bearer(machines["laptop"][1]),
        json={**BODY, "branch": branch},
    )
    assert r.status_code == 400
    assert r.json()["error"] in ("invalid_branch", "bad_request")


@pytest.mark.parametrize("version", ["", "x" * 33, "0.6 beta", 7])
def test_register_ignores_bad_client_version(
    client: TestClient, machines: Machines, conn: sqlite3.Connection, version: object
) -> None:
    # A cosmetic field must never cost a session its registration.
    r = client.post(
        "/v1/sessions",
        headers=bearer(machines["laptop"][1]),
        json={**BODY, "client_version": version},
    )
    assert r.status_code == 200
    assert _labels(conn, str(r.json()["session"]))[1] is None


def test_human_turn_body_sets_and_clears_branch(
    client: TestClient, machines: Machines, conn: sqlite3.Connection
) -> None:
    key = machines["laptop"][1]
    sid = _register(client, key, branch="feature/x")
    url = f"/v1/sessions/{sid}/human-turn"
    assert client.post(url, headers=bearer(key)).status_code == 200
    assert _labels(conn, sid)[0] == "feature/x"
    r = client.post(url, headers=bearer(key), json={"branch": "develop"})
    assert r.status_code == 200 and _labels(conn, sid)[0] == "develop"
    r = client.post(url, headers=bearer(key), json={"branch": ""})
    assert r.status_code == 200 and _labels(conn, sid)[0] is None


def test_human_turn_ignores_bad_branch_and_still_refills(
    client: TestClient, machines: Machines, conn: sqlite3.Connection
) -> None:
    key = machines["laptop"][1]
    sid = _register(client, key, branch="feature/x")
    for bad in ("x" * 101, 7):
        r = client.post(
            f"/v1/sessions/{sid}/human-turn", headers=bearer(key), json={"branch": bad}
        )
        assert r.status_code == 200
    assert _labels(conn, sid)[0] == "feature/x"
    s = load_session(conn, int(sid))
    assert s is not None and s.typed_at is not None


def test_peek_branch_query(
    client: TestClient, machines: Machines, conn: sqlite3.Connection
) -> None:
    key = machines["laptop"][1]
    sid = _register(client, key, branch="feature/x")
    url = f"/v1/sessions/{sid}/peek"
    assert client.get(url, headers=bearer(key)).status_code == 200
    assert _labels(conn, sid)[0] == "feature/x"
    for name in ("fix/a&b#c", "feature/café"):
        r = client.get(url, headers=bearer(key), params={"branch": name})
        assert r.status_code == 200 and _labels(conn, sid)[0] == name
    client.get(url, headers=bearer(key), params={"branch": ""})
    assert _labels(conn, sid)[0] is None


def test_peek_ignores_invalid_branch(
    client: TestClient, machines: Machines, conn: sqlite3.Connection
) -> None:
    key = machines["laptop"][1]
    sid = _register(client, key, branch="feature/x")
    url = f"/v1/sessions/{sid}/peek"
    plain = client.get(url, headers=bearer(key)).json()
    for bad in ("x" * 101, "a\x07b"):
        r = client.get(url, headers=bearer(key), params={"branch": bad})
        assert r.status_code == 200 and r.json() == plain
    assert _labels(conn, sid)[0] == "feature/x"


def test_register_echoes_the_stored_branch(
    client: TestClient, machines: Machines
) -> None:
    key = machines["laptop"][1]
    body = {**BODY, "agent_session_id": "a1"}
    r = client.post("/v1/sessions", headers=bearer(key), json={**body, "branch": "x"})
    assert r.json()["branch"] == "x"
    r = client.post("/v1/sessions", headers=bearer(key), json=body)
    assert r.json()["branch"] == "x"  # kept, and said so


def test_human_turn_echoes_branch_only_when_sent(
    client: TestClient, machines: Machines
) -> None:
    key = machines["laptop"][1]
    sid = _register(client, key)
    url = f"/v1/sessions/{sid}/human-turn"
    assert client.post(url, headers=bearer(key)).json() == {}
    r = client.post(url, headers=bearer(key), json={"branch": "develop"})
    assert r.json() == {"branch": "develop"}


def test_peek_echoes_branch_only_when_sent(
    client: TestClient, machines: Machines
) -> None:
    key = machines["laptop"][1]
    sid = _register(client, key)
    url = f"/v1/sessions/{sid}/peek"
    assert "branch" not in client.get(url, headers=bearer(key)).json()
    r = client.get(url, headers=bearer(key), params={"branch": "fix/y"})
    assert r.json()["branch"] == "fix/y"
