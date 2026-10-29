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
    return build_server(deps).http_app(path="/mcp", stateless_http=True)


def serve(deps: ServerDeps, host: str, port: int) -> None:
    start_sweeper(
        deps.db_path, deps.clock, SWEEP_EVERY.total_seconds(), threading.Event()
    )
    uvicorn.run(create_app(deps), host=host, port=port)
