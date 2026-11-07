from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from kampung_server.app import create_app
from kampung_server.deps import ServerDeps
from kampung_server.limits import MAX_MCP_BODY_BYTES
from kampung_server.ratelimit import RateLimiter
from server_testlib import FakeClock


@pytest.fixture
def client(db_path: Path, conn: object, clock: FakeClock) -> Iterator[TestClient]:
    deps = ServerDeps(db_path=db_path, clock=clock.now, limiter=RateLimiter())
    with TestClient(create_app(deps)) as c:
        yield c


def test_mcp_body_over_1_mb_is_413(client: TestClient) -> None:
    r = client.post("/mcp", content=b"x" * (MAX_MCP_BODY_BYTES + 1))
    assert r.status_code == 413 and r.json()["error"] == "request_too_large"


def test_streamed_mcp_body_over_1_mb_is_413(client: TestClient) -> None:
    def chunks() -> Iterator[bytes]:
        for _ in range(17):
            yield b"x" * 65536

    r = client.post("/mcp", content=chunks())
    assert r.status_code == 413


def test_mcp_body_under_the_cap_reaches_the_app(client: TestClient) -> None:
    r = client.post("/mcp", content=b"x" * 1000)
    assert r.status_code != 413  # 401: no key, which the app decides
