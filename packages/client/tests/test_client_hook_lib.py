import json
import re
import shlex
import subprocess
import sys
import time
import os
from pathlib import Path
from typing import Any, Callable

import pytest

from client_testlib import (
    LiveServer,
    dead_pid,
    git_repo,
    nearest_kept,
    scripted_server,
    write_config,
)
from kampung_client import hook


def test_paths_follow_home(home: Path) -> None:
    assert hook.config_dir() == home / ".config" / "kampung"
    assert hook.cache_dir() == home / ".cache" / "kampung"
    assert hook.sessions_dir() == home / ".cache" / "kampung" / "sessions"


def test_ancestors_starts_at_parent_and_excludes_pid_1() -> None:
    chain = hook.ancestors()
    assert chain[0][0] == nearest_kept(os.getppid())
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
def test_repo_name_sanitizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, expected: str
) -> None:
    # git reports the top folder; some of these names (a trailing dot) cannot
    # exist on Windows, so the sanitising is tested on git's answer directly.
    def fake_git(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, f"/src/{name}\n", "")

    monkeypatch.setattr(hook.subprocess, "run", fake_git)
    assert hook.repo_name(str(tmp_path)) == expected


@pytest.fixture
def temp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # pytest's tmp_path is itself inside the system temp dir, so the real temp
    # roots would make every test folder scratch. Point them somewhere fixed.
    t = tmp_path / "systemp"
    t.mkdir()
    monkeypatch.setattr(hook, "_temp_roots", lambda: [str(t)])
    return t


def test_repo_name_in_a_temp_folder_is_the_shared_scratch_mailbox(
    temp_root: Path, home: Path
) -> None:
    for name in ("chess-eval-3f9a1c2b", "tmp", "work"):
        d = temp_root / "plain" / name
        d.mkdir(parents=True)
        assert hook.repo_name(str(d)) == "scratch"
    assert hook.repo_name(str(temp_root)) == "scratch"


def test_repo_name_temp_match_ignores_case_folding(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Windows normcase lowercases; a temp root given in mixed case must still
    # match a cwd under it.
    t = tmp_path / "SysTemp"
    (t / "Eval").mkdir(parents=True)
    monkeypatch.setattr(hook, "_temp_roots", lambda: [str(t)])
    monkeypatch.setattr(hook.os.path, "normcase", lambda p: p.lower())
    assert hook.repo_name(str(t / "Eval")) == "scratch"


def test_repo_name_in_the_home_folder_is_the_shared_scratch_mailbox(
    temp_root: Path, home: Path
) -> None:
    assert hook.repo_name(str(home)) == "scratch"


def test_repo_name_outside_git_elsewhere_is_the_folder_name(
    tmp_path: Path, temp_root: Path, home: Path
) -> None:
    # A real project before `git init`, e.g. ~/src/reviewer, keeps its name.
    for parent in (tmp_path / "src", home / "src"):
        d = parent / "reviewer"
        d.mkdir(parents=True)
        assert hook.repo_name(str(d)) == "reviewer"
    odd = tmp_path / "src" / "my notes"
    odd.mkdir()
    assert hook.repo_name(str(odd)) == "my-notes"


def test_temp_roots_include_the_system_temp_dir() -> None:
    import tempfile

    roots = hook._temp_roots()
    assert os.path.normcase(os.path.realpath(tempfile.gettempdir())) in roots


def test_repo_name_uses_git_toplevel(tmp_path: Path) -> None:
    repo = git_repo(tmp_path, "api")
    (repo / "sub").mkdir()
    assert hook.repo_name(str(repo / "sub")) == "api"


def test_repo_name_gives_git_no_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The MCP server reads the adapter's stdin. On Windows a child that inherits
    # it hangs at startup, and the timeout never returns, so Codex's first tool
    # call hung for good.
    real = subprocess.run
    seen: dict[str, object] = {}

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(hook.subprocess, "run", spy)
    hook.repo_name(str(tmp_path))
    assert seen["stdin"] == subprocess.DEVNULL


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
    hook.write_session_file("dead", {"ancestors": [[dead_pid(), "x"]]})
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


def test_api_server_error_raises_hookerror(home: Path) -> None:
    httpd, _ = scripted_server(503)
    write_config(home, f"http://127.0.0.1:{httpd.server_address[1]}", "kmp_x")
    try:
        with pytest.raises(hook.HookError, match="HTTP 503"):
            hook.api("GET", "/v1/sessions/1/peek")
    finally:
        httpd.shutdown()


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
    assert data["ancestors"][0][0] == nearest_kept(os.getppid())
    assert isinstance(data["written_at"], float)


def test_register_with_awkward_dir_name(server: LiveServer, tmp_path: Path) -> None:
    d = git_repo(tmp_path / "plain", ".My Repo!")
    assert hook.register("codex", str(d), False)["mailbox"] == "laptop/My-Repo-"


@pytest.mark.skipif(
    sys.platform == "win32", reason="ps only; Windows uses GetProcessTimes"
)
def test_ancestor_start_times_ignore_locale(monkeypatch: pytest.MonkeyPatch) -> None:
    # Hooks and the adapter can run with different locales; start times must
    # still compare equal, so `ps` always runs in the C locale.
    pattern = r"^[A-Z][a-z]{2} [A-Z][a-z]{2} \d{1,2} \d\d:\d\d:\d\d \d{4}$"
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
        "cc-dead", {"agent": "claude-code", "ancestors": [[dead_pid(), "x"]]}
    )
    assert hook.sweep_stale_files() == 2
    assert hook.read_session_file("cx-new") is not None
    assert hook.read_session_file("cx-old") is None


