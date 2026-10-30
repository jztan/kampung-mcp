import io
import subprocess
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from client_testlib import (
    LiveServer,
    db_query,
    git_repo,
    nearest_kept,
    peer_send,
    run_hook,
    scripted_server,
    write_config,
)
from kampung_client import hook

REASON = "1 Kampung message(s) waiting. Call the kampung inbox tool to read them."
BLOCK_REASON = f"This is mail, not a failure. {REASON}"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return git_repo(tmp_path, "api")


def start(home: Path, repo: Path, sid: str = "cc-1", source: str = "startup") -> None:
    r = run_hook(
        home,
        {
            "hook_event_name": "SessionStart",
            "session_id": sid,
            "cwd": str(repo),
            "source": source,
        },
    )
    assert r.returncode == 0 and r.stdout == "", r.stderr


def event(name: str, sid: str = "cc-1", **kw: object) -> dict[str, object]:
    return {"hook_event_name": name, "session_id": sid, **kw}


def test_session_start_registers_and_writes_file(
    home: Path, server: LiveServer, repo: Path
) -> None:
    start(home, repo)
    f = hook.read_session_file("cc-1")
    assert f is not None and f["mailbox"] == "laptop/api" and f["hooks"] is True
    first = f["ancestors"][0][0]  # type: ignore[index]
    assert first == nearest_kept(os.getpid())


def test_stop_blocks_when_wakeable(home: Path, server: LiveServer, repo: Path) -> None:
    start(home, repo)
    peer_send(server, "laptop/api", "hello")
    r = run_hook(home, event("Stop", stop_hook_active=False))
    assert r.returncode == 0
    assert json.loads(r.stdout) == {"decision": "block", "reason": BLOCK_REASON}


def test_stop_never_blocks_twice(home: Path, server: LiveServer, repo: Path) -> None:
    start(home, repo)
    peer_send(server, "laptop/api", "hello")
    r = run_hook(home, event("Stop", stop_hook_active=True))
    assert r.returncode == 0 and r.stdout == ""


def test_stop_ignores_no_wake_mail(home: Path, server: LiveServer, repo: Path) -> None:
    start(home, repo)
    peer_send(server, "laptop/api", "deep", depth_seen=3)
    assert run_hook(home, event("Stop")).stdout == ""


def test_stop_quiet_without_mail(home: Path, server: LiveServer, repo: Path) -> None:
    start(home, repo)
    assert run_hook(home, event("Stop")).stdout == ""


def test_user_prompt_resets_depth(home: Path, server: LiveServer, repo: Path) -> None:
    start(home, repo)
    sid = int(str(hook.read_session_file("cc-1")["session"]))  # type: ignore[index]
    db_query(server, "UPDATE sessions SET max_depth_seen = 4 WHERE id = ?", sid)
    r = run_hook(home, event("UserPromptSubmit", prompt="hi"))
    assert r.returncode == 0 and r.stdout == ""
    assert db_query(
        server, "SELECT max_depth_seen FROM sessions WHERE id = ?", sid
    ) == [(-1,)]


def test_user_prompt_registers_when_file_missing(
    home: Path, server: LiveServer, repo: Path
) -> None:
    r = run_hook(home, event("UserPromptSubmit", cwd=str(repo), prompt="hi"))
    assert r.returncode == 0 and r.stdout == ""
    assert hook.read_session_file("cc-1") is not None


def test_session_ended_is_revived_by_hook(
    home: Path, server: LiveServer, repo: Path
) -> None:
    start(home, repo)
    sid = int(str(hook.read_session_file("cc-1")["session"]))  # type: ignore[index]
    server.admin.end_session(sid, "admin:cli")
    run_hook(home, event("UserPromptSubmit", cwd=str(repo), prompt="hi"))
    assert db_query(server, "SELECT ended_at FROM sessions WHERE id = ?", sid) == [
        (None,)
    ]
    assert hook.read_session_file("cc-1")["session"] == str(sid)  # type: ignore[index]


