"""Cap request bodies on the OAuth routes, which FastMCP serves and the app
cannot otherwise bound. The body is read in full up to the cap before the
route sees it, so an oversized one is refused whole with 413."""

import json
from collections import deque
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from .limits import MAX_OAUTH_BODY_BYTES

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

EXACT = ("/token", "/authorize", "/revoke")
PREFIX = "/oauth/"
_BODY = json.dumps(
    {
        "error": "request_too_large",
        "hint": f"Request bodies are limited to {MAX_OAUTH_BODY_BYTES} bytes.",
    }
).encode()


def _declared(scope: Scope) -> int | None:
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            text = value.decode("latin-1").strip()
            return int(text) if text.isascii() and text.isdigit() else None
    return None


class BodyLimit:
    def __init__(self, app: ASGIApp, limit: int = MAX_OAUTH_BODY_BYTES) -> None:
        self.app = app
        self._limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = str(scope.get("path", ""))
        if scope["type"] != "http" or not (path in EXACT or path.startswith(PREFIX)):
            await self.app(scope, receive, send)
            return
        declared = _declared(scope)
        messages: deque[Message] = deque()
        too_large = declared is not None and declared > self._limit
        size = 0
        while not too_large:
            message = await receive()
            messages.append(message)
            if message["type"] != "http.request":
                break
            size += len(message.get("body", b""))
            if size > self._limit:
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
            await send({"type": "http.response.body", "body": _BODY})
            return

        async def replay() -> Message:
            return messages.popleft() if messages else await receive()

        await self.app(scope, replay, send)
