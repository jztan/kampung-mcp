"""The connect page: where the owner's browser lands from Claude and types the
one-time code that `kampung connect` printed.

Server-rendered, no JavaScript, CSS inlined (the admin stylesheet sits behind
the admin token and Caddy's /admin rule). It shows nothing from the OAuth
request, because the client name is chosen by whoever made it.
"""

import html
import sqlite3
import threading
from collections import OrderedDict, deque
from collections.abc import Callable
from datetime import datetime
from urllib.parse import parse_qs

from fastmcp import FastMCP
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from . import audit, db, oauth_store
from .deps import ServerDeps
from .limits import (
    CONNECT_FAILURE_LIMIT,
    CONNECT_FAILURE_WINDOW,
    OAUTH_CONNECT_ATTEMPTS,
)
from .oauth import KampungOAuth
from .throttle import MAX_IPS, client_key

MAX_FORM_BYTES = 4096

SECURITY_HEADERS = {
    # No form-action: browsers apply it to the redirect after the form, which
    # would block the return to claude.ai.
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; "
        "base-uri 'none'"
    ),
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connect Claude</title>
<style>
:root{{--bg:#f6f7f5;--surface:#fff;--line:#dde2dc;--fg:#18201c;--muted:#5d6a63;
--accent:#1f6b57;--accent-fg:#fff;--bad:#b3261e;--r:6px;
--font:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif}}
@media (prefers-color-scheme: dark){{:root{{--bg:#0f1613;--surface:#151e1a;
--line:#283630;--fg:#e3ebe6;--muted:#92a39a;--accent:#5fbf9f;--accent-fg:#0b1411;
--bad:#f08a84}}}}
html{{color-scheme:light dark}}
body{{margin:0;font:16px/1.5 var(--font);color:var(--fg);background:var(--bg)}}
main{{max-width:26em;margin:0 auto;padding:16px;box-sizing:border-box}}
h1{{font-size:22px;margin:24px 0 8px}}
p{{margin:0 0 12px}}
.card{{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);
padding:16px}}
label{{display:block;font-weight:600;margin-top:8px}}
input,button{{font:inherit;min-height:44px;width:100%;box-sizing:border-box;
margin-top:8px;border-radius:var(--r)}}
input{{padding:0 12px;border:1px solid var(--line);background:var(--bg);
color:var(--fg);font-family:ui-monospace,Menlo,Consolas,monospace}}
button{{border:0;background:var(--accent);color:var(--accent-fg);font-weight:600;
cursor:pointer}}
input:focus-visible,button:focus-visible{{outline:2px solid var(--accent);
outline-offset:2px}}
.error{{color:var(--bad);font-weight:600}}
.muted{{color:var(--muted)}}
</style></head>
<body><main>
<h1>Connect Claude</h1>
<div class="card">
<p>Claude will return you to <strong>claude.ai</strong>. Enter the code printed
by <code>kampung connect</code>.</p>
{message}
<form method="post" action="/oauth/connect">
<input type="hidden" name="request" value="{request}">
<label for="code">Code</label>
<input id="code" name="code" autocomplete="one-time-code" autocapitalize="off"
spellcheck="false" autofocus>
<button type="submit">Connect</button>
</form>
<p class="muted">The code works once, for 10 minutes. If it has expired, run
<code>kampung connect</code> again.</p>
</div></main></body></html>
"""


class ConnectLimiter:
    """Refuse the code form to an address after too many wrong codes."""

    def __init__(
        self,
        clock: Callable[[], datetime],
        limit: int = CONNECT_FAILURE_LIMIT,
        max_ips: int = MAX_IPS,
    ) -> None:
        self._clock = clock
        self._limit = limit
        self._max_ips = max_ips
        self._failures: OrderedDict[str, deque[datetime]] = OrderedDict()
        self._lock = threading.Lock()

    def _recent(self, address: str, now: datetime) -> deque[datetime]:
        times = self._failures.setdefault(address, deque())
        while times and times[0] <= now - CONNECT_FAILURE_WINDOW:
            times.popleft()
        return times

    def allowed(self, address: str) -> bool:
        with self._lock:
            times = self._recent(address, self._clock())
            if not times:
                del self._failures[address]
                return True
            return len(times) < self._limit

    def failed(self, address: str) -> None:
        with self._lock:
            times = self._recent(address, self._clock())
            times.append(self._clock())
            self._failures.move_to_end(address)
            while len(self._failures) > self._max_ips:
                self._failures.popitem(last=False)


def _page(request_id: str, message: str = "") -> str:
    return PAGE.format(request=html.escape(request_id, quote=True), message=message)


def _respond(status: int, body: str) -> HTMLResponse:
    return HTMLResponse(body, status_code=status, headers=SECURITY_HEADERS)


def _plain(status: int, text: str) -> HTMLResponse:
    body = f"<!doctype html><title>Connect Claude</title><p>{html.escape(text)}</p>"
    return _respond(status, body)


UNKNOWN = "This connect request is unknown or has expired. Run kampung connect again."


async def _read_form(request: Request) -> dict[str, str] | None:
    declared = request.headers.get("content-length", "")
    if declared.isascii() and declared.isdigit() and int(declared) > MAX_FORM_BYTES:
        return None
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_FORM_BYTES:
            return None
    try:
        form = parse_qs(raw.decode(), keep_blank_values=True)
    except UnicodeDecodeError:
        return None
    return {name: values[0] for name, values in form.items() if values}


def register_connect_routes(
    mcp: FastMCP, deps: ServerDeps, provider: KampungOAuth, limiter: ConnectLimiter
) -> None:
    def live(conn: sqlite3.Connection, request_id: str) -> bool:
        return oauth_store.load_request(conn, request_id, deps.clock()) is not None

    @mcp.custom_route("/oauth/connect", methods=["GET"])
    async def show(request: Request) -> Response:
        request_id = request.query_params.get("request", "")

        def work() -> bool:
            conn = deps.connect()
            try:
                return bool(request_id) and live(conn, request_id)
            finally:
                conn.close()

        if not await run_in_threadpool(work):
            return _plain(400, UNKNOWN)
        return _respond(200, _page(request_id))

    @mcp.custom_route("/oauth/connect", methods=["POST"])
    async def submit(request: Request) -> Response:
        form = await _read_form(request)
        if form is None:
            return _plain(400, "The form is not valid.")
        request_id = form.get("request", "")
        code = form.get("code", "").strip()
        address = client_key(request.scope)
        if not limiter.allowed(address):
            return _plain(429, "Too many wrong codes. Try again in 15 minutes.")

        def work() -> Response:
            now = deps.clock()
            conn = deps.connect()
            try:
                if not request_id or not live(conn, request_id):
                    return _plain(400, UNKNOWN)
                with db.transaction(conn):
                    machine_id = (
                        oauth_store.take_connect_code(conn, code, now) if code else None
                    )
                    if machine_id is None:
                        attempts = oauth_store.fail_request(conn, request_id)
                        if attempts >= OAUTH_CONNECT_ATTEMPTS:
                            audit.record(
                                conn,
                                now,
                                "oauth",
                                "connect_burned",
                                {"request": request_id},
                            )
            finally:
                conn.close()
            if machine_id is None:
                limiter.failed(address)
                if attempts >= OAUTH_CONNECT_ATTEMPTS:
                    return _plain(400, UNKNOWN)
                message = '<p class="error">Wrong code.</p>'
                return _respond(401, _page(request_id, message))
            try:
                url = provider.complete_connect(request_id, machine_id, now)
            except ValueError:
                return _plain(400, UNKNOWN)
            return RedirectResponse(url, status_code=303, headers=SECURITY_HEADERS)

        return await run_in_threadpool(work)
