"""The /v1 hook API: session registration and loop-guard state, never seen by
the model."""

import json
import sqlite3
from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import mail, sessions
from .auth import machine_from_request
from .deps import ServerDeps
from .errors import KampungError
from .models import Machine, load_session

_STATUS = {
    "unauthorized": 401,
    "not_owner": 403,
    "unknown_session": 404,
    "session_ended": 409,
}

Handler = Callable[[sqlite3.Connection, Machine, dict[str, Any]], dict[str, Any]]


def _error(code: str, hint: str) -> JSONResponse:
    return JSONResponse({"error": code, "hint": hint}, _STATUS.get(code, 400))


async def _run(
    deps: ServerDeps, request: Request, handler: Handler, args: dict[str, Any]
) -> JSONResponse:
    def work() -> JSONResponse:
        conn = deps.connect()
        try:
            machine = machine_from_request(request, conn)
            if machine is None:
                return _error("unauthorized", "Send Authorization: Bearer <key>.")
            return JSONResponse(handler(conn, machine, args))
        except KampungError as e:
            return _error(e.code, e.hint)
        finally:
            conn.close()

    return await run_in_threadpool(work)


def _session_id(request: Request) -> int:
    try:
        return int(request.path_params["id"])
    except ValueError:
        raise KampungError("unknown_session", "No such session; register again.")


def _field(body: dict[str, Any], name: str, kind: type, required: bool) -> Any:
    value = body.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, kind):
        raise KampungError("bad_request", f"Field {name} must be {kind.__name__}.")
    return value


def register_hook_routes(mcp: FastMCP, deps: ServerDeps) -> None:
    @mcp.custom_route("/v1/sessions", methods=["POST"])
    async def register(request: Request) -> JSONResponse:
        try:
            body = json.loads(await request.body())
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return _error("bad_request", "Send a JSON object.")

        def handler(
            conn: sqlite3.Connection, machine: Machine, b: dict[str, Any]
        ) -> dict[str, Any]:
            s = sessions.register(
                conn,
                machine,
                deps.clock(),
                agent=_field(b, "agent", str, True),
                cwd=_field(b, "cwd", str, True),
                repo=_field(b, "repo", str, True),
                hooks=_field(b, "hooks", bool, True),
                agent_session_id=_field(b, "agent_session_id", str, False),
                suffix=_field(b, "suffix", str, False),
            )
            return {"session": str(s.id), "suffix": s.suffix, "mailbox": s.mailbox}

        return await _run(deps, request, handler, body)

    @mcp.custom_route("/v1/sessions/{id}/human-turn", methods=["POST"])
    async def human_turn(request: Request) -> JSONResponse:
        def handler(
            conn: sqlite3.Connection, machine: Machine, _: dict[str, Any]
        ) -> dict[str, Any]:
            s = sessions.resolve(conn, machine, _session_id(request), deps.clock())
            sessions.human_turn(conn, s)
            return {}

        return await _run(deps, request, handler, {})

    @mcp.custom_route("/v1/sessions/{id}/peek", methods=["GET"])
    async def peek(request: Request) -> JSONResponse:
        def handler(
            conn: sqlite3.Connection, machine: Machine, _: dict[str, Any]
        ) -> dict[str, Any]:
            now = deps.clock()
            s = sessions.resolve(conn, machine, _session_id(request), now)
            return dict(mail.peek(conn, s, now).to_dict())

        return await _run(deps, request, handler, {})

    @mcp.custom_route("/v1/sessions/{id}/end", methods=["POST"])
    async def end(request: Request) -> JSONResponse:
        def handler(
            conn: sqlite3.Connection, machine: Machine, _: dict[str, Any]
        ) -> dict[str, Any]:
            s = load_session(conn, _session_id(request))
            if s is None:
                raise KampungError("unknown_session", "No such session.")
            if s.machine_id != machine.id:
                raise KampungError(
                    "not_owner", "That session belongs to another machine."
                )
            sessions.end(conn, s.id, deps.clock())
            return {}

        return await _run(deps, request, handler, {})
