"""Cap request bodies on the OAuth routes, which FastMCP serves and the app
cannot otherwise bound, and on /mcp, so the server holds its own line when
it is reached without Caddy in front. The body is read in full up to the
cap before the route sees it, so an oversized one is refused whole with
413."""

import json
from collections import deque
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

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


class BodyLimit:
    def __init__(
        self,
        app: ASGIApp,
        limit: int = MAX_OAUTH_BODY_BYTES,
        mcp_limit: int = MAX_MCP_BODY_BYTES,
    ) -> None:
        self.app = app
        self._limit = limit
        self._mcp_limit = mcp_limit

    def _limit_for(self, path: str) -> int | None:
        if path in EXACT or path.startswith(PREFIX):
            return self._limit
        if path == MCP or path.startswith(MCP + "/"):
            return self._mcp_limit
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        limit = (
            self._limit_for(str(scope.get("path", "")))
            if scope["type"] == "http"
            else None
        )
        if limit is None:
            await self.app(scope, receive, send)
            return
        declared = _declared(scope)
        messages: deque[Message] = deque()
        too_large = declared is not None and declared > limit
        size = 0
        while not too_large:
            message = await receive()
            messages.append(message)
            if message["type"] != "http.request":
                break
            size += len(message.get("body", b""))
            if size > limit:
                too_large = True
            elif not message.get("more_body", False):
                break
        if too_large:
            await send(
                {
                    "type": "http.response.start",
                    "status": 413,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": _refusal(limit)})
            return

        async def replay() -> Message:
            return messages.popleft() if messages else await receive()

        await self.app(scope, replay, send)
