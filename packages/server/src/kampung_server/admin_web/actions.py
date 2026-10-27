"""The dashboard's actions. Each is a POST with a CSRF token, goes through
AdminService (so it is validated and audited once, as admin:web), and
redirects back with a one-line result so a refresh never repeats it."""

from collections.abc import Awaitable, Callable

from fastmcp import FastMCP
from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import Response

from ..admin import AdminService
from ..errors import KampungError
from ..models import Session, load_session
from .auth import AdminSession
from .views import connection
from .web import Web

ACTOR = "admin:web"
# An action returns where to go next, or a page to show instead (a re-shown form).
Action = Callable[[Request, AdminSession, FormData], Awaitable[str | Response]]


def register_actions(mcp: FastMCP, web: Web) -> None:
    admin = AdminService(web.deps.connect, web.deps.clock)

    def post(path: str, back: str) -> Callable[[Action], Action]:
        """Route a POST through the CSRF check; a KampungError becomes a banner
        on the `back` page instead of an error page."""

        def decorate(action: Action) -> Action:
            async def handler(request: Request, session: AdminSession) -> Response:
                form = await request.form()
                if not web.auth.csrf_ok(session, form.get("csrf")):
                    return web.forbidden(request, session)
                try:
                    target = await action(request, session, form)
                except KampungError as e:
                    session.flash = ("error", e.hint)
                    return web.redirect(back)
                if isinstance(target, Response):
                    return target
                return web.redirect(target)

            mcp.custom_route(path, methods=["POST"])(web.page(handler))
            return action

        return decorate

    def session_of(raw: str) -> Session:
        with connection(web) as conn:
            found = load_session(conn, int(raw)) if raw.isdigit() else None
        if found is None:
            raise KampungError("unknown_session", f"No session {raw}.")
        return found

    @post("/admin/machines", back="/admin/machines")
    async def create(request: Request, s: AdminSession, form: FormData) -> str:
        name = str(form.get("name", "")).strip()
        _, key = admin.add_machine(name, ACTOR)
        s.new_key = (name, key)  # shown once by the Machines page, never in a URL
        return "/admin/machines"

    @post("/admin/pairs", back="/admin/machines")
    async def pair(request: Request, s: AdminSession, form: FormData) -> str:
        a, b = str(form.get("a", "")), str(form.get("b", ""))
        admin.pair(a, b, ACTOR)
        s.flash = ("ok", f"Paired {a} and {b}.")
        return "/admin/machines"

    @post("/admin/pairs/delete", back="/admin/machines")
    async def unpair(request: Request, s: AdminSession, form: FormData) -> str:
        a, b = str(form.get("a", "")), str(form.get("b", ""))
        admin.unpair(a, b, ACTOR)
        s.flash = ("ok", f"Unpaired {a} and {b}. Queued mail stays until it expires.")
        return "/admin/machines"

    def revoke_page(
        request: Request, s: AdminSession, name: str, error: str | None = None
    ) -> Response:
        with connection(web) as conn:
            row = conn.execute(
                "SELECT m.revoked_at, "
                "(SELECT COUNT(*) FROM mailboxes mb WHERE mb.machine_id = m.id) "
                "AS boxes, "
                "(SELECT COUNT(*) FROM sessions se JOIN mailboxes mb "
                " ON mb.id = se.mailbox_id WHERE mb.machine_id = m.id "
                " AND se.ended_at IS NULL) AS open "
                "FROM machines m WHERE m.name = ?",
                (name,),
            ).fetchone()
        if row is None or row["revoked_at"] is not None:
            return web.render(
                request,
                "error.html",
                s,
                status=404,
                title="Revoke machine",
                message=f"No active machine named {name}.",
            )
        return web.render(
            request,
            "revoke.html",
            s,
            title="Revoke machine",
            name=name,
            boxes=row["boxes"],
            open=row["open"],
            error=error,
        )

    async def revoke_confirm(request: Request, s: AdminSession) -> Response:
        return revoke_page(request, s, request.path_params["name"])

    mcp.custom_route("/admin/machines/{name}/revoke", methods=["GET"])(
        web.page(revoke_confirm)
    )

    @post("/admin/machines/{name}/revoke", back="/admin/machines")
    async def revoke(
        request: Request, s: AdminSession, form: FormData
    ) -> str | Response:
        name = request.path_params["name"]
        if str(form.get("confirm", "")).strip() != name:
            return revoke_page(
                request, s, name, error="The name did not match; nothing was revoked."
            )
        admin.revoke(name, ACTOR)
        s.flash = ("ok", f"Revoked {name}. Its sessions are ended.")
        return "/admin/machines"

    @post("/admin/sessions/{id}/end", back="/admin/sessions")
    async def end(request: Request, s: AdminSession, form: FormData) -> str:
        target = session_of(request.path_params["id"])
        admin.end_session(target.id, ACTOR)
        s.flash = (
            "ok",
            f"Ended {target.address}. Its targeted mail falls back to "
            f"{target.mailbox}.",
        )
        return "/admin/sessions"

    @post("/admin/sessions/{id}/reset-depth", back="/admin/sessions")
    async def reset(request: Request, s: AdminSession, form: FormData) -> str:
        target = session_of(request.path_params["id"])
        admin.reset_depth(target.id, ACTOR)
        s.flash = (
            "ok",
            f"Reset depth for {target.address}. Its next message can wake peers.",
        )
        return "/admin/sessions"

    async def delete_confirm(request: Request, s: AdminSession) -> Response:
        message_id = request.path_params["id"]
        with connection(web) as conn:
            row = conn.execute(
                "SELECT delivered_at FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
        if row is None or row["delivered_at"] is not None:
            return web.render(
                request,
                "error.html",
                s,
                status=404,
                title="Delete message",
                message="No queued message with that id. It may have been read, "
                "deleted or swept.",
            )
        return web.render(
            request, "delete_message.html", s, title="Delete message", id=message_id
        )

    mcp.custom_route("/admin/messages/{id}/delete", methods=["GET"])(
        web.page(delete_confirm)
    )

    @post("/admin/messages/{id}/delete", back="/admin/messages")
    async def delete(request: Request, s: AdminSession, form: FormData) -> str:
        message_id = request.path_params["id"]
        admin.delete_message(message_id, ACTOR)
        s.flash = ("ok", f"Deleted {message_id}.")
        return "/admin/messages"
