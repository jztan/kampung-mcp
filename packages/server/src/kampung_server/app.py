"""Assemble the FastMCP server: auth, MCP tools, hook API and health."""

import threading

import uvicorn
from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider, MultiAuth
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__, admin_web, db, guard
from .auth import MachineKeyVerifier
from .bodylimit import BodyLimit
from .deps import ServerDeps
from .hook_api import register_hook_routes
from .limits import SWEEP_EVERY
from .oauth import KampungOAuth
from .oauth_connect import ConnectLimiter, register_connect_routes
from .sweep import start_sweeper
from .throttle import AnyKnown, AuthFailureThrottle, ClientAddress
from .tools import SERVER_INSTRUCTIONS, register_tools


def build_server(deps: ServerDeps) -> FastMCP:
    # With a public URL, Claude app tokens are tried first, then machine
    # keys. Without one, no OAuth route exists.
    auth: AuthProvider = MachineKeyVerifier(deps)
    provider = None
    if deps.public_url is not None:
        provider = KampungOAuth(deps, deps.public_url)
        auth = MultiAuth(server=provider, verifiers=[MachineKeyVerifier(deps)])
    mcp = FastMCP(
        "kampung", instructions=SERVER_INSTRUCTIONS, version=__version__, auth=auth
    )
    register_tools(mcp, deps)
    register_hook_routes(mcp, deps)
    admin_web.register(mcp, deps)
    if provider is not None:
        register_connect_routes(mcp, deps, provider, ConnectLimiter(deps.clock))

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"version": __version__})

    return mcp


def create_app(deps: ServerDeps) -> Starlette:
    app = build_server(deps).http_app(path="/mcp", stateless_http=True)
    known = AnyKnown(deps.keys, deps.tokens)
    app.add_middleware(BodyLimit, keys=known)
    app.add_middleware(ClientAddress)
    # Outermost, so it sees the 401 from FastMCP's own key check on /mcp too.
    app.add_middleware(AuthFailureThrottle, keys=known)
    return app


def cap_pair_debt(deps: ServerDeps) -> None:
    """Fit stored pair debt to this start's settings: a shorter refill interval
    takes effect at once instead of after the old, longer one."""
    conn = deps.connect()
    try:
        with db.transaction(conn):
            guard.cap_pairs(conn, deps.clock(), deps.guard)
    finally:
        conn.close()


# Seconds a restart waits for open requests: event streams never end by
# themselves, so after this they are closed and adapters reconnect or poll.
SHUTDOWN_GRACE = 3


def serve(
    deps: ServerDeps, host: str, port: int, forwarded_allow_ips: str = "127.0.0.1"
) -> None:
    cap_pair_debt(deps)
    start_sweeper(
        deps.db_path, deps.clock, SWEEP_EVERY.total_seconds(), threading.Event()
    )
    uvicorn.run(
        create_app(deps),
        host=host,
        port=port,
        # The client IP the throttle and the dashboard's login limit key on.
        proxy_headers=True,
        forwarded_allow_ips=forwarded_allow_ips,
        timeout_graceful_shutdown=SHUTDOWN_GRACE,
    )
