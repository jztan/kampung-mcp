"""The /admin dashboard: mounted only when KAMPUNG_ADMIN_TOKEN is set."""

import sys
from importlib.resources import files

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import Response

from ..deps import ServerDeps
from .auth import AdminAuth, AdminSession, TooManyAttempts
from .actions import register_actions
from .views import register_pages
from .web import COOKIE, Web, cookie_secure, secure

MIN_TOKEN_LENGTH = 24


def register(mcp: FastMCP, deps: ServerDeps) -> None:
    token = deps.admin_token
    if not token:
        return
    if len(token) < MIN_TOKEN_LENGTH:
        print(
            f"kampung: KAMPUNG_ADMIN_TOKEN must be at least {MIN_TOKEN_LENGTH} "
            "characters; /admin is off.",
            file=sys.stderr,
        )
        return
    web = Web(deps, AdminAuth(token, deps.clock))
    _register_auth_routes(mcp, web)
    register_pages(mcp, web)
    register_actions(mcp, web)


def _register_auth_routes(mcp: FastMCP, web: Web) -> None:
    css = (files(__package__) / "static" / "admin.css").read_text()
    js = (files(__package__) / "static" / "admin.js").read_text()
    logo = (files(__package__) / "static" / "logo.svg").read_text()

    @mcp.custom_route("/admin/static/admin.css", methods=["GET"])
    async def stylesheet(request: Request) -> Response:
        return secure(Response(css, media_type="text/css"))

    @mcp.custom_route("/admin/static/admin.js", methods=["GET"])
    async def script(request: Request) -> Response:
        return secure(Response(js, media_type="text/javascript"))

    @mcp.custom_route("/admin/static/logo.svg", methods=["GET"])
    async def logo_image(request: Request) -> Response:
        return secure(Response(logo, media_type="image/svg+xml"))

    @mcp.custom_route("/admin/login", methods=["GET", "POST"])
    async def login(request: Request) -> Response:
        if request.method == "GET":
            return web.render(request, "login.html")
        form = await request.form()
        client = request.client.host if request.client else "unknown"
        try:
            session = web.auth.login(client, str(form.get("token", "")))
        except TooManyAttempts as e:
            return web.render(
                request,
                "login.html",
                status=429,
                error=f"Too many attempts, try again in {e.minutes} minutes.",
            )
        if session is None:
            return web.render(
                request, "login.html", status=401, error="That token is not right."
            )
        response = web.redirect("/admin")
        response.set_cookie(
            COOKIE,
            session.id,
            max_age=43200,
            path="/admin",
            secure=cookie_secure(request),
            httponly=True,
            samesite="strict",
        )
        return response

    async def logout(request: Request, session: AdminSession) -> Response:
        form = await request.form()
        if not web.auth.csrf_ok(session, form.get("csrf")):
            return web.forbidden(request, session)
        web.auth.logout(session)
        response = web.redirect("/admin/login")
        response.delete_cookie(
            COOKIE,
            path="/admin",
            secure=cookie_secure(request),
            httponly=True,
            samesite="strict",
        )
        return response

    mcp.custom_route("/admin/logout", methods=["POST"])(web.page(logout))
