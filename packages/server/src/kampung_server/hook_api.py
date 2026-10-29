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

from . import audit, mail, sessions
from .auth import machine_from_request
from .deps import ServerDeps
from .errors import KampungError
from .limits import MAX_REQUEST_BYTES
from .models import Machine, load_session

_STATUS = {
    "unauthorized": 401,
    "not_owner": 403,
    "unknown_session": 404,
    "session_ended": 409,
    "request_too_large": 413,
    "rate_limited": 429,
}

Handler = Callable[[sqlite3.Connection, Machine, dict[str, Any]], dict[str, Any]]


def _error(code: str, hint: str) -> JSONResponse:
    return JSONResponse({"error": code, "hint": hint}, _STATUS.get(code, 400))


async def _run(
    deps: ServerDeps, request: Request, handler: Handler, read_body: bool = False
) -> JSONResponse:
    # The key is checked before the body is read, so a caller without one
    # cannot make the server buffer or parse anything.
    machine = await machine_from_request(request, deps)
    if machine is None:
        return _error("unauthorized", "Send Authorization: Bearer <key>.")
    args: dict[str, Any] = {}
    if read_body:
        try:
            args = await _json_object(request)
        except KampungError as e:
            return _error(e.code, e.hint)

    def work() -> JSONResponse:
        conn = deps.connect()
        try:
            return JSONResponse(handler(conn, machine, args))
        except KampungError as e:
            return _error(e.code, e.hint)
        finally:
            conn.close()

    return await run_in_threadpool(work)


async def _json_object(request: Request) -> dict[str, Any]:
    """The body as a JSON object, read no further than MAX_REQUEST_BYTES."""
    too_large = KampungError(
        "request_too_large", f"Request bodies are limited to {MAX_REQUEST_BYTES} bytes."
    )
    declared = request.headers.get("content-length", "")
    if declared.isascii() and declared.isdigit() and int(declared) > MAX_REQUEST_BYTES:
        raise too_large
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_REQUEST_BYTES:
            raise too_large
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        body = None
    if not isinstance(body, dict):
        raise KampungError("bad_request", "Send a JSON object.")
    return body


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
        def handler(
            conn: sqlite3.Connection, machine: Machine, b: dict[str, Any]
        ) -> dict[str, Any]:
            now = deps.clock()
            deps.register_limiter.check(machine.id, now)
            s = sessions.register(
                conn,
                machine,
                now,
                agent=_field(b, "agent", str, True),
                cwd=_field(b, "cwd", str, True),
                repo=_field(b, "repo", str, True),
                hooks=_field(b, "hooks", bool, True),
                agent_session_id=_field(b, "agent_session_id", str, False),
                suffix=_field(b, "suffix", str, False),
            )
            return {"session": str(s.id), "suffix": s.suffix, "mailbox": s.mailbox}

        return await _run(deps, request, handler, read_body=True)

    @mcp.custom_route("/v1/sessions/{id}/human-turn", methods=["POST"])
    async def human_turn(request: Request) -> JSONResponse:
        def handler(
            conn: sqlite3.Connection, machine: Machine, _: dict[str, Any]
        ) -> dict[str, Any]:
            s = sessions.resolve(conn, machine, _session_id(request), deps.clock())
            sessions.human_turn(conn, s)
            return {}

        return await _run(deps, request, handler)

    @mcp.custom_route("/v1/sessions/{id}/peek", methods=["GET"])
    async def peek(request: Request) -> JSONResponse:
        def handler(
            conn: sqlite3.Connection, machine: Machine, _: dict[str, Any]
        ) -> dict[str, Any]:
            now = deps.clock()
            s = sessions.resolve(conn, machine, _session_id(request), now)
            return dict(mail.peek(conn, s, now).to_dict())

        return await _run(deps, request, handler)

    @mcp.custom_route("/v1/sessions/{id}/end", methods=["POST"])
    async def end(request: Request) -> JSONResponse:
        def handler(
            conn: sqlite3.Connection, machine: Machine, _: dict[str, Any]
        ) -> dict[str, Any]:
            now = deps.clock()
            actor = f"machine:{machine.name}"
            s = load_session(conn, _session_id(request))
            if s is None:
                raise KampungError("unknown_session", "No such session.")
            if s.machine_id != machine.id:
                audit.record(conn, now, actor, "denied", {"session": s.id})
                raise KampungError(
                    "not_owner", "That session belongs to another machine."
                )
            sessions.end(conn, s.id, now)
            audit.record(conn, now, actor, "end_session", {"session": s.id})
            return {}

        return await _run(deps, request, handler)
