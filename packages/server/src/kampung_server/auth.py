"""Machine-key authentication for /mcp (FastMCP verifier) and /v1 (by hand).

FastMCP's `auth=` does not cover custom routes, so every /v1 handler calls
`machine_from_request` itself.
"""

import sqlite3

from fastmcp.server.auth import AccessToken, TokenVerifier
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request

from .deps import ServerDeps
from .identity import authenticate
from .models import Machine


class MachineKeyVerifier(TokenVerifier):
    def __init__(self, deps: ServerDeps) -> None:
        super().__init__()
        self._deps = deps

    async def verify_token(self, token: str) -> AccessToken | None:
        machine = await run_in_threadpool(self._lookup, token)
        if machine is None:
            return None
        return AccessToken(
            token=token,
            client_id=machine.name,
            scopes=[],
            claims={"machine_id": machine.id},
        )

    def _lookup(self, token: str) -> Machine | None:
        conn = self._deps.connect()
        try:
            return authenticate(conn, token)
        finally:
            conn.close()


def machine_from_request(request: Request, conn: sqlite3.Connection) -> Machine | None:
    scheme, _, key = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not key:
        return None
    return authenticate(conn, key.strip())