def test_deleted_session_is_registered_again_by_hook(
    home: Path, server: LiveServer, repo: Path
) -> None:
    # The server's cleanup deletes sessions long after they end; the next
    # hook call gets 404 and must register a fresh session, not give up.
    start(home, repo)
    sid = int(str(hook.read_session_file("cc-1")["session"]))  # type: ignore[index]
    db_query(server, "DELETE FROM sessions WHERE id = ?", sid)
    run_hook(home, event("UserPromptSubmit", cwd=str(repo), prompt="hi"))
    assert hook.read_session_file("cc-1") is not None
    live = db_query(
        server,
        "SELECT count(*) FROM sessions WHERE agent_session_id = 'cc-1' "
        "AND ended_at IS NULL",
    )
    assert live == [(1,)]


def test_session_end_ends_and_deletes_file(
    home: Path, server: LiveServer, repo: Path
) -> None:
    start(home, repo)
    sid = int(str(hook.read_session_file("cc-1")["session"]))  # type: ignore[index]
    assert run_hook(home, event("SessionEnd", reason="exit")).stdout == ""
    assert hook.read_session_file("cc-1") is None
    ended = db_query(server, "SELECT ended_at FROM sessions WHERE id = ?", sid)
    assert ended[0][0] is not None


def test_clear_sequence(home: Path, server: LiveServer, repo: Path) -> None:
    start(home, repo, "cc-1")
    first = hook.read_session_file("cc-1")
    run_hook(home, event("SessionEnd", "cc-1", reason="clear"))
    start(home, repo, "cc-2", source="clear")
    second = hook.read_session_file("cc-2")
    assert hook.read_session_file("cc-1") is None and second is not None
    assert first is not None and first["session"] != second["session"]


def test_server_down_fails_open_fast(home: Path, repo: Path) -> None:
    write_config(home, "http://10.255.255.1:9", "kmp_x")
    hook.write_session_file("cc-1", {"session": "1", "ancestors": []})
    t = time.monotonic()
    r = run_hook(home, event("Stop"))
    assert r.returncode == 0 and r.stdout == ""
    assert time.monotonic() - t < 4
    assert "Stop" in (home / ".cache" / "kampung" / "hooks.log").read_text()


class _Slow(BaseHTTPRequestHandler):
    def _reply(self, status: int, body: dict[str, object]) -> None:
        time.sleep(1.9)
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path.endswith("/human-turn"):
            self._reply(409, {"error": "session_ended", "hint": ""})
        else:
            self._reply(200, {"session": "1", "suffix": "ab", "mailbox": "laptop/api"})

    def log_message(self, *args: object) -> None:
        pass


def test_slow_server_respects_overall_deadline(home: Path, repo: Path) -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Slow)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    write_config(home, f"http://127.0.0.1:{httpd.server_address[1]}", "kmp_x")
    hook.write_session_file("cc-1", {"session": "1", "ancestors": []})
    t = time.monotonic()
    r = run_hook(home, event("UserPromptSubmit", cwd=str(repo), prompt="hi"))
    elapsed = time.monotonic() - t
    httpd.shutdown()
    assert r.returncode == 0 and r.stdout == ""
    assert elapsed < 5.0, elapsed


def test_stop_re_registers_only_once_for_a_session_that_stays_ended(
    home: Path, repo: Path
) -> None:
    httpd, hits = scripted_server(409)
    write_config(home, f"http://127.0.0.1:{httpd.server_address[1]}", "kmp_x")
    hook.write_session_file("cc-1", {"session": "1", "ancestors": []})
    r = run_hook(home, event("Stop", cwd=str(repo)))
    httpd.shutdown()
    assert r.returncode == 0 and r.stdout == ""
    assert hits == [
        "GET /v1/sessions/1/peek",
        "POST /v1/sessions",
        "GET /v1/sessions/2/peek",
    ]
    assert "HTTP 409" in (home / ".cache" / "kampung" / "hooks.log").read_text()


