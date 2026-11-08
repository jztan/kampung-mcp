from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from kampung_server.app import create_app
from kampung_server.deps import ServerDeps
from kampung_server.limits import MAX_MCP_BODY_BYTES
from kampung_server.ratelimit import RateLimiter
from kampung_server.models import Machine
from server_testlib import FakeClock

Machines = dict[str, tuple[Machine, str]]


@pytest.fixture
def client(db_path: Path, conn: object, clock: FakeClock) -> Iterator[TestClient]:
    deps = ServerDeps(db_path=db_path, clock=clock.now, limiter=RateLimiter())
    with TestClient(create_app(deps)) as c:
        yield c


def test_mcp_body_over_1_mb_is_413(client: TestClient) -> None:
    r = client.post("/mcp", content=b"x" * (MAX_MCP_BODY_BYTES + 1))
    assert r.status_code == 413 and r.json()["error"] == "request_too_large"


def _known_key(client: TestClient, machines: Machines) -> dict[str, str]:
    key = machines["laptop"][1]
    headers = {"Authorization": f"Bearer {key}"}
    # One request loads the key index, so the key is known from then on.
    client.get("/v1/sessions/0/peek", headers=headers)
    return headers


def test_streamed_mcp_body_over_1_mb_is_413(
    client: TestClient, machines: Machines
) -> None:
    def chunks() -> Iterator[bytes]:
        for _ in range(17):
            yield b"x" * 65536

    headers = _known_key(client, machines)
    r = client.post("/mcp", content=chunks(), headers=headers)
    assert r.status_code == 413


def test_wrong_key_gets_401_without_the_body_being_buffered(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kampung_server import bodylimit

    buffered: list[int] = []
    real = bodylimit.BodyLimit._buffer

    async def spy(self: object, *a: object, **kw: object) -> object:
        buffered.append(1)
        return await real(self, *a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(bodylimit.BodyLimit, "_buffer", spy)

    def chunks() -> Iterator[bytes]:
        for _ in range(16):
            yield b"x" * 65536

    r = client.post(
        "/mcp", content=chunks(), headers={"Authorization": "Bearer kmp_wrong"}
    )
    assert r.status_code == 401 and buffered == []


def test_unknown_bearer_is_still_cut_off_past_the_cap() -> None:
    import asyncio

    from kampung_server.bodylimit import BodyLimit

    seen: list[str] = []

    async def app(scope: object, receive: object, send: object) -> None:
        while True:
            message = await receive()  # type: ignore[operator]
            seen.append(message["type"])
            if message["type"] != "http.request":
                return

    class Nobody:
        def is_known(self, key: str) -> bool:
            return False

    chunks = [
        {"type": "http.request", "body": b"x" * 600, "more_body": True}
        for _ in range(3)
    ]

    async def receive() -> dict[str, object]:
        return chunks.pop(0)

    async def send(message: object) -> None:
        pass

    scope = {
        "type": "http",
        "path": "/mcp",
        "headers": [(b"authorization", b"Bearer kmp_new")],
    }
    limited = BodyLimit(app, mcp_limit=1000, keys=Nobody())  # type: ignore[arg-type]
    asyncio.run(limited(scope, receive, send))  # type: ignore[arg-type]
    assert seen == ["http.request", "http.disconnect"]


def test_mcp_body_under_the_cap_reaches_the_app(client: TestClient) -> None:
    r = client.post("/mcp", content=b"x" * 1000)
    assert r.status_code != 413  # 401: no key, which the app decides
