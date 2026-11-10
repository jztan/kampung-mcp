"""Cap request bodies on the OAuth routes, which FastMCP serves and the app
cannot otherwise bound, and on /mcp, so the server holds its own line when
it is reached without Caddy in front. The body is read in full up to the
cap before the route sees it, so an oversized one is refused whole with
413. On /mcp a request without a known key is not buffered: FastMCP refuses
it with 401 unread, and a running count still cuts it off past the cap."""

import json
from collections import deque
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any, Protocol

from .limits import MAX_MCP_BODY_BYTES, MAX_OAUTH_BODY_BYTES

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

EXACT = ("/token", "/authorize", "/revoke")
PREFIX = "/oauth/"
MCP = "/mcp"


def _refusal(limit: int) -> bytes:
    return json.dumps(
        {
            "error": "request_too_large",
            "hint": f"Request bodies are limited to {limit} bytes.",
        }
    ).encode()


def _declared(scope: Scope) -> int | None:
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            text = value.decode("latin-1").strip()
            return int(text) if text.isascii() and text.isdigit() else None
    return None


def _bearer(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            scheme, _, token = value.decode("latin-1").partition(" ")
            return token.strip() if scheme.lower() == "bearer" else None
    return None


class KnownKeys(Protocol):
    def is_known(self, key: str) -> bool: ...


async def _refuse(send: Send, limit: int) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": _refusal(limit)})


class BodyLimit:
    def __init__(
        self,
        app: ASGIApp,
        limit: int = MAX_OAUTH_BODY_BYTES,
        mcp_limit: int = MAX_MCP_BODY_BYTES,
        keys: KnownKeys | None = None,
    ) -> None:
        self.app = app
        self._limit = limit
        self._mcp_limit = mcp_limit
        self._keys = keys

    def _limit_for(self, path: str) -> int | None:
        if path in EXACT or path.startswith(PREFIX):
            return self._limit
        if path == MCP or path.startswith(MCP + "/"):
            return self._mcp_limit
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = str(scope.get("path", ""))
        limit = self._limit_for(path) if scope["type"] == "http" else None
        if limit is None:
            await self.app(scope, receive, send)
            return
        declared = _declared(scope)
        if declared is not None and declared > limit:
            await _refuse(send, limit)
            return
        if self._keys is not None and self._limit_for(path) == self._mcp_limit:
            bearer = _bearer(scope)
            if bearer is None or not self._keys.is_known(bearer):
                # FastMCP answers 401 before reading the body, so a caller
                # without a known key never makes the server buffer it. The
                # cap still holds, as a running count: a valid key the index
                # has not loaded yet is cut off past it rather than buffered.
                await self.app(scope, _counted(receive, limit), send)
                return
        await self._buffer(scope, receive, send, limit)

    async def _buffer(
        self, scope: Scope, receive: Receive, send: Send, limit: int
    ) -> None:
        messages: deque[Message] = deque()
        size = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] != "http.request":
                break
            size += len(message.get("body", b""))
            if size > limit:
                await _refuse(send, limit)
                return
            if not message.get("more_body", False):
                break

        async def replay() -> Message:
            return messages.popleft() if messages else await receive()

        await self.app(scope, replay, send)


def _counted(receive: Receive, limit: int) -> Receive:
    size = 0

    async def counting() -> Message:
        nonlocal size
        if size > limit:
            return {"type": "http.disconnect"}
        message = await receive()
        if message["type"] == "http.request":
            size += len(message.get("body", b""))
            if size > limit:
                return {"type": "http.disconnect"}
        return message

    return counting