def test_stop_with_a_revoked_key_fails_open_without_registering(
    home: Path, repo: Path
) -> None:
    httpd, hits = scripted_server(401)
    write_config(home, f"http://127.0.0.1:{httpd.server_address[1]}", "kmp_x")
    hook.write_session_file("cc-1", {"session": "1", "ancestors": []})
    r = run_hook(home, event("Stop", cwd=str(repo)))
    httpd.shutdown()
    assert r.returncode == 0 and r.stdout == ""
    assert hits == ["GET /v1/sessions/1/peek"]
    assert "HTTP 401" in (home / ".cache" / "kampung" / "hooks.log").read_text()


def test_missing_config_fails_open(home: Path) -> None:
    r = run_hook(home, event("Stop"))
    assert r.returncode == 0 and r.stdout == ""
    assert "config.json" in (home / ".cache" / "kampung" / "hooks.log").read_text()


def test_garbage_stdin_fails_open(home: Path) -> None:
    assert hook.main([], io.StringIO("not json")) == 0
    assert (home / ".cache" / "kampung" / "hooks.log").exists()


def test_unknown_event_is_ignored(home: Path) -> None:
    assert hook.main([], io.StringIO(json.dumps(event("PreToolUse")))) == 0


def test_stuck_handler_is_cut_off_at_the_deadline(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # DNS, ps and git calls are not bounded by HTTP timeouts; the whole run is.
    write_config(home, "http://127.0.0.1:9", "kmp_x")
    monkeypatch.setattr(hook, "HOOK_DEADLINE", 0.5)
    monkeypatch.setitem(hook.HANDLERS, "Stop", lambda event, clock: time.sleep(30))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    t = time.monotonic()
    assert hook.main([], io.StringIO(json.dumps(event("Stop")))) == 0
    assert time.monotonic() - t < 2
    assert out.getvalue() == ""
    assert "deadline" in (home / ".cache" / "kampung" / "hooks.log").read_text()


CHANNEL_PROMPT = '<channel source="kampung">\n1 Kampung message(s) waiting.\n</channel>'


def test_channel_prompt_does_not_reset_depth(
    home: Path, server: LiveServer, repo: Path
) -> None:
    # A turn started by a Channels push is not the user typing.
    start(home, repo)
    sid = int(str(hook.read_session_file("cc-1")["session"]))  # type: ignore[index]
    db_query(server, "UPDATE sessions SET max_depth_seen = 4 WHERE id = ?", sid)
    r = run_hook(home, event("UserPromptSubmit", prompt=CHANNEL_PROMPT))
    assert r.returncode == 0 and r.stdout == ""
    assert db_query(
        server, "SELECT max_depth_seen FROM sessions WHERE id = ?", sid
    ) == [(4,)]


NUDGE = {
    "hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext": REASON,
    }
}


def tool_used(name: str = "Bash") -> dict[str, object]:
    return event("PostToolUse", tool_name=name, tool_input={})


def test_post_tool_use_nudges_once_per_new_mail(
    home: Path, server: LiveServer, repo: Path
) -> None:
    start(home, repo)
    peer_send(server, "laptop/api", "while you work")
    first = run_hook(home, tool_used())
    assert json.loads(first.stdout) == NUDGE
    # Same mail, later tool call past the throttle window: no repeat.
    state = hook.cache_dir() / "nudge" / "cc-1.json"
    state.write_text(json.dumps({"checked": 0, "notified": 1}))
    assert run_hook(home, tool_used()).stdout == ""


def test_post_tool_use_is_throttled(home: Path, server: LiveServer, repo: Path) -> None:
    start(home, repo)
    assert run_hook(home, tool_used()).stdout == ""  # checked now, nothing waiting
    peer_send(server, "laptop/api", "arrives inside the window")
    assert run_hook(home, tool_used()).stdout == ""  # within 30 s: no HTTP, no nudge


