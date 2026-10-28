import json
import re
import shlex
import subprocess
import sys
import time
import os
from pathlib import Path
from typing import Callable

import pytest

from client_testlib import LiveServer, git_repo, write_config
from kampung_client import hook


def test_paths_follow_home(home: Path) -> None:
    assert hook.config_dir() == home / ".config" / "kampung"
    assert hook.cache_dir() == home / ".cache" / "kampung"
    assert hook.sessions_dir() == home / ".cache" / "kampung" / "sessions"


def test_ancestors_starts_at_parent_and_excludes_pid_1() -> None:
    chain = hook.ancestors()
    assert chain[0][0] == os.getppid()
    assert all(pid != 1 for pid, _ in chain)
    assert all(
        isinstance(start, str) and start == start.strip() and start
        for _, start in chain
    )


@pytest.mark.parametrize(
    "name,expected",
    [
        ("api", "api"),
        ("my repo", "my-repo"),
        (".hidden", "hidden"),
        ("a" * 70, "a" * 64),
        ("...", "root"),
    ],
)
def test_repo_name_sanitizes(tmp_path: Path, name: str, expected: str) -> None:
    d = tmp_path / "plain" / name
    d.mkdir(parents=True)
    assert hook.repo_name(str(d)) == expected


def test_repo_name_uses_git_toplevel(tmp_path: Path) -> None:
    repo = git_repo(tmp_path, "api")
    (repo / "sub").mkdir()
    assert hook.repo_name(str(repo / "sub")) == "api"


def test_session_file_round_trip_and_delete(home: Path) -> None:
    data = {
        "session": "1",
        "suffix": "ab",
        "mailbox": "laptop/api",
        "cwd": "/x",
        "written_at": 1.0,
        "hooks": True,
        "ancestors": [[5, "s"]],
    }
    path = hook.write_session_file("cc-1", data)
    assert path == hook.sessions_dir() / "cc-1.json"
    assert hook.read_session_file("cc-1") == data
    assert hook.list_session_files() == [data]
    hook.delete_session_file("cc-1")
    assert hook.read_session_file("cc-1") is None
    hook.delete_session_file("cc-1")


def test_list_session_files_skips_garbage(home: Path) -> None:
    hook.sessions_dir().mkdir(parents=True)
    (hook.sessions_dir() / "bad.json").write_text("{")
    assert hook.list_session_files() == []


def test_sweep_removes_dead_ancestor_files(home: Path) -> None:
    live = [list(a) for a in hook.ancestors()]
    hook.write_session_file("live", {"ancestors": live})
    hook.write_session_file("dead", {"ancestors": [[999999, "x"]]})
    hook.write_session_file("reused", {"ancestors": [[live[0][0], "not-its-start"]]})
    assert hook.sweep_stale_files() == 2
    assert hook.read_session_file("live") is not None
    assert hook.read_session_file("dead") is None


def test_api_returns_4xx_body(server: LiveServer) -> None:
    status, body = hook.api("GET", "/v1/sessions/0/peek")
    assert status == 404 and body["error"] == "unknown_session"


def test_api_network_error_raises_hookerror(home: Path) -> None:
    write_config(home, "http://127.0.0.1:9", "kmp_x")
    with pytest.raises(hook.HookError):
        hook.api("GET", "/health")


def test_load_config_missing_raises(home: Path) -> None:
    with pytest.raises(hook.HookError):
        hook.load_config()
    d = home / ".config" / "kampung"
    d.mkdir(parents=True)
    (d / "config.json").write_text(json.dumps({"server": "http://x"}))
    with pytest.raises(hook.HookError):
        hook.load_config()


def test_register_returns_session_file_dict(server: LiveServer, tmp_path: Path) -> None:
    repo = git_repo(tmp_path, "api")
    data = hook.register("claude-code", str(repo), True, "cc-1")
    assert data["mailbox"] == "laptop/api" and data["hooks"] is True
    assert data["cwd"] == str(repo) and isinstance(data["session"], str)
    assert data["ancestors"][0][0] == os.getppid()
    assert isinstance(data["written_at"], float)


def test_register_with_awkward_dir_name(server: LiveServer, tmp_path: Path) -> None:
    d = tmp_path / "plain" / ".My Repo!"
    d.mkdir(parents=True)
    assert hook.register("codex", str(d), False)["mailbox"] == "laptop/My-Repo-"


