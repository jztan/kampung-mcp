"""Per-IP limit on wrong machine keys for /mcp and /v1.

Wrong keys already cost no database work (see keyindex), but an address
that keeps sending them gets 429 once AUTH_FAILURE_LIMIT of its failures fall
within the last AUTH_FAILURE_WINDOW, until the oldest ages out. A request
whose key is known always gets through, so a revoked machine retrying behind
a shared NAT cannot lock out the machines next to it. The address is the one
uvicorn reports (behind Caddy, the X-Forwarded-For address; see
KAMPUNG_FORWARDED_ALLOW_IPS); IPv6 clients are grouped by /64, since one
host can hold a whole /64.
"""

import ipaddress
import json
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any, Protocol

from .limits import AUTH_FAILURE_LIMIT, AUTH_FAILURE_WINDOW

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

GUARDED = ("/mcp", "/v1/")
MAX_IPS = 10_000
_BODY = json.dumps(
    {
        "error": "rate_limited",
        "hint": "Too many requests with a wrong key; try again in a minute.",
    }
).encode()


class KnownKeys(Protocol):
    def is_known(self, key: str) -> bool: ...


def _client_key(scope: Scope) -> str:
    client = scope.get("client")
    host = str(client[0]) if client else "unknown"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    if address.version == 6:
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return host


def _bearer(scope: Scope) -> str:
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            scheme, _, key = value.decode("latin-1").partition(" ")
            return key.strip() if scheme.lower() == "bearer" else ""
    return ""


class AuthFailureThrottle:
    def __init__(
        self,
        app: ASGIApp,
        keys: KnownKeys,
        limit: int = AUTH_FAILURE_LIMIT,
        window: float = AUTH_FAILURE_WINDOW.total_seconds(),
        max_ips: int = MAX_IPS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.app = app
        self._keys = keys
        self._limit = limit
        self._window = window
        self._max_ips = max_ips
        self._monotonic = monotonic
        # Least recently failed first, so the oldest entry is dropped when full.
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not str(scope["path"]).startswith(GUARDED):
            await self.app(scope, receive, send)
            return
        ip = _client_key(scope)
        if self._blocked(ip) and not self._keys.is_known(_bearer(scope)):
            await send(
                {
                    "type": "http.response.start",
                    "status": 429,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"retry-after", str(int(self._window)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": _BODY})
            return

        async def watch(message: Message) -> None:
            if message["type"] == "http.response.start" and message["status"] == 401:
                self._record(ip)
            await send(message)

        await self.app(scope, receive, watch)

    def tracked(self) -> int:
        return len(self._failures)

    def _recent(self, ip: str, now: float) -> deque[float] | None:
        times = self._failures.get(ip)
        if times is None:
            return None
        while times and times[0] <= now - self._window:
            times.popleft()
        if not times:
            del self._failures[ip]
            return None
        return times

    def _blocked(self, ip: str) -> bool:
        with self._lock:
            times = self._recent(ip, self._monotonic())
            return times is not None and len(times) >= self._limit

    def _record(self, ip: str) -> None:
        with self._lock:
            now = self._monotonic()
            times = self._recent(ip, now) or deque()
            times.append(now)
            self._failures[ip] = times
            self._failures.move_to_end(ip)
            while len(self._failures) > self._max_ips:
                self._failures.popitem(last=False)
