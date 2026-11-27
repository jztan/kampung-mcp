"""Session names: the hook reads the title the agent shows the person and
reports it, so peers can address the session as machine/repo@name."""

import json
from pathlib import Path

import pytest

from client_testlib import LiveServer, db_query, git_repo, run_hook
from kampung_client import hook


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return git_repo(tmp_path, "api")


def _line(kind: str, title: str) -> str:
    key = "customTitle" if kind == "custom-title" else "aiTitle"
    return json.dumps({"type": kind, key: title, "sessionId": "cc-1"})


def _transcript(path: Path, *lines: str, filler: int = 0) -> Path:
    body = [json.dumps({"type": "user", "message": "x" * 200})] * filler
    path.write_text("\n".join([*lines, *body]) + "\n")
    return path


# -- reading Claude Code's transcript


def test_claude_newest_auto_title(tmp_path: Path) -> None:
    t = _transcript(
        tmp_path / "t.jsonl",
        _line("ai-title", "Old title"),
        json.dumps({"type": "user"}),
        _line("ai-title", "Gateway work"),
    )
    assert hook.claude_title(str(t)) == ("auto", "Gateway work")


def test_claude_rename_beats_auto(tmp_path: Path) -> None:
    t = _transcript(
        tmp_path / "t.jsonl",
        _line("custom-title", "chess"),
        _line("ai-title", "Gateway work"),
    )
    assert hook.claude_title(str(t)) == ("rename", "chess")


def test_claude_only_the_tail_is_read(tmp_path: Path) -> None:
    t = _transcript(tmp_path / "t.jsonl", _line("ai-title", "Too old"), filler=400)
    assert t.stat().st_size > hook.TRANSCRIPT_TAIL
    assert hook.claude_title(str(t)) is None


@pytest.mark.parametrize(
    "content",
    ['{"type":"ai-title","aiTitle":', '{"type":"ai-title","aiTitle":7}', "\x00\xff"],
)
def test_claude_garbage_means_no_title(tmp_path: Path, content: str) -> None:
    t = tmp_path / "t.jsonl"
    t.write_text(content)
    assert hook.claude_title(str(t)) is None


def test_claude_missing_transcript(tmp_path: Path) -> None:
    assert hook.claude_title(str(tmp_path / "nope.jsonl")) is None
    assert hook.claude_title(None) is None


# -- reading Codex's session index


def _index(home: Path, *rows: tuple[str, str]) -> None:
    d = home / ".codex"
    d.mkdir(exist_ok=True)
    (d / "session_index.jsonl").write_text(
        "".join(
            json.dumps({"id": i, "thread_name": n, "updated_at": "2026-11-27"}) + "\n"
            for i, n in rows
        )
    )


def test_codex_newest_name_for_this_thread(home: Path) -> None:
    _index(home, ("cx-1", "First"), ("cx-2", "Other"), ("cx-1", "Gateway work"))
    assert hook.codex_title("cx-1") == ("auto", "Gateway work")
    assert hook.codex_title("cx-9") is None


def test_codex_home_is_honoured(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _index(home, ("cx-1", "Wrong"))  # the default folder, not used
    other = tmp_path / "codex-home"
    other.mkdir()
    (other / "session_index.jsonl").write_text(
        json.dumps({"id": "cx-1", "thread_name": "Right"}) + "\n"
    )
    monkeypatch.setenv("CODEX_HOME", str(other))
    assert hook.codex_title("cx-1") == ("auto", "Right")


def test_codex_no_index_means_no_title(home: Path) -> None:
    assert hook.codex_title("cx-1") is None


# -- KAMPUNG_NAME


def test_kampung_name_wins(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = _transcript(tmp_path / "t.jsonl", _line("custom-title", "chess"))
    ev = {"session_id": "cc-1", "transcript_path": str(t)}
    assert hook.session_title(ev) == ("rename", "chess")
    monkeypatch.setenv("KAMPUNG_NAME", "  screen-view ")
    assert hook.session_title(ev) == ("env", "screen-view")


# -- end to end through the hooks


def _name(server: LiveServer) -> list[tuple[object, ...]]:
    return db_query(server, "SELECT name, title_kind FROM sessions")


def test_session_start_and_stop_report_the_title(
    tmp_path: Path, home: Path, server: LiveServer, repo: Path
) -> None:
    t = _transcript(tmp_path / "t.jsonl", _line("ai-title", "Gateway work"))
    base = {"session_id": "cc-1", "cwd": str(repo), "transcript_path": str(t)}
    r = run_hook(home, {**base, "hook_event_name": "SessionStart"})
    assert r.returncode == 0, r.stderr
    assert _name(server) == [("gateway-work", "auto")]
    _transcript(t, _line("ai-title", "Gateway work"), _line("custom-title", "Api fix"))
    r = run_hook(home, {**base, "hook_event_name": "Stop"})
    assert r.returncode == 0, r.stderr
    assert _name(server) == [("api-fix", "rename")]
    data = hook.read_session_file("cc-1")
    assert data is not None and data["title_sent"] == ["rename", "Api fix"]


def test_codex_session_start_reports_its_thread_name(
    home: Path, server: LiveServer, repo: Path
) -> None:
    _index(home, ("cx-1", "Review the docs"))
    ev = {"hook_event_name": "SessionStart", "session_id": "cx-1", "cwd": str(repo)}
    r = run_hook(home, ev, "--agent", "codex")
    assert r.returncode == 0, r.stderr
    assert _name(server) == [("review-the-docs", "auto")]


def test_kampung_name_reaches_the_server(
    home: Path, server: LiveServer, repo: Path
) -> None:
    ev = {"hook_event_name": "SessionStart", "session_id": "cc-1", "cwd": str(repo)}
    r = run_hook(home, ev, env={"KAMPUNG_NAME": "screen-view"})
    assert r.returncode == 0, r.stderr
    assert _name(server) == [("screen-view", "env")]


def test_a_broken_transcript_never_breaks_the_hook(
    tmp_path: Path, home: Path, server: LiveServer, repo: Path
) -> None:
    t = tmp_path / "t.jsonl"
    t.write_bytes(b"\xff\xfe garbage")
    ev = {
        "hook_event_name": "SessionStart",
        "session_id": "cc-1",
        "cwd": str(repo),
        "transcript_path": str(t),
    }
    r = run_hook(home, ev)
    assert r.returncode == 0, r.stderr
    assert _name(server) == [(None, None)]
