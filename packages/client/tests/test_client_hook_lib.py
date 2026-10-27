import json
import re
import os
from pathlib import Path

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
