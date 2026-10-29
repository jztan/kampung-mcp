"""Machine-key authentication for /mcp (FastMCP verifier) and /v1 (by hand).

FastMCP's `auth=` does not cover custom routes, so every /v1 handler calls
`machine_from_request` itself. Both answer a key missing from the in-memory
index without touching the database.
"""

from fastmcp.server.auth import AccessToken, TokenVerifier
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request

from .deps import ServerDeps
from .models import Machine


class MachineKeyVerifier(TokenVerifier):
    def __init__(self, deps: ServerDeps) -> None:
        super().__init__()
        self._deps = deps

    async def verify_token(self, token: str) -> AccessToken | None:
        machine = await _lookup(self._deps, token)
        if machine is None:
            return None
        return AccessToken(
            token=token,
            client_id=machine.name,
            scopes=[],
            claims={"machine_id": machine.id},
        )


async def machine_from_request(request: Request, deps: ServerDeps) -> Machine | None:
    scheme, _, key = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not key:
        return None
    return await _lookup(deps, key.strip())


async def _lookup(deps: ServerDeps, key: str) -> Machine | None:
    if deps.keys.is_unknown(key):
        return None
    return await run_in_threadpool(deps.keys.lookup, key)