def test_post_tool_use_skips_kampung_tools(
    home: Path, server: LiveServer, repo: Path
) -> None:
    start(home, repo)
    peer_send(server, "laptop/api", "hello")
    assert run_hook(home, tool_used("mcp__kampung__inbox")).stdout == ""
    assert (
        not (hook.cache_dir() / "nudge" / "cc-1.json").exists()
        or json.loads((hook.cache_dir() / "nudge" / "cc-1.json").read_text())[
            "notified"
        ]
        == 0
    )


def test_post_tool_use_fails_open(home: Path, repo: Path) -> None:
    write_config(home, "http://10.255.255.1:9", "kmp_x")
    hook.write_session_file("cc-1", {"session": "1", "ancestors": []})
    r = run_hook(home, tool_used())
    assert r.returncode == 0 and r.stdout == ""


def test_session_end_removes_nudge_state(
    home: Path, server: LiveServer, repo: Path
) -> None:
    start(home, repo)
    run_hook(home, tool_used())
    assert (hook.cache_dir() / "nudge" / "cc-1.json").exists()
    run_hook(home, event("SessionEnd", reason="exit"))
    assert not (hook.cache_dir() / "nudge" / "cc-1.json").exists()


def test_post_tool_use_throttles_even_when_server_is_down(home: Path) -> None:
    # Review fix: an unreachable server must not cost every tool call 2 s.
    write_config(home, "http://10.255.255.1:9", "kmp_x")
    hook.write_session_file("cc-1", {"session": "1", "ancestors": []})
    run_hook(home, tool_used())  # one slow attempt is allowed
    t = time.monotonic()
    r = run_hook(home, tool_used())
    assert r.returncode == 0 and r.stdout == ""
    assert time.monotonic() - t < 1.0


# Codex ------------------------------------------------------------------


def codex_start(
    home: Path, repo: Path, sid: str = "cx-1", *extra: str
) -> "subprocess.CompletedProcess[str]":
    return run_hook(
        home,
        {
            "hook_event_name": "SessionStart",
            "session_id": sid,
            "cwd": str(repo),
            "source": "startup",
        },
        "--agent",
        "codex",
        *extra,
    )


def codex(home: Path, ev: dict[str, object]) -> "subprocess.CompletedProcess[str]":
    return run_hook(home, ev, "--agent", "codex")


def test_codex_session_start_registers_codex_without_ancestry(
    home: Path, server: LiveServer, repo: Path
) -> None:
    r = codex_start(home, repo)
    assert r.returncode == 0
    f = hook.read_session_file("cx-1")
    assert f is not None
    assert f["agent"] == "codex" and f["hooks"] is True and f["ancestors"] == []
    assert db_query(server, "SELECT agent, hooks FROM sessions") == [("codex", 1)]
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "SessionStart"
    assert out["additionalContext"] == hook.CODEX_START_CONTEXT


def test_stray_shell_args_are_ignored(
    home: Path, server: LiveServer, repo: Path
) -> None:
    r = codex_start(home, repo, "cx-1", "||", "true")
    assert r.returncode == 0
    f = hook.read_session_file("cx-1")
    assert f is not None and f["agent"] == "codex"


def test_wake_prompt_does_not_reset_depth(
    home: Path, server: LiveServer, repo: Path
) -> None:
    # A turn started by `codex queue` is not the user typing.
    codex_start(home, repo)
    sid = int(str(hook.read_session_file("cx-1")["session"]))  # type: ignore[index]
    db_query(server, "UPDATE sessions SET max_depth_seen = 4 WHERE id = ?", sid)
    prompt = f"{hook.WAKE_PREFIX} {REASON}"
    r = codex(home, event("UserPromptSubmit", "cx-1", prompt=prompt))
    assert r.returncode == 0 and r.stdout == ""
    assert db_query(
        server, "SELECT max_depth_seen FROM sessions WHERE id = ?", sid
    ) == [(4,)]
    codex(home, event("UserPromptSubmit", "cx-1", prompt="typed"))
    assert db_query(
        server, "SELECT max_depth_seen FROM sessions WHERE id = ?", sid
    ) == [(-1,)]


