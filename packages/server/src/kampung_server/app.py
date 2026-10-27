"""Assemble the FastMCP server: auth, hook API, health, and (Task 10) tools."""

from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__
from .auth import MachineKeyVerifier
from .deps import ServerDeps
from .hook_api import register_hook_routes


def build_server(deps: ServerDeps) -> FastMCP:
    mcp = FastMCP("kampung", auth=MachineKeyVerifier(deps))
    register_hook_routes(mcp, deps)

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"version": __version__})

    return mcp


def create_app(deps: ServerDeps) -> Starlette:
    return build_server(deps).http_app(path="/mcp", stateless_http=True)