def test_stale_turn_mark_counts_as_idle(home: Path) -> None:
    # An interrupted turn may never fire Stop; its mark must not stop wakes
    # for good.
    hook.mark_turn("cx-1")
    assert hook.turn_active("cx-1")
    old = time.time() - hook.TURN_STALE - 1
    os.utime(hook.cache_dir() / "turn" / "cx-1", (old, old))
    assert not hook.turn_active("cx-1")
    hook.end_turn("cx-1")
    hook.end_turn("cx-1")  # already gone: no error


def test_sweep_keeps_file_when_lookup_fails(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hook.write_session_file("busy", {"ancestors": [[12345, "t"]]})

    def fail(pid: int) -> None:
        raise hook.LookupFailed(pid)

    monkeypatch.setattr(hook, "_lookup", fail)
    assert hook.sweep_stale_files() == 0
    assert hook.read_session_file("busy") is not None


def test_ps_timeout_is_a_failed_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    def timeout(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired("ps", 2)

    monkeypatch.setattr(hook.subprocess, "run", timeout)
    with pytest.raises(hook.LookupFailed):
        hook._ps_lookup(12345)
    assert hook._process(12345) is None
    assert hook.ancestors(12345) == []


@pytest.mark.parametrize(
    ("returncode", "stderr", "fails"),
    [
        (1, "", False),  # no such process, on macOS and procps alike
        (-9, "", True),  # ps killed
        (1, "ps: Operation not permitted\n", True),  # sandbox denied it
    ],
)
def test_ps_failure_is_not_a_dead_process(
    monkeypatch: pytest.MonkeyPatch, returncode: int, stderr: str, fails: bool
) -> None:
    def run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, returncode, "", stderr)

    monkeypatch.setattr(hook.subprocess, "run", run)
    if fails:
        with pytest.raises(hook.LookupFailed):
            hook._ps_lookup(12345)
    else:
        assert hook._ps_lookup(12345) is None


def test_log_rotates_past_its_size_cap(home: Path) -> None:
    log = home / ".cache" / "kampung" / "hooks.log"
    log.parent.mkdir(parents=True)
    log.write_text("x" * hook.LOG_MAX_BYTES)
    hook._log("Stop", ValueError("newest"))
    assert log.read_text().endswith("Stop: ValueError: newest\n")
    assert log.stat().st_size < 1024
    assert log.with_name("hooks.log.1").stat().st_size == hook.LOG_MAX_BYTES


def test_log_below_its_cap_is_not_rotated(home: Path) -> None:
    hook._log("Stop", ValueError("first"))
    hook._log("Stop", ValueError("second"))
    log = home / ".cache" / "kampung" / "hooks.log"
    assert len(log.read_text().splitlines()) == 2
    assert not log.with_name("hooks.log.1").exists()


def test_log_is_written_when_rotation_fails(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(*args: object) -> None:
        raise PermissionError("in use")  # Windows, another hook has it open

    log = home / ".cache" / "kampung" / "hooks.log"
    log.parent.mkdir(parents=True)
    log.write_text("x" * hook.LOG_MAX_BYTES)
    monkeypatch.setattr(hook.os, "replace", denied)
    hook._log("Stop", ValueError("kept"))
    assert log.read_text().endswith("kept\n")


@pytest.mark.parametrize(
    "body, seen, expected",
    [
        ({"wakeable": 1, "newest": "msg_b"}, None, (True, "msg_b")),
        ({"wakeable": 1, "newest": "msg_b"}, "msg_b", (False, "msg_b")),
        # Same count, different mail: one message replaced another.
        ({"wakeable": 1, "newest": "msg_b"}, "msg_a", (True, "msg_b")),
        ({"wakeable": 0, "newest": None}, "msg_a", (False, 0)),
        # Older servers send only the count.
        ({"wakeable": 1}, None, (True, 1)),
        ({"wakeable": 1}, 1, (False, 1)),
        ({"wakeable": 2}, 1, (True, 2)),
        ({"wakeable": 1}, "msg_a", (True, 1)),  # server went back to an older one
        ({}, 3, (False, 0)),
    ],
)
def test_new_mail(
    body: dict[str, object], seen: hook.Seen, expected: tuple[bool, hook.Seen]
) -> None:
    assert hook.new_mail(body, seen) == expected


def test_a_registered_folder_keeps_its_mailbox_after_a_rule_change(
    server: LiveServer,
    tmp_path: Path,
    temp_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    d = tmp_path / "src" / "reviewer"
    d.mkdir(parents=True)
    assert hook.register("codex", str(d), True, "cx-1")["mailbox"] == "laptop/reviewer"
    # A later client sends every non-git folder to scratch: this one stays.
    monkeypatch.setattr(hook, "_is_scratch", lambda cwd: True)
    assert hook.repo_name(str(d)) == "reviewer"
    assert hook.register("codex", str(d), True, "cx-2")["mailbox"] == "laptop/reviewer"
    fresh = tmp_path / "src" / "other"
    fresh.mkdir()
    assert hook.repo_name(str(fresh)) == "scratch"


def test_the_pin_is_keyed_by_the_git_toplevel(
    server: LiveServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = git_repo(tmp_path, "api")
    (repo / "sub").mkdir()
    hook.register("claude-code", str(repo / "sub"), True, "cc-1")
    monkeypatch.setattr(hook, "_NAME_BAD", re.compile(r"[a-z]"))  # would mangle it
    assert hook.repo_name(str(repo)) == "api"


def test_scratch_folders_are_not_pinned(server: LiveServer, home: Path) -> None:
    assert hook.register("claude-code", str(home), True, "cc-1")["mailbox"] == (
        "laptop/scratch"
    )
    assert not (home / ".cache" / "kampung" / "mailboxes.json").exists()


def test_a_damaged_pin_file_is_ignored(
    home: Path, tmp_path: Path, temp_root: Path
) -> None:
    pins = home / ".cache" / "kampung" / "mailboxes.json"
    pins.parent.mkdir(parents=True, exist_ok=True)
    pins.write_text("{not json")
    d = tmp_path / "src" / "reviewer"
    d.mkdir(parents=True)
    assert hook.repo_name(str(d)) == "reviewer"


def test_register_asks_git_once(
    server: LiveServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Hooks have 2 seconds; git is slow to start on Windows, so naming and
    # pinning the mailbox share one look at the worktree.
    repo = git_repo(tmp_path, "api")
    real = hook.subprocess.run
    seen: list[list[str]] = []

    def counting(cmd: list[str], *a: Any, **kw: Any) -> Any:
        seen.append(cmd)
        return real(cmd, *a, **kw)

    monkeypatch.setattr(hook.subprocess, "run", counting)
    assert hook.register("claude-code", str(repo), True, "cc-1")["mailbox"] == (
        "laptop/api"
    )
    assert sum("rev-parse" in cmd for cmd in seen) == 1


# Idle wake files ---------------------------------------------------------


def test_wake_paths_stay_inside_the_wake_dir(home: Path) -> None:
    p = hook.wake_path("../../etc/x y")
    assert p.parent == hook.wake_dir() and p.name == ".._.._etc_x_y"
    assert hook.ack_path("cc-1") == hook.wake_dir() / "cc-1.ack"


@pytest.mark.parametrize("sid", ["", ".", "..", "..."])
def test_a_wake_path_is_never_the_folder_itself(home: Path, sid: str) -> None:
    p = hook.wake_path(sid)
    assert p.parent == hook.wake_dir() and p.name.strip(".")


def test_write_wake_token_never_creates_the_file(home: Path) -> None:
    assert hook.write_wake_token("cc-1") is None and not hook.wake_dir().exists()
    hook.ensure_wake_file("cc-1")
    first, second = hook.write_wake_token("cc-1"), hook.write_wake_token("cc-1")
    assert first and second and first != second
    assert hook.read_token(hook.wake_path("cc-1")) == second
    assert [p.name for p in hook.wake_dir().iterdir()] == ["cc-1"]  # no temp left


def test_ensure_wake_file_keeps_a_token(home: Path) -> None:
    hook.ensure_wake_file("cc-1")
    token = hook.write_wake_token("cc-1")
    hook.ensure_wake_file("cc-1")
    assert hook.read_token(hook.wake_path("cc-1")) == token


def test_read_token_of_a_missing_file_is_empty(home: Path) -> None:
    assert hook.read_token(hook.wake_path("none")) == ""


def test_idle_wake_is_on_only_for_claude_code_with_a_wake_file(home: Path) -> None:
    assert hook.idle_wake_on("claude-code", "cc-1") is False
    hook.ensure_wake_file("cc-1")
    assert hook.idle_wake_on("claude-code", "cc-1") is True
    assert hook.idle_wake_on("codex", "cc-1") is False
    assert hook.idle_wake_on("claude-code", None) is False


def test_sweep_keeps_a_fresh_wake_file_without_a_session_file(home: Path) -> None:
    fresh = hook.ensure_wake_file("starting")
    old = hook.ensure_wake_file("gone")
    hook.ack_path("gone").write_text("t")
    past = time.time() - hook.WAKE_ORPHAN_AGE - 60
    os.utime(old, (past, past))
    os.utime(hook.ack_path("gone"), (past, past))
    hook.sweep_stale_files()
    assert fresh.exists()
    assert not old.exists() and not hook.ack_path("gone").exists()


def test_sweep_removes_wake_files_with_a_dead_session_file(home: Path) -> None:
    hook.write_session_file(
        "dead", {"agent": "claude-code", "ancestors": [[dead_pid(), "x"]]}
    )
    hook.ensure_wake_file("dead")
    hook.ack_path("dead").write_text("t")
    assert hook.sweep_stale_files() == 1
    assert not hook.wake_path("dead").exists() and not hook.ack_path("dead").exists()


def test_sweep_keeps_the_wake_file_of_a_live_session(home: Path) -> None:
    live = [list(a) for a in hook.ancestors()]
    hook.write_session_file("live", {"agent": "claude-code", "ancestors": live})
    wake = hook.ensure_wake_file("live")
    past = time.time() - hook.WAKE_ORPHAN_AGE - 60
    os.utime(wake, (past, past))
    hook.sweep_stale_files()
    assert wake.exists()


# Session labels ------------------------------------------------------------


def _git_dir(repo: Path, head: str) -> Path:
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text(head)
    return repo


def test_git_branch_plain_and_nested(tmp_path: Path) -> None:
    repo = _git_dir(tmp_path / "api", "ref: refs/heads/develop\n")
    nested = repo / "a" / "b"
    nested.mkdir(parents=True)
    assert hook.git_branch(str(repo)) == "develop"
    assert hook.git_branch(str(nested)) == "develop"


def test_git_branch_worktree_relative_gitdir(tmp_path: Path) -> None:
    main = _git_dir(tmp_path / "main", "ref: refs/heads/develop\n")
    wt_meta = main / ".git" / "worktrees" / "wt"
    wt_meta.mkdir(parents=True)
    (wt_meta / "HEAD").write_text("ref: refs/heads/feature/x\n")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: ../main/.git/worktrees/wt\n")
    assert hook.git_branch(str(wt)) == "feature/x"


def test_git_branch_worktree_absolute_gitdir(tmp_path: Path) -> None:
    meta = tmp_path / "elsewhere"
    meta.mkdir()
    (meta / "HEAD").write_text("ref: refs/heads/fix/y\n")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {meta}\n")
    assert hook.git_branch(str(wt)) == "fix/y"


def test_git_branch_detached(tmp_path: Path) -> None:
    repo = _git_dir(tmp_path / "api", "0123456789abcdef0123456789abcdef01234567\n")
    assert hook.git_branch(str(repo)) == "detached 0123456"


def test_git_branch_crlf(tmp_path: Path) -> None:
    repo = _git_dir(tmp_path / "api", "ref: refs/heads/main\r\n")
    assert hook.git_branch(str(repo)) == "main"


def test_git_branch_other_ref(tmp_path: Path) -> None:
    repo = _git_dir(tmp_path / "api", "ref: refs/remotes/o/x\n")
    assert hook.git_branch(str(repo)) == "remotes/o/x"


@pytest.mark.parametrize("head", ["garbage\n", "ref: refs/heads/a\x07b\n", ""])
def test_git_branch_none_for_bad_heads(tmp_path: Path, head: str) -> None:
    assert hook.git_branch(str(_git_dir(tmp_path / "api", head))) is None


def test_git_branch_none_outside_git(tmp_path: Path) -> None:
    d = tmp_path / "plain"
    d.mkdir()
    assert hook.git_branch(str(d)) is None


def test_git_branch_none_when_head_unreadable(tmp_path: Path) -> None:
    repo = tmp_path / "api"
    (repo / ".git" / "HEAD").mkdir(parents=True)  # a folder, so reading fails
    assert hook.git_branch(str(repo)) is None


def test_git_branch_cuts_long_names(tmp_path: Path) -> None:
    repo = _git_dir(tmp_path / "api", "ref: refs/heads/" + "x" * 150 + "\n")
    assert hook.git_branch(str(repo)) == "x" * 100


def test_client_version_matches_package() -> None:
    import kampung_client

    assert hook.client_version() == kampung_client.__version__


def test_client_version_none_when_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(name: str) -> str:
        raise LookupError(name)

    monkeypatch.setattr(hook.importlib.metadata, "version", boom)
    assert hook.client_version() is None


def test_register_sends_branch_and_version(server: LiveServer, tmp_path: Path) -> None:
    import kampung_client
    from client_testlib import db_query

    repo = git_repo(tmp_path, "api")
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-q", "-b", "feature/x"], check=True
    )
    data = hook.register("claude-code", str(repo), True, "cc-1")
    assert data["branch_sent"] == "feature/x"
    rows = db_query(server, "SELECT branch, client_version FROM sessions")
    assert rows == [("feature/x", kampung_client.__version__)]


def test_register_omits_version_when_unknown(
    server: LiveServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from client_testlib import db_query

    monkeypatch.setattr(hook, "client_version", lambda: None)
    plain = tmp_path / "plain"
    plain.mkdir()
    data = hook.register("claude-code", str(plain), True, "cc-1")
    assert data["branch_sent"] == ""
    assert db_query(server, "SELECT client_version FROM sessions") == [(None,)]
