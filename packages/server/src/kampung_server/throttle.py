"""Per-IP limit on wrong machine keys for /mcp and /v1.

Wrong keys already cost no database work (see keyindex), but a client that
keeps guessing still gets 429 after AUTH_FAILURE_LIMIT failures in
AUTH_FAILURE_WINDOW, before its request reaches the app. The client IP is
the one uvicorn reports, which behind Caddy is the X-Forwarded-For address
(see KAMPUNG_FORWARDED_ALLOW_IPS).
"""

import json
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

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


class AuthFailureThrottle:
    def __init__(
        self,
        app: ASGIApp,
        limit: int = AUTH_FAILURE_LIMIT,
        window: float = AUTH_FAILURE_WINDOW.total_seconds(),
        max_ips: int = MAX_IPS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.app = app
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
        client = scope.get("client")
        ip = client[0] if client else "unknown"
        if self._blocked(ip):
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