def test_ancestor_start_times_ignore_locale(monkeypatch: pytest.MonkeyPatch) -> None:
    # Hooks and the adapter can run with different locales; start times must
    # still compare equal, so `ps` always runs in the C locale.
    pattern = r"^[A-Z][a-z]{2} [A-Z][a-z]{2} [ \d]\d \d\d:\d\d:\d\d \d{4}$"
    monkeypatch.setenv("LANG", "en_SG.UTF-8")
    monkeypatch.setenv("LC_ALL", "en_SG.UTF-8")
    with_locale = hook.ancestors()
    monkeypatch.delenv("LANG")
    monkeypatch.delenv("LC_ALL")
    assert hook.ancestors() == with_locale
    assert all(re.match(pattern, start) for _, start in with_locale)


def _table(
    rows: dict[int, hook.ProcessInfo],
) -> Callable[[int], hook.ProcessInfo | None]:
    return rows.get


def test_walk_skips_shell_wrappers() -> None:
    # Windows: Claude Code runs hooks through Git Bash, so a hook's parent is a
    # short-lived bash.exe; the chain must start at claude (node) instead.
    rows = {
        300: (200, "t300", "python.exe"),  # the hook
        200: (100, "t200", "bash.exe"),
        100: (50, "t100", "node.exe"),  # claude
        50: (10, "t50", "pwsh.exe"),
        10: (4, "t10", "WindowsTerminal.exe"),
        4: (0, "t4", "System"),
    }
    assert hook._walk(300, _table(rows)) == [(100, "t100"), (10, "t10"), (4, "t4")]


def test_walk_skips_posix_shells() -> None:
    # macOS and Linux: Claude Code runs `<python> hook.py || true` through
    # /bin/sh, which stays alive as the hook's parent until the hook exits.
    rows = {
        300: (200, "t300", "python3.12"),  # the hook
        200: (100, "t200", "sh"),
        100: (50, "t100", "claude"),
        50: (10, "t50", "zsh"),
        10: (4, "t10", "tmux"),
        4: (0, "t4", "launchd"),
    }
    assert hook._walk(300, _table(rows)) == [(100, "t100"), (10, "t10"), (4, "t4")]


@pytest.mark.skipif(os.name == "nt", reason="POSIX shells and ps")
def test_ancestors_skip_the_shell_running_the_hook() -> None:
    probe = "from kampung_client import hook; print(hook.ancestors()[0][0])"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(probe)} || true"
    out = subprocess.run(
        ["/bin/sh", "-c", command], capture_output=True, text=True, timeout=10
    )
    assert int(out.stdout) == os.getpid()


def test_walk_stops_on_a_pid_loop() -> None:
    # Windows reuses pids, so a parent pid can point back down the chain.
    rows = {
        30: (20, "t30", "python.exe"),
        20: (10, "t20", "node.exe"),
        10: (20, "t10", "explorer.exe"),
    }
    assert hook._walk(30, _table(rows)) == [(20, "t20"), (10, "t10")]


def test_walk_stops_at_unknown_parent() -> None:
    rows = {30: (20, "t30", "python.exe")}
    assert hook._walk(30, _table(rows)) == []


def test_walk_skips_the_venv_launcher_at_the_bottom() -> None:
    # Windows: a venv's Scripts\python.exe is a launcher that starts the real
    # interpreter as its child, so every chain begins with a launcher.
    rows = {
        2816: (1412, "t2816", "python.exe"),  # the adapter
        1412: (9364, "t1412", "python.exe"),  # its launcher
        9364: (8884, "t9364", "claude.exe"),
        8884: (4, "t8884", "WindowsTerminal.exe"),
    }
    assert hook._walk(2816, _table(rows)) == [(9364, "t9364"), (8884, "t8884")]


def test_walk_keeps_python_above_the_first_kept_ancestor() -> None:
    rows = {
        30: (20, "t30", "python.exe"),
        20: (10, "t20", "claude.exe"),
        10: (4, "t10", "pythonw.exe"),  # a Python program further up
    }
    assert hook._walk(30, _table(rows)) == [(20, "t20"), (10, "t10")]


def test_sweep_keeps_codex_files_and_ages_them_out(home: Path) -> None:
    now = time.time()
    hook.write_session_file(
        "cx-new", {"agent": "codex", "ancestors": [], "written_at": now}
    )
    hook.write_session_file(
        "cx-old", {"agent": "codex", "ancestors": [], "written_at": now - 8 * 86400}
    )
    hook.write_session_file(
        "cc-dead", {"agent": "claude-code", "ancestors": [[999999, "x"]]}
    )
    assert hook.sweep_stale_files() == 2
    assert hook.read_session_file("cx-new") is not None
    assert hook.read_session_file("cx-old") is None
