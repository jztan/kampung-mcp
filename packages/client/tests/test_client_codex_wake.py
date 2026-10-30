"""The Codex waker: polls peek and wakes an idle Codex thread with `codex queue`."""

import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from client_testlib import LiveServer, RawMCP, git_repo, peer_send, run_hook
from kampung_client import hook

WAKE = (
    "[kampung] 1 Kampung message(s) waiting. Call the kampung inbox tool to "
    "read them."
)


def fake_codex(
    tmp_path: Path, exit_code: int = 0, hang_first: float = 0
) -> tuple[Path, Path]:
    """A `codex` stand-in that logs its arguments, one JSON line per call.
    With `hang_first`, the first call sleeps that long, then writes `finished`."""
    if sys.platform == "win32":
        pytest.skip("waking Codex with codex queue is not supported on Windows yet")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "codex-calls.jsonl"
    script = bindir / "codex"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys, time\n"
        f"open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        f"if {hang_first} and len(open({str(log)!r}).readlines()) == 1:\n"
        f"    time.sleep({hang_first})\n"
        f"    open({str(tmp_path / 'finished')!r}, 'w').close()\n"
        f"sys.exit({exit_code})\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script, log


def calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


def wait_for_calls(log: Path, n: int, seconds: float) -> list[list[str]]:
    end = time.monotonic() + seconds
    while time.monotonic() < end and len(calls(log)) < n:
        time.sleep(0.2)
    return calls(log)


def codex_start(home: Path, repo: Path, sid: str = "cx-1") -> dict[str, object]:
    run_hook(
        home,
        {
            "hook_event_name": "SessionStart",
            "session_id": sid,
            "cwd": str(repo),
            "source": "startup",
        },
        "--agent",
        "codex",
    )
    data = hook.read_session_file(sid)
    assert data is not None
    return data


def call_inbox(mcp: RawMCP, sid: str, rid: str = "c1") -> dict[str, object]:
    mcp.send(
        {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "tools/call",
            "params": {
                "name": "inbox",
                "arguments": {},
                "_meta": {"x-codex-turn-metadata": {"session_id": sid}},
            },
        }
    )
    reply = [m for m in mcp.read(10, until=rid) if m.get("id") == rid]
    assert reply, "no reply to tools/call"
    return reply[0]


def log_lines(home: Path, name: str) -> list[str]:
    path = home / ".cache" / "kampung" / "hooks.log"
    if not path.exists():
        return []
    return [line for line in path.read_text().splitlines() if f" {name}:" in line]


def test_waker_queues_once_per_new_wakeable_mail(
    home: Path, server: LiveServer, tmp_path: Path
) -> None:
    repo = git_repo(tmp_path, "api")
    data = codex_start(home, repo)
    codex, log = fake_codex(tmp_path)
    mcp = RawMCP(home, repo, "--codex-bin", str(codex), agent="codex")
    try:
        mcp.initialize()
        call_inbox(mcp, "cx-1")
        peer_send(server, f"laptop/api#{data['suffix']}", "wake up")
        got = wait_for_calls(log, 1, 8)
        assert got == [["queue", "--thread", "cx-1", "--message", WAKE]]
        time.sleep(5)
        assert len(calls(log)) == 1  # same mail, no repeat
    finally:
        mcp.close()


def test_waker_silent_before_binding(
    home: Path, server: LiveServer, tmp_path: Path
) -> None:
    repo = git_repo(tmp_path, "api")
    codex_start(home, repo)
    codex, log = fake_codex(tmp_path)
    mcp = RawMCP(home, repo, "--codex-bin", str(codex), agent="codex")
    try:
        mcp.initialize()
        peer_send(server, "laptop/api", "nobody bound yet")
        time.sleep(6)
        assert calls(log) == []
    finally:
        mcp.close()


def test_waker_stops_after_session_end(
    home: Path, server: LiveServer, tmp_path: Path
) -> None:
    repo = git_repo(tmp_path, "api")
    codex_start(home, repo)
    codex, log = fake_codex(tmp_path)
    mcp = RawMCP(home, repo, "--codex-bin", str(codex), agent="codex")
    try:
        mcp.initialize()
        call_inbox(mcp, "cx-1")
        run_hook(
            home,
            {"hook_event_name": "SessionEnd", "session_id": "cx-1", "reason": "x"},
            "--agent",
            "codex",
        )
        peer_send(server, "laptop/api", "after the end")
        time.sleep(6)
        assert calls(log) == []
    finally:
        mcp.close()


def test_waker_queue_failure_logged_once(
    home: Path, server: LiveServer, tmp_path: Path
) -> None:
    repo = git_repo(tmp_path, "api")
    data = codex_start(home, repo)
    codex, log = fake_codex(tmp_path, exit_code=1)
    mcp = RawMCP(home, repo, "--codex-bin", str(codex), agent="codex")
    try:
        mcp.initialize()
        call_inbox(mcp, "cx-1")
        peer_send(server, f"laptop/api#{data['suffix']}", "one")
        wait_for_calls(log, 1, 8)
        peer_send(server, f"laptop/api#{data['suffix']}", "two")
        assert len(wait_for_calls(log, 2, 8)) == 2  # retried on the next rise
        assert len(log_lines(home, "codex-wake")) == 1
        reply = call_inbox(mcp, "cx-1", "c2")
        text = json.dumps(reply)
        assert "one" in text and "two" in text  # tools still work
    finally:
        mcp.close()


