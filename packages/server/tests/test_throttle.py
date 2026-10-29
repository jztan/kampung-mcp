import asyncio
from typing import Any

from kampung_server.throttle import AuthFailureThrottle

Scope = dict[str, Any]


class App:
    """A stand-in ASGI app that answers every request with `status`."""

    def __init__(self, status: int) -> None:
        self.status = status
        self.calls = 0

    async def __call__(self, scope: Scope, receive: Any, send: Any) -> None:
        self.calls += 1
        await send({"type": "http.response.start", "status": self.status})
        await send({"type": "http.response.body", "body": b""})


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Keys:
    """Stand-in for KeyIndex: only `good` is a known key."""

    def is_known(self, key: str) -> bool:
        return key == "good"


def call(
    app: AuthFailureThrottle,
    path: str = "/v1/sessions",
    ip: str = "1.2.3.4",
    key: str = "bad",
) -> int:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b""}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "path": path,
        "client": (ip, 5000),
        "headers": [(b"authorization", f"Bearer {key}".encode())],
    }
    asyncio.run(app(scope, receive, send))
    return int(sent[0]["status"])


def test_blocks_an_ip_after_too_many_bad_keys() -> None:
    inner, clock = App(401), Clock()
    app = AuthFailureThrottle(inner, Keys(), limit=3, window=60, monotonic=clock)
    assert [call(app) for _ in range(3)] == [401, 401, 401]
    assert call(app) == 429 and inner.calls == 3
    assert call(app, path="/mcp") == 429
    assert call(app, ip="5.6.7.8") == 401
    clock.now += 61
    assert call(app) == 401


def test_successes_and_other_paths_do_not_count() -> None:
    clock = Clock()
    ok = AuthFailureThrottle(App(200), Keys(), limit=1, window=60, monotonic=clock)
    assert [call(ok) for _ in range(5)] == [200] * 5
    other = AuthFailureThrottle(App(401), Keys(), limit=1, window=60, monotonic=clock)
    assert [call(other, path="/admin/login") for _ in range(5)] == [401] * 5
    assert [call(other, path="/health") for _ in range(5)] == [401] * 5


def test_remembers_a_bounded_number_of_ips() -> None:
    app = AuthFailureThrottle(
        App(401), Keys(), limit=1, window=60, max_ips=10, monotonic=Clock()
    )
    for i in range(100):
        call(app, ip=f"10.0.0.{i}")
    assert app.tracked() <= 10


def test_lifespan_and_other_scopes_pass_through() -> None:
    inner = App(200)
    app = AuthFailureThrottle(inner, Keys(), limit=1, window=60, monotonic=Clock())

    async def nothing() -> dict[str, Any]:
        return {}

    async def sink(message: dict[str, Any]) -> None:
        pass

    asyncio.run(app({"type": "lifespan"}, nothing, sink))
    assert inner.calls == 1


def test_a_known_key_is_never_throttled() -> None:
    # A revoked machine retrying from a shared NAT must not lock out the
    # machines with valid keys behind the same address.
    inner = App(401)
    app = AuthFailureThrottle(inner, Keys(), limit=2, window=60, monotonic=Clock())
    assert [call(app) for _ in range(3)] == [401, 401, 429]
    assert call(app, key="good") == 401  # reaches the app (which says 401 here)
    assert inner.calls == 3


def test_ipv6_clients_are_grouped_by_64() -> None:
    app = AuthFailureThrottle(App(401), Keys(), limit=2, window=60, monotonic=Clock())
    assert call(app, ip="2001:db8:1:2::1") == 401
    assert call(app, ip="2001:db8:1:2::ffff") == 401
    assert call(app, ip="2001:db8:1:2:abcd::9") == 429
    assert call(app, ip="2001:db8:1:3::1") == 401