def test_codex_stop_blocks_once(home: Path, server: LiveServer, repo: Path) -> None:
    codex_start(home, repo)
    peer_send(server, "laptop/api", "hello")
    r = codex(home, event("Stop", "cx-1", stop_hook_active=False))
    assert json.loads(r.stdout) == {"decision": "block", "reason": BLOCK_REASON}
    again = codex(home, event("Stop", "cx-1", stop_hook_active=True))
    assert again.returncode == 0 and again.stdout == ""


def test_codex_post_tool_use_nudges_and_skips_kampung_tools(
    home: Path, server: LiveServer, repo: Path
) -> None:
    codex_start(home, repo)
    peer_send(server, "laptop/api", "while you work")
    used = event("PostToolUse", "cx-1", tool_name="mcp__kampung__inbox")
    assert codex(home, used).stdout == ""
    state = hook.cache_dir() / "nudge" / "cx-1.json"
    state.write_text(json.dumps({"checked": 0, "notified": 0}))
    bash = event("PostToolUse", "cx-1", tool_name="Bash", tool_input={})
    assert json.loads(codex(home, bash).stdout) == NUDGE


def test_codex_session_end_is_cut_off_before_codex_cancels_it(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Codex cancels SessionEnd hooks at 3 s; anything unbounded (DNS, ps) must
    # still finish first, and the session file must still go.
    write_config(home, "http://127.0.0.1:9", "kmp_x")
    hook.write_session_file("cx-1", {"session": "1", "ancestors": []})
    monkeypatch.setattr(hook, "api", lambda *a, **k: time.sleep(30))
    t = time.monotonic()
    ev = json.dumps(event("SessionEnd", "cx-1", reason="other"))
    assert hook.main(["--agent", "codex"], io.StringIO(ev)) == 0
    assert time.monotonic() - t < 2.9
    assert hook.read_session_file("cx-1") is None


def test_codex_turn_is_busy_from_prompt_to_stop(
    home: Path, server: LiveServer, repo: Path
) -> None:
    # The waker must not queue a wake into a running turn: Codex would run it
    # after the turn, when the mail has already been read.
    codex_start(home, repo)
    assert not hook.turn_active("cx-1")
    codex(home, event("UserPromptSubmit", "cx-1", prompt="typed"))
    assert hook.turn_active("cx-1")
    codex(home, event("Stop", "cx-1", stop_hook_active=False))
    assert not hook.turn_active("cx-1")


def test_codex_turn_stays_busy_while_stop_blocks(
    home: Path, server: LiveServer, repo: Path
) -> None:
    codex_start(home, repo)
    codex(home, event("UserPromptSubmit", "cx-1", prompt=f"{hook.WAKE_PREFIX} x"))
    assert hook.turn_active("cx-1")  # a wake turn is a turn too
    peer_send(server, "laptop/api", "hello")
    codex(home, event("Stop", "cx-1", stop_hook_active=False))
    assert hook.turn_active("cx-1")  # blocked: the turn goes on
    codex(home, event("Stop", "cx-1", stop_hook_active=True))
    assert not hook.turn_active("cx-1")


def test_claude_code_turns_are_not_marked(
    home: Path, server: LiveServer, repo: Path
) -> None:
    start(home, repo)
    run_hook(home, event("UserPromptSubmit", "cc-1", prompt="typed"))
    assert not hook.turn_active("cc-1")


def test_codex_stop_cut_off_at_the_deadline_ends_the_turn(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A Stop that times out prints no block, so the turn is over; a mark left
    # behind would hold back idle wakes until it goes stale.
    write_config(home, "http://127.0.0.1:9", "kmp_x")
    hook.write_session_file("cx-1", {"session": "1", "ancestors": []})
    hook.mark_turn("cx-1")
    monkeypatch.setattr(hook, "api", lambda *a, **k: time.sleep(30))
    monkeypatch.setattr(hook, "HOOK_DEADLINE", 0.5)
    ev = json.dumps(event("Stop", "cx-1", stop_hook_active=False))
    assert hook.main(["--agent", "codex"], io.StringIO(ev)) == 0
    assert not hook.turn_active("cx-1")
