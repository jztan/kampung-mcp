"""GET /v1/sessions/{id}/events: a session's peek counts, pushed as they
change, so the adapter need not poll. The stream never writes last_seen: a
sleeping machine's half-open stream would keep it fresh for minutes, so the
adapter's own 30-second peek keeps liveness instead."""

import json
import threading
import time
from collections.abc import AsyncIterator
from typing import Any

import anyio
import anyio.to_thread
from starlette.requests import Request

from .deps import ServerDeps
from .identity import authenticate
from .limits import MAX_LIVE_SESSIONS_PER_MACHINE
from .mail import peek_counts
from .mailsignal import SIGNAL
from .models import load_session

SIGNAL_CHECK = 0.25  # seconds between looks at the in-memory version
PING_EVERY = 15.0  # seconds of silence before a ": ping" comment
RECHECK = 30.0  # seconds between recomputes even without a bump
MAX_STREAMS_PER_MACHINE = MAX_LIVE_SESSIONS_PER_MACHINE
RECOMPUTE_THREADS = 8  # recomputes never take the whole threadpool

_open: dict[int, int] = {}
_open_lock = threading.Lock()
_limiter: anyio.CapacityLimiter | None = None


def claim(machine_id: int) -> bool:
    """Count one more open stream for the machine; False at the cap."""
    with _open_lock:
        if _open.get(machine_id, 0) >= MAX_STREAMS_PER_MACHINE:
            return False
        _open[machine_id] = _open.get(machine_id, 0) + 1
        return True


def release(machine_id: int) -> None:
    with _open_lock:
        _open[machine_id] = max(0, _open.get(machine_id, 0) - 1)


def _recompute_limiter() -> anyio.CapacityLimiter:
    global _limiter
    if _limiter is None:  # created on first use, inside the event loop
        _limiter = anyio.CapacityLimiter(RECOMPUTE_THREADS)
    return _limiter


def _event(name: str, data: dict[str, Any]) -> str:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


class Slot:
    """One claimed stream slot, released once however the response ends."""

    def __init__(self, machine_id: int) -> None:
        self.machine_id = machine_id
        self._lock = threading.Lock()
        self._held = True

    def release(self) -> None:
        with self._lock:
            held, self._held = self._held, False
        if held:
            release(self.machine_id)


async def stream(
    deps: ServerDeps, request: Request, key: str, slot: Slot, session_id: int
) -> AsyncIterator[str]:
    """Yield SSE text until the client goes, the session ends or the key that
    opened the stream stops working (rotated or revoked, possibly by the CLI
    in another process, before the session was revived). Releases the slot;
    the response releases it too, in case this body never starts."""

    def counts() -> dict[str, object] | None:
        conn = deps.connect()
        try:
            machine = authenticate(conn, key)
            if machine is None or machine.id != slot.machine_id:
                return None
            s = load_session(conn, session_id)
            if s is None or s.ended_at is not None:
                return None
            return dict(peek_counts(conn, s, deps.clock()).to_dict())
        finally:
            conn.close()

    try:
        last: dict[str, object] | None = None
        seen = -1
        checked = sent = 0.0
        while True:
            now = time.monotonic()
            version = SIGNAL.current()  # read before the recompute
            if version != seen or now - checked >= RECHECK:
                seen, checked = version, now
                state = await anyio.to_thread.run_sync(
                    counts, limiter=_recompute_limiter()
                )
                if state is None:
                    if not await anyio.to_thread.run_sync(_key_works, deps, key):
                        return  # just close: the adapter falls back and sees 401
                    yield _event("ended", {})
                    return
                if state != last:
                    last, sent = state, now
                    yield _event("peek", state)
            if now - sent >= PING_EVERY:
                sent = now
                yield ": ping\n\n"
            if await request.is_disconnected():
                return
            await anyio.sleep(SIGNAL_CHECK)
    finally:
        slot.release()


def _key_works(deps: ServerDeps, key: str) -> bool:
    conn = deps.connect()
    try:
        return authenticate(conn, key) is not None
    finally:
        conn.close()
