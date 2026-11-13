"""The /v1 hook API: session registration and loop-guard state, never seen by
the model."""

import json
import sqlite3
from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from . import audit, db, events, mail, sessions
from .auth import machine_from_request
from .deps import ServerDeps
from .errors import KampungError
from .ids import parse_session_id
from .limits import MAX_REQUEST_BYTES
from .models import Machine, load_session

_STATUS = {
    "unauthorized": 401,
    "unknown_session": 404,
    "session_ended": 409,
    "request_too_large": 413,
    "rate_limited": 429,
    "too_many_streams": 429,
}

Handler = Callable[[sqlite3.Connection, Machine, dict[str, Any]], dict[str, Any]]


def _error(code: str, hint: str) -> JSONResponse:
    return JSONResponse({"error": code, "hint": hint}, _STATUS.get(code, 400))


async def _run(
    deps: ServerDeps,
    request: Request,
    handler: Handler,
    read_body: bool = False,
    body_optional: bool = False,
) -> JSONResponse:
    # The key is checked before the body is read, so a caller without one
    # cannot make the server buffer or parse anything.
    machine = await machine_from_request(request, deps)
    if machine is None:
        return _error("unauthorized", "Send Authorization: Bearer <key>.")
    args: dict[str, Any] = {}
    if read_body:
        try:
            args = await _json_object(request, body_optional)
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


async def _json_object(request: Request, optional: bool = False) -> dict[str, Any]:
    """The body as a JSON object, read no further than MAX_REQUEST_BYTES. With
    `optional`, an empty body reads as {} (clients before 0.6 send none)."""
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
    if optional and not raw.strip():
        return {}
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        body = None
    if not isinstance(body, dict):
        raise KampungError("bad_request", "Send a JSON object.")
    return body


def _session_id(request: Request) -> int:
    session_id = parse_session_id(request.path_params["id"])
    if session_id is None:
        raise KampungError("unknown_session", "No such session; register again.")
    return session_id


def _field(body: dict[str, Any], name: str, kind: type, required: bool) -> Any:
    value = body.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, kind):
        raise KampungError("bad_request", f"Field {name} must be {kind.__name__}.")
    return value


_ABSENT = object()


def _reported_branch(value: object) -> str | None | sessions.Keep:
    """A branch sent with a typed prompt or a peek. A bad one is ignored, not
    refused: refusing would also lose the refill or the Stop hook's wake check,
    and the client already cuts and filters what it sends."""
    if not isinstance(value, str):
        return sessions.Keep.KEEP
    try:
        return sessions.check_branch(value)
    except KampungError:
        return sessions.Keep.KEEP


def _reported_version(value: object) -> str | None:
    """A client version, or None when missing or malformed: a cosmetic field
    must never cost a session its registration."""
    if not isinstance(value, str):
        return None
    try:
        return sessions.check_client_version(value)
    except KampungError:
        return None


