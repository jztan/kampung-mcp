import json
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from client_testlib import LiveServer
from kampung_client import cli, hook
from kampung_client import setup
from kampung_client.setup import SetupError, manual_command, run_setup

EVENTS = ["SessionStart", "UserPromptSubmit", "Stop", "SessionEnd", "PostToolUse"]


class Recorder:
    def __init__(self, fail: bool = False, missing: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.fail, self.missing = fail, missing

    def __call__(self, cmd: list[str]) -> int:
        self.calls.append(cmd)
        if self.missing:
            raise FileNotFoundError(cmd[0])
        return 1 if self.fail else 0


def adapter_cmd(agent: str) -> list[str]:
    return [sys.executable, "-m", "kampung_client", "adapter", "--agent", agent]


def settings(home: Path) -> dict:  # type: ignore[type-arg]
    return json.loads((home / ".claude" / "settings.json").read_text())


def test_setup_writes_private_config_and_hook(home: Path, server: LiveServer) -> None:
    (home / ".config").mkdir(exist_ok=True)
    run_setup(server.url + "/", server.keys["laptop"], ["claude-code"], Recorder())
    cfg = home / ".config" / "kampung" / "config.json"
    assert json.loads(cfg.read_text()) == {
        "server": server.url,
        "key": server.keys["laptop"],
    }
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
    # The hook runs from the install, so it always matches the adapter's version.
    assert not (home / ".config" / "kampung" / "kampung_hook.py").exists()
    cmd = settings(home)["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert shlex.split(cmd)[:2] == [sys.executable, hook.__file__]


def test_setup_rejects_bad_key(home: Path, server: LiveServer) -> None:
    with pytest.raises(SetupError, match="rejected this key"):
        run_setup(server.url, "kmp_wrong", ["claude-code"], Recorder())
    cfg = json.loads((home / ".config" / "kampung" / "config.json").read_text())
    assert cfg["key"] == server.keys["laptop"]  # the fixture's config survives
    assert not (home / ".claude" / "settings.json").exists()


def test_setup_rejects_unreachable_server(home: Path) -> None:
    with pytest.raises(SetupError):
        run_setup("http://127.0.0.1:9", "kmp_x", ["claude-code"], Recorder())


def test_setup_merges_claude_hooks_idempotently(home: Path, server: LiveServer) -> None:
    existing = {
        "model": "x",
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other.sh"}]}]},
    }
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text(json.dumps(existing))
    for _ in range(2):
        run_setup(server.url, server.keys["laptop"], ["claude-code"], Recorder())
    s = settings(home)
    assert s["model"] == "x"
    command = f"{shlex.quote(sys.executable)} {shlex.quote(hook.__file__)} || true"
    for event in EVENTS:
        ours = [e for e in s["hooks"][event] if "other.sh" not in str(e)]
        assert ours == [
            {"hooks": [{"type": "command", "command": command, "timeout": 5}]}
        ]
    assert len(s["hooks"]["Stop"]) == 2
    backup = home / ".claude" / "settings.json.kampung-backup"
    assert json.loads(backup.read_text()) == existing


def test_setup_quotes_paths_with_spaces(
    tmp_path: Path, server: LiveServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    spaced = tmp_path / "my home"
    spaced.mkdir()
    monkeypatch.setenv("HOME", str(spaced))
    installed = str(spaced / "site-packages" / "kampung_client" / "hook.py")
    monkeypatch.setattr(hook, "__file__", installed)
    run_setup(server.url, server.keys["laptop"], ["claude-code"], Recorder())
    cmd = settings(spaced)["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert shlex.split(cmd)[1] == installed


def test_setup_replaces_hooks_from_the_old_copied_script(
    home: Path, server: LiveServer
) -> None:
    old_copy = home / ".config" / "kampung" / "kampung_hook.py"
    old_copy.parent.mkdir(parents=True, exist_ok=True)
    old_copy.write_text("# old")
    old = f"{shlex.quote(sys.executable)} {shlex.quote(str(old_copy))}"
    entry = {"hooks": [{"type": "command", "command": old, "timeout": 5}]}
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"hooks": {event: [entry] for event in EVENTS}})
    )
    run_setup(server.url, server.keys["laptop"], ["claude-code"], Recorder())
    for event in EVENTS:
        [only] = settings(home)["hooks"][event]
        assert "kampung_hook.py" not in only["hooks"][0]["command"]
    assert not old_copy.exists()


def test_hook_command_exits_0_when_the_hook_file_is_gone(tmp_path: Path) -> None:
    # Python exits 2 for a missing script, and Claude Code reads exit 2 from a
    # hook as "block" (a UserPromptSubmit hook would block every prompt).
    cmd = setup.hook_command(str(tmp_path / "gone" / "hook.py"))
    assert subprocess.run(cmd, shell=True, capture_output=True).returncode == 0


def test_setup_registers_mcp_with_claude(home: Path, server: LiveServer) -> None:
    rec = Recorder()
    run_setup(server.url, server.keys["laptop"], ["claude-code"], rec)
    assert rec.calls == [
        ["claude", "mcp", "remove", "--scope", "user", "kampung"],
        [
            "claude",
            "mcp",
            "add",
            "--scope",
            "user",
            "kampung",
            "--",
            *adapter_cmd("claude-code"),
        ],
    ]


def test_setup_codex_registers_without_hooks(home: Path, server: LiveServer) -> None:
    rec = Recorder()
    run_setup(server.url, server.keys["laptop"], ["codex"], rec)
    assert not (home / ".claude" / "settings.json").exists()
    assert rec.calls == [
        ["codex", "mcp", "remove", "kampung"],
        ["codex", "mcp", "add", "kampung", "--", *adapter_cmd("codex")],
    ]


def test_setup_missing_cli_prints_manual_command(
    home: Path, server: LiveServer
) -> None:
    lines = run_setup(
        server.url, server.keys["laptop"], ["claude-code"], Recorder(missing=True)
    )
    assert any("claude mcp add --scope user kampung --" in line for line in lines)


def test_setup_failed_cli_prints_manual_command(home: Path, server: LiveServer) -> None:
    lines = run_setup(server.url, server.keys["laptop"], ["codex"], Recorder(fail=True))
    assert any("codex mcp add kampung --" in line for line in lines)


def test_default_runner_runs_the_resolved_program(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # On Windows an npm install of Claude Code is claude.cmd, which
    # CreateProcess only finds through PATHEXT, i.e. through shutil.which.
    cmd_path = r"C:\nvm4w\nodejs\claude.CMD"
    ran: list[list[str]] = []
    monkeypatch.setattr(setup.shutil, "which", lambda name: cmd_path)

    def run(cmd: list[str]) -> subprocess.CompletedProcess[bytes]:
        ran.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(setup.subprocess, "run", run)
    assert setup.default_runner(["claude", "mcp", "list"]) == 0
    assert ran == [[cmd_path, "mcp", "list"]]


def test_default_runner_missing_program_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(setup.shutil, "which", lambda name: None)
    with pytest.raises(FileNotFoundError):
        setup.default_runner(["claude", "mcp", "list"])


def test_manual_command_posix_uses_shell_quoting() -> None:
    cmd = ["claude", "mcp", "add", "kampung", "--", "/my venv/python", "-m", "x"]
    assert manual_command(cmd, windows=False) == (
        "claude mcp add kampung -- '/my venv/python' -m x"
    )


def test_manual_command_windows_quotes_for_powershell() -> None:
    cmd = [
        "claude",
        "mcp",
        "add",
        "--scope",
        "user",
        "kampung",
        "--",
        r"C:\src\kampung-mcp\.venv\Scripts\python.exe",
        "-m",
        "kampung_client",
        "adapter",
        "--agent",
        "claude-code",
    ]
    assert manual_command(cmd, windows=True) == (
        "claude mcp add --scope user kampung '--' "
        r"'C:\src\kampung-mcp\.venv\Scripts\python.exe' "
        "-m kampung_client adapter --agent claude-code"
    )


def test_manual_command_windows_escapes_single_quotes() -> None:
    assert manual_command([r"C:\it's\python.exe"], windows=True) == (
        r"'C:\it''s\python.exe'"
    )


def test_setup_sweeps_stale_files(home: Path, server: LiveServer) -> None:
    hook.write_session_file("dead", {"ancestors": [[999999, "x"]]})
    run_setup(server.url, server.keys["laptop"], ["codex"], Recorder())
    assert hook.read_session_file("dead") is None


def test_cli_setup_exit_codes(
    home: Path,
    server: LiveServer,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("kampung_client.setup.default_runner", Recorder())
    assert (
        cli.main(
            [
                "setup",
                "--server",
                server.url,
                "--key",
                server.keys["laptop"],
                "--agent",
                "both",
            ]
        )
        == 0
    )
    assert "Claude Code" in capsys.readouterr().out
    assert cli.main(["setup", "--server", server.url, "--key", "kmp_wrong"]) == 1
    assert capsys.readouterr().err.startswith("error: ")


@pytest.mark.parametrize(
    "content",
    ["{not json", "[]", '{"hooks": []}', '{"hooks": {"Stop": {"x": 1}}}'],
)
def test_setup_refuses_malformed_settings_before_changing_anything(
    home: Path, server: LiveServer, content: str
) -> None:
    settings_file = home / ".claude" / "settings.json"
    settings_file.parent.mkdir()
    settings_file.write_text(content)
    rec = Recorder()
    with pytest.raises(SetupError, match="settings.json"):
        run_setup(server.url, server.keys["desktop"], ["claude-code"], rec)
    assert settings_file.read_text() == content
    cfg = json.loads((home / ".config" / "kampung" / "config.json").read_text())
    assert cfg["key"] == server.keys["laptop"]  # not overwritten with desktop's
    assert not (home / ".config" / "kampung" / "kampung_hook.py").exists()
    assert rec.calls == []


def test_setup_channels_registers_push_adapter(home: Path, server: LiveServer) -> None:
    rec = Recorder()
    lines = run_setup(
        server.url, server.keys["laptop"], ["claude-code"], rec, channels=True
    )
    assert rec.calls[-1] == [
        "claude",
        "mcp",
        "add",
        "--scope",
        "user",
        "kampung",
        "--",
        *adapter_cmd("claude-code"),
        "--channels",
    ]
    assert any(
        "--dangerously-load-development-channels server:kampung" in line
        for line in lines
    )


def test_setup_without_channels_says_idle_sessions_wait(
    home: Path, server: LiveServer
) -> None:
    lines = run_setup(server.url, server.keys["laptop"], ["claude-code"], Recorder())
    hint = [line for line in lines if "--channels" in line]
    assert len(hint) == 1
    assert "idle" in hint[0]


def test_setup_codex_only_skips_channels_hint(home: Path, server: LiveServer) -> None:
    lines = run_setup(server.url, server.keys["laptop"], ["codex"], Recorder())
    assert not any("--channels" in line for line in lines)


def test_cli_setup_channels_flag(
    home: Path,
    server: LiveServer,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rec = Recorder()
    monkeypatch.setattr("kampung_client.setup.default_runner", rec)
    assert (
        cli.main(
            [
                "setup",
                "--server",
                server.url,
                "--key",
                server.keys["laptop"],
                "--channels",
            ]
        )
        == 0
    )
    assert rec.calls[-1][-2:] == ["claude-code", "--channels"]
