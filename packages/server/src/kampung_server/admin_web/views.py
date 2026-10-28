"""The dashboard's read-only pages."""

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import Response

from ..clock import parse_ts
from . import queries
from .auth import AdminSession
from .web import Web

SESSION_STATES = ("open", "all", "ended")
MESSAGE_STATES = ("queued", "delivered", "expired", "all")
AGENTS = ("claude-code", "codex", "other")


def ago(value: str | None, now: datetime) -> str:
    """'just now', '4 min ago', '3 h ago', '2 d ago'; 'never' for None."""
    if value is None:
        return "never"
    return f"{age(value, now)} ago" if age(value, now) != "0 min" else "just now"


def age(value: str, now: datetime) -> str:
    minutes = max(0, int((now - parse_ts(value)).total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {minutes:02d} min"
    return f"{hours // 24} d {hours % 24} h"


def utc(value: str | None) -> str:
    return "" if value is None else parse_ts(value).strftime("%Y-%m-%d %H:%M:%S")


def install_filters(web: Web) -> None:
    web.env.filters["utc"] = utc
    web.env.filters["ago"] = lambda v: ago(v, web.deps.clock())
    web.env.filters["age"] = lambda v: age(v, web.deps.clock())


@contextmanager
def connection(web: Web) -> Iterator[sqlite3.Connection]:
    conn = web.deps.connect()
    try:
        yield conn
    finally:
        conn.close()


def nav_counts(web: Web) -> Callable[[], dict[str, Any]]:
    def counts() -> dict[str, Any]:
        with connection(web) as conn:
            o = queries.overview(conn, web.deps.clock())
            boxes = conn.execute("SELECT COUNT(*) FROM mailboxes").fetchone()[0]
        return {
            "machines": o.machines - o.revoked,
            "mailboxes": boxes,
            "sessions": f"{o.live_sessions} live",
            "messages": f"{o.queued} queued",
        }

    return counts


def _pick(value: str | None, allowed: tuple[str, ...], default: str) -> str:
    return value if value in allowed else default


def register_pages(mcp: FastMCP, web: Web) -> None:
    install_filters(web)
    web.nav_counts = nav_counts(web)

    async def overview(request: Request, session: AdminSession) -> Response:
        with connection(web) as conn:
            o = queries.overview(conn, web.deps.clock())
        return web.render(
            request,
            "overview.html",
            session,
            title="Overview",
            auto_refresh=True,
            o=o,
        )

    async def machines(request: Request, session: AdminSession) -> Response:
        with connection(web) as conn:
            rows = queries.machines(conn, web.deps.clock())
        new_key, session.new_key = session.new_key, None
        return web.render(
            request,
            "machines.html",
            session,
            title="Machines",
            # A refresh would wipe a key that is shown only once.
            auto_refresh=new_key is None,
            rows=rows,
            active=[m.name for m in rows if m.active],
            new_key=new_key,
        )

    async def mailboxes(request: Request, session: AdminSession) -> Response:
        with connection(web) as conn:
            rows = queries.mailboxes(conn, web.deps.clock())
        return web.render(
            request,
            "mailboxes.html",
            session,
            title="Mailboxes",
            auto_refresh=True,
            rows=rows,
        )

    async def sessions(request: Request, session: AdminSession) -> Response:
        state = _pick(request.query_params.get("state"), SESSION_STATES, "open")
        agent = request.query_params.get("agent")
        agent = agent if agent in AGENTS else None
        with connection(web) as conn:
            page = queries.sessions(conn, web.deps.clock(), state, agent)
        return web.render(
            request,
            "sessions.html",
            session,
            title="Sessions",
            auto_refresh=True,
            page=page,
            state=state,
            agent=agent,
            agents=AGENTS,
        )

    async def messages(request: Request, session: AdminSession) -> Response:
        q = request.query_params
        state = _pick(q.get("state"), MESSAGE_STATES, "queued")
        no_wake = q.get("no_wake") == "1"
        with connection(web) as conn:
            boxes = [b.address for b in queries.mailboxes(conn, web.deps.clock())]
            mailbox = q.get("mailbox") if q.get("mailbox") in boxes else None
            page = queries.messages(conn, web.deps.clock(), mailbox, state, no_wake)
        return web.render(
            request,
            "messages.html",
            session,
            title="Messages",
            auto_refresh=True,
            page=page,
            boxes=boxes,
            mailbox=mailbox,
            state=state,
            no_wake=no_wake,
        )

    async def message(request: Request, session: AdminSession) -> Response:
        with connection(web) as conn:
            m = queries.message(conn, web.deps.clock(), request.path_params["id"])
        if m is None:
            return web.render(
                request,
                "error.html",
                session,
                status=404,
                title="Message",
                message="No such message. It may have been deleted or swept "
                "after it expired.",
            )
        return web.render(
            request,
            "message.html",
            session,
            title="Message",
            auto_refresh=True,
            m=m,
        )

    async def audit(request: Request, session: AdminSession) -> Response:
        with connection(web) as conn:
            actors, actions = queries.actors_and_actions(conn)
            actor = request.query_params.get("actor")
            action = request.query_params.get("action")
            actor = actor if actor in actors else None
            action = action if action in actions else None
            page = queries.audit(conn, actor, action)
        return web.render(
            request,
            "audit.html",
            session,
            title="Audit log",
            auto_refresh=True,
            page=page,
            actors=actors,
            actions=actions,
            actor=actor,
            action=action,
        )

    for path, handler in (
        ("/admin", overview),
        ("/admin/machines", machines),
        ("/admin/mailboxes", mailboxes),
        ("/admin/sessions", sessions),
        ("/admin/messages", messages),
        ("/admin/messages/{id}", message),
        ("/admin/audit", audit),
    ):
        mcp.custom_route(path, methods=["GET"])(web.page(handler))
