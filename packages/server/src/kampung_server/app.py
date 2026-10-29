"""Assemble the FastMCP server: auth, MCP tools, hook API and health."""

import threading

import uvicorn
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__, admin_web
from .auth import MachineKeyVerifier
from .deps import ServerDeps
from .hook_api import register_hook_routes
from .limits import SWEEP_EVERY
from .sweep import start_sweeper
from .throttle import AuthFailureThrottle
from .tools import SERVER_INSTRUCTIONS, register_tools


def build_server(deps: ServerDeps) -> FastMCP:
    mcp = FastMCP(
        "kampung", instructions=SERVER_INSTRUCTIONS, auth=MachineKeyVerifier(deps)
    )
    register_tools(mcp, deps)
    register_hook_routes(mcp, deps)
    admin_web.register(mcp, deps)

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"version": __version__})

    return mcp


def create_app(deps: ServerDeps) -> Starlette:
    app = build_server(deps).http_app(path="/mcp", stateless_http=True)
    # Outermost, so it sees the 401 from FastMCP's own key check on /mcp too.
    app.add_middleware(AuthFailureThrottle, keys=deps.keys)
    return app


def serve(
    deps: ServerDeps, host: str, port: int, forwarded_allow_ips: str = "127.0.0.1"
) -> None:
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
    )
