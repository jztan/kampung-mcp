"""Rendering and request plumbing shared by the dashboard's pages and actions."""

from collections.abc import Awaitable, Callable
from typing import Any

from jinja2 import Environment, PackageLoader, select_autoescape
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .. import __version__
from ..deps import ServerDeps
from .auth import AdminAuth, AdminSession

COOKIE = "kampung_admin"
SECTIONS = {
    "/admin/machines",
    "/admin/mailboxes",
    "/admin/sessions",
    "/admin/messages",
    "/admin/audit",
}
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; img-src 'self' data:; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    # Pages show message bodies and, once, a new machine key.
    "Cache-Control": "no-store",
}

Handler = Callable[[Request], Awaitable[Response]]
PageHandler = Callable[[Request, AdminSession], Awaitable[Response]]


def secure(response: Response) -> Response:
    response.headers.update(SECURITY_HEADERS)
    return response


class Web:
    def __init__(self, deps: ServerDeps, auth: AdminAuth) -> None:
        self.deps = deps
        self.auth = auth
        self.env = Environment(
            loader=PackageLoader("kampung_server", "admin_web/templates"),
            autoescape=select_autoescape(default=True, default_for_string=True),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self.nav_counts: Callable[[], dict[str, Any]] = dict

    def session(self, request: Request) -> AdminSession | None:
        return self.auth.get(request.cookies.get(COOKIE))

    def render(
        self,
        request: Request,
        template: str,
        session: AdminSession | None = None,
        status: int = 200,
        **ctx: Any,
    ) -> Response:
        flash = None
        if session is not None:
            flash, session.flash = session.flash, None
        path = request.url.path
        if request.method == "GET":
            refresh = path + (f"?{request.url.query}" if request.url.query else "")
        else:  # a page shown for a POST: refresh its section, never re-POST
            section = "/".join(path.split("/")[:3])
            refresh = section if section in SECTIONS else "/admin"
        html = self.env.get_template(template).render(
            path=path,
            refresh=refresh,
            csrf=session.csrf if session else None,
            flash=flash,
            version=__version__,
            now=self.deps.clock(),
            nav=self.nav_counts() if session else {},
            **ctx,
        )
        return secure(HTMLResponse(html, status_code=status))

    def redirect(self, url: str) -> Response:
        return secure(RedirectResponse(url, status_code=303))

    def page(self, handler: PageHandler) -> Handler:
        """Only for a signed-in owner; anyone else goes to the login page."""

        async def wrapped(request: Request) -> Response:
            session = self.session(request)
            if session is None:
                return self.redirect("/admin/login")
            return await handler(request, session)

        return wrapped

    def forbidden(self, request: Request, session: AdminSession) -> Response:
        return self.render(
            request,
            "error.html",
            session,
            status=403,
            title="Form expired",
            message="This form has expired. Reload the page and try again.",
        )