def test_waker_kills_a_hung_codex_queue_and_wakes_again(
    home: Path, server: LiveServer, tmp_path: Path
) -> None:
    repo = git_repo(tmp_path, "api")
    data = codex_start(home, repo)
    codex, log = fake_codex(tmp_path, hang_first=7)  # past the 5 s queue timeout
    mcp = RawMCP(home, repo, "--codex-bin", str(codex), agent="codex")
    try:
        mcp.initialize()
        call_inbox(mcp, "cx-1")
        peer_send(server, f"laptop/api#{data['suffix']}", "one")
        wait_for_calls(log, 1, 8)
        first = time.monotonic()  # the hung call has started
        end = time.monotonic() + 10
        while time.monotonic() < end and not log_lines(home, "codex-wake"):
            time.sleep(0.2)
        assert any("timed out" in line for line in log_lines(home, "codex-wake"))
        peer_send(server, f"laptop/api#{data['suffix']}", "two")
        assert len(wait_for_calls(log, 2, 8)) == 2  # still polling after the hang
        time.sleep(max(0.0, first + 9 - time.monotonic()))
        assert not (tmp_path / "finished").exists()  # the hung call was killed
    finally:
        mcp.close()


def test_waker_server_down_logged_once(
    home: Path, server: LiveServer, tmp_path: Path
) -> None:
    repo = git_repo(tmp_path, "api")
    codex_start(home, repo)
    codex, log = fake_codex(tmp_path)
    mcp = RawMCP(home, repo, "--codex-bin", str(codex), agent="codex")
    try:
        mcp.initialize()
        call_inbox(mcp, "cx-1")
        server.uvicorn.should_exit = True
        time.sleep(7)
        assert calls(log) == []
        assert len(log_lines(home, "codex-wake")) == 1
    finally:
        mcp.close()


def test_no_waker_without_codex_bin(
    home: Path, server: LiveServer, tmp_path: Path
) -> None:
    # Even with a `codex` on PATH, wake needs the explicit --codex-bin.
    repo = git_repo(tmp_path, "api")
    data = codex_start(home, repo)
    codex, log = fake_codex(tmp_path)
    path = f"{codex.parent}{os.pathsep}{os.environ['PATH']}"
    mcp = RawMCP(home, repo, agent="codex", path=path)
    try:
        mcp.initialize()
        call_inbox(mcp, "cx-1")
        peer_send(server, f"laptop/api#{data['suffix']}", "no waker")
        time.sleep(6)
        assert calls(log) == []
    finally:
        mcp.close()


async def test_waker_backs_off_while_server_is_down(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from kampung_client.adapter import CodexWaker, SessionResolver

    resolver = SessionResolver("codex", "/w/api", wait=0)
    resolver.bound = "cx-1"
    monkeypatch.setattr(resolver, "target", lambda: {"session": "1"})

    def down(*args: object, **kwargs: object) -> None:
        raise hook.HookError("GET /v1/sessions/1/peek: connection refused")

    monkeypatch.setattr(hook, "api", down)
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)
        if len(delays) >= 4:
            raise asyncio.CancelledError

    monkeypatch.setattr("kampung_client.adapter.asyncio.sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await CodexWaker(resolver, "/bin/false").poll()
    assert delays == [4.0, 8.0, 16.0, 30.0]


def test_waker_waits_for_a_busy_turn_to_end(
    home: Path, server: LiveServer, tmp_path: Path
) -> None:
    # A queued prompt runs only after the current turn, when the nudge or the
    # Stop hook has already had the mail read: an empty wake.
    repo = git_repo(tmp_path, "api")
    data = codex_start(home, repo)
    codex, log = fake_codex(tmp_path)
    mcp = RawMCP(home, repo, "--codex-bin", str(codex), agent="codex")

    def hook_event(name: str, **kw: object) -> None:
        ev = {"hook_event_name": name, "session_id": "cx-1", **kw}
        run_hook(home, ev, "--agent", "codex")

    try:
        mcp.initialize()
        call_inbox(mcp, "cx-1")
        hook_event("UserPromptSubmit", prompt="typed")
        peer_send(server, f"laptop/api#{data['suffix']}", "mid-turn")
        time.sleep(5)
        assert calls(log) == []
        hook_event("Stop", stop_hook_active=True)  # turn ends, mail still unread
        assert wait_for_calls(log, 1, 8) == [
            ["queue", "--thread", "cx-1", "--message", WAKE]
        ]
    finally:
        mcp.close()


def test_waker_wakes_for_mail_that_arrives_late_in_a_wake_turn(
    home: Path, server: LiveServer, tmp_path: Path
) -> None:
    # The wake turn reads the first message; another lands before its Stop.
    # The count from the first wake must not hide the second one.
    repo = git_repo(tmp_path, "api")
    data = codex_start(home, repo)
    codex, log = fake_codex(tmp_path)
    mcp = RawMCP(home, repo, "--codex-bin", str(codex), agent="codex")
    to = f"laptop/api#{data['suffix']}"

    def hook_event(name: str, **kw: object) -> None:
        ev = {"hook_event_name": name, "session_id": "cx-1", **kw}
        run_hook(home, ev, "--agent", "codex")

    try:
        mcp.initialize()
        call_inbox(mcp, "cx-1")
        peer_send(server, to, "first")
        assert len(wait_for_calls(log, 1, 8)) == 1
        hook_event("UserPromptSubmit", prompt=WAKE)
        call_inbox(mcp, "cx-1", "c2")
        time.sleep(3)
        peer_send(server, to, "second")
        time.sleep(3)
        hook_event("Stop", stop_hook_active=True)
        assert len(wait_for_calls(log, 2, 8)) == 2
    finally:
        mcp.close()
