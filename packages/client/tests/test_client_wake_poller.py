"""The adapter's wake poller: the wake file first, Channels as the fallback."""

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from client_testlib import (
    LiveServer,
    db_query,
    git_repo,
    hook_chain,
    peer_send,
    scripted_server,
    write_config,
)
from kampung_client import adapter, hook
from kampung_client.adapter import SessionResolver

REASON = "1 Kampung message(s) waiting. Call the kampung inbox tool to read them."
SID = "cc-1"
MISS = "idle wake not confirmed"


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send_notification(self, note: Any) -> None:
        self.sent.append(note)


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adapter, "CHANNEL_POLL", 0.05)
    monkeypatch.setattr(adapter, "ACK_WAIT", 0.4)
    monkeypatch.delenv("CLAUDE_JOB_DIR", raising=False)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return git_repo(tmp_path, "api")


def hooked_session(cwd: Path, **extra: object) -> dict[str, object]:
    """What the SessionStart hook would leave behind for this test process tree."""
    data = hook.register("claude-code", str(cwd), True, SID)
    data["ancestors"] = hook_chain()
    data.update(extra)
    hook.write_session_file(SID, data)
    return data


def poller(repo: Path, channels: bool) -> tuple[adapter._WakePoller, FakeClient]:
    p = adapter._WakePoller(SessionResolver("claude-code", str(repo), wait=0), channels)
    client = FakeClient()
    p.session = client
    return p, client


@contextlib.asynccontextmanager
async def background(work: Any) -> AsyncIterator[None]:
    task = asyncio.get_running_loop().create_task(work)
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def run_for(p: adapter._WakePoller, seconds: float) -> None:
    async with background(p.poll()):
        await asyncio.sleep(seconds)


async def until(check: Callable[[], bool], seconds: float = 5.0) -> None:
    end = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < end, "timed out waiting"
        await asyncio.sleep(0.02)


async def acker(tokens: list[str], skip: int = 0) -> None:
    """Stands in for Claude Code running the wake hook: acks every new token,
    after ignoring the first `skip` of them."""
    while True:
        token = hook.read_token(hook.wake_path(SID))
        if token and token not in tokens:
            tokens.append(token)
            if len(tokens) > skip:
                hook.write_ack(SID)
        await asyncio.sleep(0.02)


async def watcher(tokens: list[str]) -> None:
    """Records every token the wake file ever holds; never acks."""
    await acker(tokens, skip=10**6)


def misses() -> list[str]:
    log = hook.cache_dir() / "hooks.log"
    if not log.exists():
        return []
    return [x for x in log.read_text().splitlines() if MISS in x]


async def test_no_peek_without_a_wake_file_and_without_channels(
    home: Path, repo: Path
) -> None:
    httpd, hits = scripted_server(200)
    try:
        write_config(home, f"http://127.0.0.1:{httpd.server_port}", "kmp_x")
        hook.write_session_file(
            SID,
            {
                "session": "2",
                "suffix": "cd",
                "mailbox": "laptop/api",
                "cwd": str(repo),
                "written_at": time.time(),
                "hooks": True,
                "agent": "claude-code",
                "agent_session_id": SID,
                "ancestors": hook_chain(),
                "background": False,
            },
        )
        p, _ = poller(repo, channels=False)
        await run_for(p, 0.4)
        assert hits == []
        hook.ensure_wake_file(SID)
        await run_for(p, 0.4)
    finally:
        httpd.shutdown()
    assert "GET /v1/sessions/2/peek" in hits
    assert not [h for h in hits if h.startswith("POST")]


async def test_a_rise_writes_one_token_and_no_push_when_acked(
    home: Path, server: LiveServer, repo: Path
) -> None:
    hooked_session(repo)
    hook.ensure_wake_file(SID)
    p, client = poller(repo, channels=True)
    tokens: list[str] = []
    async with background(acker(tokens)), background(p.poll()):
        peer_send(server, "laptop/api", "hello")
        await until(lambda: len(tokens) == 1)
        await asyncio.sleep(0.5)  # no new mail: no new token
        assert len(tokens) == 1
        peer_send(server, "laptop/api", "again")
        await until(lambda: len(tokens) == 2)
        await asyncio.sleep(0.5)
    assert len(tokens) == 2 and client.sent == [] and misses() == []


async def test_held_mail_writes_no_token(
    home: Path, server: LiveServer, repo: Path
) -> None:
    hooked_session(repo)
    hook.ensure_wake_file(SID)
    p, client = poller(repo, channels=True)
    peer_send(server, "laptop/api", "held", held=True)
    await run_for(p, 0.6)
    assert hook.read_token(hook.wake_path(SID)) == "" and client.sent == []


async def test_one_retry_then_channels_push_when_never_acked(
    home: Path, server: LiveServer, repo: Path
) -> None:
    data = hooked_session(repo)
    hook.ensure_wake_file(SID)
    p, client = poller(repo, channels=True)
    tokens: list[str] = []
    async with background(watcher(tokens)), background(p.poll()):
        peer_send(server, "laptop/api", "hello")
        await until(lambda: len(client.sent) == 1)
        await asyncio.sleep(0.3)
        assert len(tokens) == 2 and len(client.sent) == 1
        assert client.sent[0].method == "notifications/claude/channel"
        assert client.sent[0].params == {"content": REASON}
        assert len(misses()) == 1
        assert misses()[0].endswith(
            f"idle wake not confirmed for laptop/api#{data['suffix']}: "
            "Claude Code did not run the FileChanged hook"
        )
        # The next rise tries the file first again, and is not logged again.
        peer_send(server, "laptop/api", "again")
        await until(lambda: len(client.sent) == 2)
        await asyncio.sleep(0.3)
    assert len(tokens) == 4 and len(client.sent) == 2 and len(misses()) == 1


