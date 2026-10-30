import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

import kampung_server
from server_testlib import FakeClock
from kampung_server.admin import AdminService
from kampung_server.app import create_app
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
    assert peek.json() == {"waiting": 0, "wakeable": 0}


def test_other_machines_session_403(client: TestClient, machines: Machines) -> None:
    sid = _register(client, machines["laptop"][1])
    r = client.get(f"/v1/sessions/{sid}/peek", headers=bearer(machines["desktop"][1]))
    assert r.status_code == 403 and r.json()["error"] == "not_owner"
    r = client.post(f"/v1/sessions/{sid}/end", headers=bearer(machines["desktop"][1]))
    assert r.status_code == 403


def test_unknown_session_404(client: TestClient, machines: Machines) -> None:
    r = client.get("/v1/sessions/9999/peek", headers=bearer(machines["laptop"][1]))
    assert r.status_code == 404 and r.json()["error"] == "unknown_session"
    r = client.get("/v1/sessions/abc/peek", headers=bearer(machines["laptop"][1]))
    assert r.status_code == 404


def test_human_turn_resets_depth(
    client: TestClient, conn: sqlite3.Connection, machines: Machines
) -> None:
    key = machines["laptop"][1]
    sid = _register(client, key)
    conn.execute("UPDATE sessions SET max_depth_seen = 4 WHERE id = ?", (int(sid),))
    r = client.post(f"/v1/sessions/{sid}/human-turn", headers=bearer(key))
    assert r.status_code == 200 and r.json() == {}
    s = load_session(conn, int(sid))
    assert s is not None and s.max_depth_seen == -1


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


def test_register_other_machines_agent_session_403(
    client: TestClient, machines: Machines
) -> None:
    _register(client, machines["desktop"][1], agent_session_id="cc-9")
    r = client.post(
        "/v1/sessions",
        headers=bearer(machines["laptop"][1]),
        json={**BODY, "agent_session_id": "cc-9"},
    )
    assert r.status_code == 403


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
    assert r.status_code == 403
    r = client.post(f"/v1/sessions/{sid}/end", headers=bearer(machines["laptop"][1]))
    assert r.status_code == 200
    rows = conn.execute(
        "SELECT actor, action FROM audit WHERE action IN ('denied', 'end_session') "
        "ORDER BY id"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("machine:desktop", "denied"),
        ("machine:laptop", "end_session"),
    ]


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