def register_hook_routes(mcp: FastMCP, deps: ServerDeps) -> None:
    @mcp.custom_route("/v1/sessions", methods=["POST"])
    async def register(request: Request) -> JSONResponse:
        def handler(
            conn: sqlite3.Connection, machine: Machine, b: dict[str, Any]
        ) -> dict[str, Any]:
            now = deps.clock()
            deps.register_limiter.check(machine.id, now)
            agent = _field(b, "agent", str, True)
            if agent == sessions.CONNECTOR_AGENT:
                # Only the connect flow may make a session whose sends are
                # always depth 0; a machine key must not opt out that way.
                raise KampungError(
                    "invalid_agent", "claude-app sessions come from kampung connect."
                )
            agent_session_id = _field(b, "agent_session_id", str, False)
            if agent_session_id is not None and agent_session_id.startswith(
                sessions.CONNECTOR_ID_PREFIX
            ):
                raise KampungError(
                    "invalid_agent_session",
                    "agent_session_id values starting with "
                    f"{sessions.CONNECTOR_ID_PREFIX!r} are the connector's.",
                )
            s = sessions.register(
                conn,
                machine,
                now,
                agent=agent,
                cwd=_field(b, "cwd", str, True),
                repo=_field(b, "repo", str, True),
                hooks=_field(b, "hooks", bool, True),
                agent_session_id=agent_session_id,
                suffix=_field(b, "suffix", str, False),
                background=_field(b, "background", bool, False) or False,
                idle_wake=_field(b, "idle_wake", bool, False) or False,
                branch=(
                    sessions.check_branch(_field(b, "branch", str, True))
                    if "branch" in b
                    else sessions.Keep.KEEP
                ),
                client_version=_reported_version(b.get("client_version")),
            )
            # The stored branch is echoed so a client knows this server keeps
            # it (one before 0.6 drops it silently).
            return {
                "session": str(s.id),
                "suffix": s.suffix,
                "mailbox": s.mailbox,
                "branch": s.branch,
            }

        return await _run(deps, request, handler, read_body=True)

    @mcp.custom_route("/v1/sessions/{id}/human-turn", methods=["POST"])
    async def human_turn(request: Request) -> JSONResponse:
        def handler(
            conn: sqlite3.Connection, machine: Machine, b: dict[str, Any]
        ) -> dict[str, Any]:
            now = deps.clock()
            deps.human_turn_limiter.check(machine.id, now)
            s = sessions.resolve(conn, machine, _session_id(request), now)
            branch = _reported_branch(b.get("branch", _ABSENT))
            with db.transaction(conn):
                if not isinstance(branch, sessions.Keep):
                    sessions.set_branch(conn, s.id, branch)
                cleared = sessions.human_turn(conn, s, now)
                audit.record(
                    conn,
                    now,
                    f"machine:{machine.name}",
                    "refill",
                    {"mailbox": s.mailbox, "pairs": cleared, "by": "human_turn"},
                )
            return {} if isinstance(branch, sessions.Keep) else {"branch": branch}

        return await _run(deps, request, handler, read_body=True, body_optional=True)

    @mcp.custom_route("/v1/sessions/{id}/peek", methods=["GET"])
    async def peek(request: Request) -> JSONResponse:
        def handler(
            conn: sqlite3.Connection, machine: Machine, _: dict[str, Any]
        ) -> dict[str, Any]:
            now = deps.clock()
            deps.peek_limiter.check(machine.id, now)
            s = sessions.resolve(conn, machine, _session_id(request), now)
            branch = _reported_branch(request.query_params.get("branch", _ABSENT))
            result = dict(mail.peek(conn, s, now).to_dict())
            if not isinstance(branch, sessions.Keep):
                sessions.set_branch(conn, s.id, branch)
                result["branch"] = branch  # echoed only when stored
            return result

        return await _run(deps, request, handler)

    @mcp.custom_route("/v1/sessions/{id}/events", methods=["GET"])
    async def session_events(request: Request) -> Response:
        machine = await machine_from_request(request, deps)
        if machine is None:
            return _error("unauthorized", "Send Authorization: Bearer <key>.")

        def check() -> int:
            now = deps.clock()
            deps.stream_limiter.check(machine.id, now)
            conn = deps.connect()
            try:
                s = load_session(conn, _session_id(request))
                if s is None or not sessions.usable_by(s, machine):
                    raise KampungError(
                        "unknown_session", "No such session; register again."
                    )
                if s.ended_at is not None:
                    raise KampungError(
                        "session_ended", "Re-read your session file or re-register."
                    )
                return s.id
            finally:
                conn.close()

        try:
            session_id = await run_in_threadpool(check)
        except KampungError as e:
            return _error(e.code, e.hint)
        if not events.claim(machine.id):
            return _error(
                "too_many_streams",
                f"At most {events.MAX_STREAMS_PER_MACHINE} open streams per machine.",
            )
        return StreamingResponse(
            events.stream(deps, request, machine.id, session_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @mcp.custom_route("/v1/sessions/{id}/end", methods=["POST"])
    async def end(request: Request) -> JSONResponse:
        def handler(
            conn: sqlite3.Connection, machine: Machine, _: dict[str, Any]
        ) -> dict[str, Any]:
            now = deps.clock()
            deps.end_limiter.check(machine.id, now)
            actor = f"machine:{machine.name}"
            s = load_session(conn, _session_id(request))
            if s is None or not sessions.usable_by(s, machine):
                raise KampungError("unknown_session", "No such session.")
            sessions.end(conn, s.id, now)
            audit.record(conn, now, actor, "end_session", {"session": s.id})
            return {}

        return await _run(deps, request, handler)