async def test_a_late_ack_on_the_second_write_stops_the_push(
    home: Path, server: LiveServer, repo: Path
) -> None:
    hooked_session(repo)
    hook.ensure_wake_file(SID)
    p, client = poller(repo, channels=True)
    tokens: list[str] = []
    async with background(acker(tokens, skip=1)), background(p.poll()):
        peer_send(server, "laptop/api", "hello")
        await until(lambda: len(tokens) == 2)
        await asyncio.sleep(0.8)
    assert len(tokens) == 2 and client.sent == [] and misses() == []


async def test_no_push_without_channels(
    home: Path, server: LiveServer, repo: Path
) -> None:
    hooked_session(repo)
    hook.ensure_wake_file(SID)
    p, client = poller(repo, channels=False)
    async with background(p.poll()):
        peer_send(server, "laptop/api", "hello")
        await until(lambda: len(misses()) == 1)
        await asyncio.sleep(0.3)
    assert client.sent == [] and len(misses()) == 1


async def test_never_a_push_in_a_background_session(
    home: Path, server: LiveServer, repo: Path
) -> None:
    hooked_session(repo, background=True)
    hook.ensure_wake_file(SID)
    p, client = poller(repo, channels=True)
    async with background(p.poll()):
        peer_send(server, "laptop/api", "hello")
        await until(lambda: len(misses()) == 1)
        await asyncio.sleep(0.3)
    assert client.sent == []
    # The same session with no wake file at all: Channels alone, still no push.
    hook.delete_wake_files(SID)
    p, client = poller(repo, channels=True)
    await run_for(p, 0.6)
    assert client.sent == []


async def test_a_foreground_session_without_a_wake_file_is_pushed(
    home: Path, server: LiveServer, repo: Path
) -> None:
    hooked_session(repo)
    p, client = poller(repo, channels=True)
    async with background(p.poll()):
        peer_send(server, "laptop/api", "hello")
        await until(lambda: len(client.sent) == 1)
    assert client.sent[0].params == {"content": REASON}
    assert misses() == [] and not hook.wake_dir().exists()


async def test_a_wake_file_that_goes_away_is_not_logged_as_a_miss(
    home: Path, server: LiveServer, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooked_session(repo)
    hook.ensure_wake_file(SID)
    p, client = poller(repo, channels=True)

    async def gone(agent_session_id: str) -> bool:
        hook.delete_wake_files(agent_session_id)  # as SessionEnd would
        return False

    monkeypatch.setattr(p, "file_wake", gone)
    async with background(p.poll()):
        peer_send(server, "laptop/api", "hello")
        await until(lambda: len(client.sent) == 1)
    assert misses() == []


async def test_an_ended_session_is_not_revived(
    home: Path, server: LiveServer, repo: Path
) -> None:
    data = hooked_session(repo)
    hook.ensure_wake_file(SID)
    status, _ = hook.api("POST", f"/v1/sessions/{data['session']}/end")
    assert status == 200
    peer_send(server, "laptop/api", "hello")
    p, client = poller(repo, channels=True)
    await run_for(p, 0.6)
    assert hook.read_token(hook.wake_path(SID)) == "" and client.sent == []
    live = "SELECT count(*) FROM sessions WHERE ended_at IS NULL AND agent = ?"
    assert db_query(server, live, "claude-code") == [(0,)]


async def test_file_wake_does_not_bring_back_a_deleted_wake_file(
    home: Path, repo: Path
) -> None:
    p, _ = poller(repo, channels=False)
    assert await asyncio.wait_for(p.file_wake(SID), 0.2) is False
    assert not hook.wake_dir().exists()


async def test_file_wake_is_true_as_soon_as_the_token_is_acked(
    home: Path, repo: Path
) -> None:
    hook.ensure_wake_file(SID)
    p, _ = poller(repo, channels=False)
    tokens: list[str] = []
    async with background(acker(tokens)):
        assert await asyncio.wait_for(p.file_wake(SID), 0.35) is True
    assert len(tokens) == 1


def test_revive_sends_idle_wake(home: Path, server: LiveServer, repo: Path) -> None:
    data = hooked_session(repo)
    live = "SELECT idle_wake FROM sessions WHERE ended_at IS NULL AND agent = ?"
    assert db_query(server, live, "claude-code") == [(0,)]
    hook.ensure_wake_file(SID)
    resolver = SessionResolver("claude-code", str(repo), wait=0)
    assert resolver.current()["session"] == data["session"]
    hook.api("POST", f"/v1/sessions/{data['session']}/end")
    resolver.refresh()
    assert db_query(server, live, "claude-code") == [(1,)]


async def test_cancelling_the_poller_closes_its_stream(
    home: Path, server: LiveServer, repo: Path
) -> None:
    # A stream task left behind after the loop ends kept the event loop
    # busy at shutdown on Windows (CI hung until the job timed out).
    hooked_session(repo)
    hook.ensure_wake_file(SID)
    p, _ = poller(repo, channels=False)
    async with background(p.poll()):
        await until(lambda: p.source.state == "streaming")
        stream = p.source.task
        assert stream is not None and not stream.done()
    assert stream.done() and p.source.task is None
