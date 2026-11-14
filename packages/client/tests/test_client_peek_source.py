"""PeekSource: mail counts from the server's event stream, polling as the
fallback."""

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from client_testlib import LiveServer, git_repo, peer_send
from kampung_client import adapter, hook

SID = "cc-1"


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adapter, "CHANNEL_POLL", 0.05)
    monkeypatch.setattr(adapter, "STREAM_RETRY", 0.05)
    monkeypatch.delenv("CLAUDE_JOB_DIR", raising=False)


def _session(tmp_path: Path) -> str:
    data = hook.register("claude-code", str(git_repo(tmp_path, "api")), True, SID)
    return str(data["session"])


def _count_peeks(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    real = hook.api

    def counting(method: str, path: str, *a: Any, **kw: Any) -> Any:
        if path.endswith("/peek"):
            seen.append(path)
        return real(method, path, *a, **kw)

    monkeypatch.setattr(hook, "api", counting)
    return seen


async def _until(check: Callable[[], bool], seconds: float = 5.0) -> None:
    end = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < end, "timed out waiting"
        await asyncio.sleep(0.02)


@contextlib.asynccontextmanager
async def _pumping(
    source: adapter.PeekSource, sid: str, bodies: list[dict[str, object]]
) -> AsyncIterator[None]:
    """Calls get() in a loop the way the wake loops do."""

    async def pump() -> None:
        while True:
            body, waited = await source.get(sid)
            bodies.append(body)
            if not waited:
                await asyncio.sleep(adapter.CHANNEL_POLL)

    task = asyncio.get_running_loop().create_task(pump())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await source.close()


async def test_streaming_stops_polling_and_pushes_new_mail(
    home: Path, server: LiveServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = _session(tmp_path)
    peeks = _count_peeks(monkeypatch)
    source = adapter.PeekSource()
    bodies: list[dict[str, object]] = []
    async with _pumping(source, sid, bodies):
        await _until(lambda: source.state == "streaming")
        settled = len(peeks)
        await asyncio.sleep(0.5)
        assert len(peeks) == settled  # no polling while the stream is up
        start = time.monotonic()
        peer_send(server, "laptop/api", "hello")
        await _until(lambda: bool(bodies) and bodies[-1].get("wakeable") == 1)
        assert time.monotonic() - start < 1.5


async def test_heartbeat_peeks_while_streaming(
    home: Path, server: LiveServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(adapter, "HEARTBEAT", 0.3)
    sid = _session(tmp_path)
    peeks = _count_peeks(monkeypatch)
    source = adapter.PeekSource()
    async with _pumping(source, sid, []):
        await _until(lambda: source.state == "streaming")
        settled = len(peeks)
        # Two heartbeats, with room for a slow runner; a stream alone never peeks.
        await _until(lambda: len(peeks) >= settled + 2)


async def test_a_dropped_stream_polls_at_once_then_reconnects(
    home: Path, server: LiveServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = _session(tmp_path)
    source = adapter.PeekSource()
    opened: list[str] = []
    real = source._read

    async def flaky(s: str) -> None:
        opened.append(s)
        if len(opened) == 1:
            source.state = "streaming"
            raise ConnectionError("dropped")
        await real(s)

    monkeypatch.setattr(source, "_read", flaky)
    peeks = _count_peeks(monkeypatch)
    async with _pumping(source, sid, []):
        await _until(lambda: len(opened) >= 2)
        assert peeks  # polled while it was down
        await _until(lambda: source.state == "streaming")


async def test_a_new_target_session_gets_a_new_stream(
    home: Path, server: LiveServer, tmp_path: Path
) -> None:
    first = _session(tmp_path)
    second = str(
        hook.register("claude-code", str(git_repo(tmp_path, "web")), True, "cc-2")[
            "session"
        ]
    )
    source = adapter.PeekSource()
    await source.get(first)
    await _until(lambda: source.state == "streaming")
    task = source.task
    await source.get(second)
    assert source.sid == second and source.task is not task
    await source.close()


def test_a_bare_404_means_an_older_server() -> None:
    assert adapter.older_server_404(b"Not Found")
    assert adapter.older_server_404(b"")
    assert not adapter.older_server_404(
        b'{"error": "unknown_session", "hint": "No such session"}'
    )


async def test_an_older_server_is_polled_for_good(
    home: Path, server: LiveServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = _session(tmp_path)
    source = adapter.PeekSource()
    real_path = adapter.EVENTS_PATH
    monkeypatch.setattr(adapter, "EVENTS_PATH", real_path + "-missing")
    peeks = _count_peeks(monkeypatch)
    async with _pumping(source, sid, []):
        await _until(lambda: source.old_server)
        before = len(peeks)
        await asyncio.sleep(0.3)
        assert len(peeks) > before
        assert source.task is None or source.task.done()


async def test_an_ended_session_is_not_mistaken_for_an_older_server(
    home: Path, server: LiveServer, tmp_path: Path
) -> None:
    sid = _session(tmp_path)
    server.admin.end_session(int(sid), "admin:cli")
    source = adapter.PeekSource()
    async with _pumping(source, sid, []):
        await _until(lambda: source.state == "ended")
        assert not source.old_server


async def test_a_revived_session_gets_its_stream_back(
    home: Path, server: LiveServer, tmp_path: Path
) -> None:
    # The sweep ends a session after a long sleep; the hooks later revive the
    # same id. The adapter must stream again, not poll for the rest of its life.
    sid = _session(tmp_path)
    source = adapter.PeekSource()
    async with _pumping(source, sid, []):
        await _until(lambda: source.state == "streaming")
        server.admin.end_session(int(sid), "admin:cli")
        await _until(lambda: source.state == "ended")
        again = hook.register("claude-code", str(tmp_path / "api"), True, SID)
        assert str(again["session"]) == sid  # revived: the same row
        await _until(lambda: source.state == "streaming")


async def test_streaming_waits_for_the_first_event(home: Path, tmp_path: Path) -> None:
    # A proxy that sends the headers but buffers the body: no event ever
    # arrives, so the adapter must keep polling rather than wait on a stream.
    import socket
    import threading

    import uvicorn
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, StreamingResponse
    from starlette.routing import Route

    from client_testlib import write_config

    peeks: list[str] = []

    async def silent(request: Request) -> StreamingResponse:
        async def never() -> Any:
            await asyncio.sleep(3600)
            yield ""

        return StreamingResponse(never(), media_type="text/event-stream")

    async def peek(request: Request) -> JSONResponse:
        peeks.append(request.url.path)
        return JSONResponse({"waiting": 0, "wakeable": 0, "newest": None})

    app = Starlette(
        routes=[
            Route("/v1/sessions/{sid}/events", silent),
            Route("/v1/sessions/{sid}/peek", peek),
        ]
    )
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    srv = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            timeout_graceful_shutdown=1,
        )
    )
    threading.Thread(target=srv.run, daemon=True).start()
    while not srv.started:
        await asyncio.sleep(0.02)
    write_config(home, f"http://127.0.0.1:{port}", "kmp_x")
    source = adapter.PeekSource()
    try:
        async with _pumping(source, "7", []):
            await asyncio.sleep(0.5)
            assert source.state == "connecting"
            assert len(peeks) >= 3  # still polling
    finally:
        srv.should_exit = True
