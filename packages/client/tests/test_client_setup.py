import json
import shlex
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from client_testlib import LiveServer, dead_pid, set_home
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


def test_setup_writes_private_config_and_hook(
    home: Path, server: LiveServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(setup, "_windows", lambda: False)  # POSIX quoting
    (home / ".config").mkdir(exist_ok=True)
    run_setup(server.url + "/", server.keys["laptop"], ["claude-code"], Recorder())
    cfg = home / ".config" / "kampung" / "config.json"
    assert json.loads(cfg.read_text()) == {
        "server": server.url,
        "key": server.keys["laptop"],
    }
    if sys.platform != "win32":  # chmod only sets the read-only bit there
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


def test_setup_merges_claude_hooks_idempotently(
    home: Path, server: LiveServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(setup, "_windows", lambda: False)  # POSIX quoting
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
    monkeypatch.setattr(setup, "_windows", lambda: False)  # POSIX quoting
    spaced = tmp_path / "my home"
    spaced.mkdir()
    set_home(monkeypatch, spaced)
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


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX hook command; Windows gets the PowerShell form (cmd has no true)",
)
def test_hook_command_exits_0_when_the_hook_file_is_gone(tmp_path: Path) -> None:
    # Python exits 2 for a missing script, and Claude Code reads exit 2 from a
    # hook as "block" (a UserPromptSubmit hook would block every prompt).
    cmd = setup.hook_command(str(tmp_path / "gone" / "hook.py"))
    assert subprocess.run(cmd, shell=True, capture_output=True).returncode == 0


def test_powershell_hook_command_quotes_for_powershell() -> None:
    cmd = setup.powershell_hook_command(r"C:\it's\hook.py")
    assert cmd == f"& '{sys.executable}' 'C:\\it''s\\hook.py'; exit 0"


def test_setup_runs_claude_hooks_in_powershell_on_windows(
    home: Path, server: LiveServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Without Git Bash, Claude Code runs hooks in PowerShell, where 5.1 cannot
    # parse `|| true`. Naming the shell works with or without Git Bash.
    monkeypatch.setattr(setup, "_windows", lambda: True)
    run_setup(server.url, server.keys["laptop"], ["claude-code"], Recorder())
    for event in EVENTS:
        [only] = settings(home)["hooks"][event]
        [entry] = only["hooks"]
        assert entry["shell"] == "powershell"
        assert entry["command"] == setup.powershell_hook_command(hook.__file__)


def test_setup_names_no_shell_elsewhere(
    home: Path, server: LiveServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(setup, "_windows", lambda: False)  # POSIX quoting
    run_setup(server.url, server.keys["laptop"], ["claude-code"], Recorder())
    assert "shell" not in settings(home)["hooks"]["Stop"][0]["hooks"][0]


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


def codex_hooks(home: Path) -> dict:  # type: ignore[type-arg]
    return json.loads((home / ".codex" / "hooks.json").read_text())


@pytest.fixture
def codex_at(monkeypatch: pytest.MonkeyPatch) -> str:
    """Pretend `codex` is installed here, on macOS or Linux (setup skips Codex
    hooks and waking on Windows), whatever this machine is."""
    monkeypatch.setattr(setup.shutil, "which", lambda name: "/opt/x/codex")
    monkeypatch.setattr(setup, "_windows", lambda: False)
    return "/opt/x/codex"


def test_setup_codex_registers_adapter(
    home: Path, server: LiveServer, codex_at: str
) -> None:
    rec = Recorder()
    run_setup(server.url, server.keys["laptop"], ["codex"], rec)
    assert not (home / ".claude" / "settings.json").exists()
    assert rec.calls == [
        ["codex", "mcp", "remove", "kampung"],
        [
            "codex",
            "mcp",
            "add",
            "kampung",
            "--",
            *adapter_cmd("codex"),
            "--codex-bin",
            codex_at,
        ],
    ]


FOREIGN = {"type": "command", "command": "afplay /System/Library/Sounds/Glass.aiff"}


def test_setup_codex_merges_hooks_and_keeps_foreign(
    home: Path, server: LiveServer, codex_at: str
) -> None:
    path = home / ".codex" / "hooks.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [FOREIGN]}]}}))
    for _ in range(2):
        run_setup(server.url, server.keys["laptop"], ["codex"], Recorder())
    hooks = codex_hooks(home)["hooks"]
    assert set(hooks) == set(setup.EVENTS)
    assert hooks["Stop"][0] == {"hooks": [FOREIGN]} and len(hooks["Stop"]) == 2
    for event in setup.EVENTS:
        (ours,) = [e for e in hooks[event] if e != {"hooks": [FOREIGN]}]
        handler = ours["hooks"][0]
        assert handler["type"] == "command"
        assert handler["timeout"] == (3 if event == "SessionEnd" else 5)
        assert shlex.split(handler["command"])[:4] == [
            sys.executable,
            hook.__file__,
            "--agent",
            "codex",
        ]
    backup = home / ".codex" / "hooks.json.kampung-backup"
    assert json.loads(backup.read_text()) == {"hooks": {"Stop": [{"hooks": [FOREIGN]}]}}


@pytest.mark.parametrize("content", ["{not json", "[1]", '{"hooks": {"Stop": 1}}'])
def test_setup_codex_refuses_malformed_hooks_before_changing_anything(
    home: Path, server: LiveServer, codex_at: str, content: str
) -> None:
    path = home / ".codex" / "hooks.json"
    path.parent.mkdir()
    path.write_text(content)
    rec = Recorder()
    with pytest.raises(SetupError, match="hooks.json"):
        run_setup(server.url, server.keys["desktop"], ["codex"], rec)
    assert path.read_text() == content
    cfg = json.loads((home / ".config" / "kampung" / "config.json").read_text())
    assert cfg["key"] == server.keys["laptop"]
    assert rec.calls == []


def test_setup_codex_without_codex_on_path_has_no_waker(
    home: Path, server: LiveServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(setup.shutil, "which", lambda name: None)
    monkeypatch.setattr(setup, "_windows", lambda: False)
    rec = Recorder()
    lines = run_setup(server.url, server.keys["laptop"], ["codex"], rec)
    assert "--codex-bin" not in rec.calls[-1]
    assert any("codex was not found" in line for line in lines)


def test_setup_codex_says_approve_hooks_and_caveat(
    home: Path, server: LiveServer, codex_at: str
) -> None:
    lines = run_setup(server.url, server.keys["laptop"], ["codex"], Recorder())
    assert any("/hooks" in line for line in lines)
    assert any("run unattended" in line for line in lines)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX hook command; Windows gets the PowerShell form (cmd has no true)",
)
def test_hook_command_codex_exits_0_when_the_hook_file_is_gone(
    tmp_path: Path,
) -> None:
    cmd = setup.codex_hook_command(str(tmp_path / "gone" / "hook.py"))
    assert subprocess.run(cmd, shell=True, capture_output=True).returncode == 0


def test_setup_codex_on_windows_stays_polling(
    home: Path, server: LiveServer, codex_at: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(setup, "_windows", lambda: True)
    rec = Recorder()
    lines = run_setup(server.url, server.keys["laptop"], ["codex"], rec)
    assert not (home / ".codex" / "hooks.json").exists()
    assert rec.calls[-1] == [
        "codex",
        "mcp",
        "add",
        "kampung",
        "--",
        *adapter_cmd("codex"),
    ]
    assert any("polling" in line and "Windows" in line for line in lines)


def test_setup_missing_cli_prints_manual_command(
    home: Path, server: LiveServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(setup, "_windows", lambda: False)  # POSIX quoting
    lines = run_setup(
        server.url, server.keys["laptop"], ["claude-code"], Recorder(missing=True)
    )
    assert any("claude mcp add --scope user kampung --" in line for line in lines)


def test_setup_failed_cli_prints_manual_command(
    home: Path, server: LiveServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(setup, "_windows", lambda: False)  # POSIX quoting
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
    hook.write_session_file("dead", {"ancestors": [[dead_pid(), "x"]]})
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


def test_setup_keeps_foreign_hook_in_shared_entry(
    home: Path, server: LiveServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(setup, "_windows", lambda: False)  # POSIX quoting
    command = f"{shlex.quote(sys.executable)} {shlex.quote(hook.__file__)} || true"
    other = {"type": "command", "command": "other.sh"}
    ours = {"type": "command", "command": command, "timeout": 5}
    shared = {"matcher": "*", "hooks": [other, ours]}
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"hooks": {"Stop": [shared]}})
    )
    for _ in range(2):
        run_setup(server.url, server.keys["laptop"], ["claude-code"], Recorder())
    assert settings(home)["hooks"]["Stop"] == [
        {"matcher": "*", "hooks": [other]},
        {"hooks": [ours]},
    ]


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges")
def test_setup_keeps_settings_symlink(home: Path, server: LiveServer) -> None:
    dotfiles = home / "dotfiles" / "settings.json"
    dotfiles.parent.mkdir()
    dotfiles.write_text(json.dumps({"model": "x"}))
    (home / ".claude").mkdir()
    link = home / ".claude" / "settings.json"
    link.symlink_to(dotfiles)
    run_setup(server.url, server.keys["laptop"], ["claude-code"], Recorder())
    assert link.is_symlink()
    data = json.loads(dotfiles.read_text())
    assert data["model"] == "x" and "Stop" in data["hooks"]


def test_setup_leaves_no_tmp_files(home: Path, server: LiveServer) -> None:
    run_setup(server.url, server.keys["laptop"], ["claude-code"], Recorder())
    assert [p.name for p in (home / ".claude").iterdir() if "tmp" in p.name] == []


def test_setup_denies_the_claude_app_connector_in_claude_code(
    home: Path, server: LiveServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(setup, "_windows", lambda: False)
    existing = {"deniedMcpServers": [{"serverName": "other"}]}
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text(json.dumps(existing))
    for _ in range(2):
        lines = run_setup(
            server.url, server.keys["laptop"], ["claude-code"], Recorder()
        )
    assert settings(home)["deniedMcpServers"] == [
        {"serverName": "other"},
        {"serverName": "claude.ai kampung"},
        {"serverUrl": f"{server.url}/*"},
    ]
    assert any("claude.ai kampung" in line for line in lines)


def test_connector_pattern_covers_the_server_whatever_the_connector_is_named() -> None:
    assert setup.connector_url_pattern("https://mail.example.com") == (
        "https://mail.example.com/*"
    )
    assert setup.connector_url_pattern("https://Mail.Example.com:8443/") == (
        "https://mail.example.com:8443/*"
    )
    assert setup.connector_url_pattern("http://[::1]:8000") == "http://[::1]:8000/*"


def test_setup_denies_the_connector_by_its_given_name(
    home: Path, server: LiveServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(setup, "_windows", lambda: False)
    run_setup(
        server.url,
        server.keys["laptop"],
        ["claude-code"],
        Recorder(),
        connector_name="Home mail",
    )
    assert settings(home)["deniedMcpServers"] == [
        {"serverName": "claude.ai Home mail"},
        {"serverUrl": f"{server.url}/*"},
    ]


def test_setup_refuses_malformed_denied_servers_before_changing_anything(
    home: Path, server: LiveServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(setup, "_windows", lambda: False)
    (home / ".claude").mkdir()
    path = home / ".claude" / "settings.json"
    path.write_text(json.dumps({"deniedMcpServers": "nope"}))
    rec = Recorder()
    with pytest.raises(SetupError, match="deniedMcpServers"):
        run_setup(server.url, server.keys["desktop"], ["claude-code"], rec)
    assert json.loads(path.read_text()) == {"deniedMcpServers": "nope"}
    cfg = json.loads((hook.config_dir() / "config.json").read_text())
    assert cfg["key"] == server.keys["laptop"]
    assert rec.calls == []


def test_setup_codex_only_leaves_claude_settings_alone(
    home: Path, server: LiveServer, codex_at: str
) -> None:
    run_setup(server.url, server.keys["laptop"], ["codex"], Recorder())
    assert not (home / ".claude" / "settings.json").exists()


# Idle wake: the version floor, the new entries, the wake wrapper -----------

IDLE_EVENTS = ["FileChanged", "CwdChanged"]
OLD_HINT = "until you type"
ReadVersion = Callable[[], "tuple[int, int, int] | None"]


def _ps(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def test_claude_hook_items_spell_out_the_entries() -> None:
    py, script = shlex.quote(sys.executable), shlex.quote(hook.__file__)
    plain = {"type": "command", "command": f"{py} {script} || true", "timeout": 5}
    start = {**plain, "command": f"{py} {script} --idle-wake || true"}
    wake = {
        "type": "command",
        "command": f"[ -f {script} ] || exit 0; {py} {script} --wake; "
        "[ $? -eq 2 ] && exit 2; exit 0",
        "asyncRewake": True,
        "timeout": 5,
    }
    items = setup.claude_hook_items(True, False)
    assert items == {
        "SessionStart": [start],
        "UserPromptSubmit": [plain],
        "Stop": [plain],
        "SessionEnd": [plain],
        "PostToolUse": [plain],
        "FileChanged": [wake, plain],
        "CwdChanged": [plain],
    }
    # Only the wake hook may wake a session: asyncRewake on any other entry
    # would turn every exit 2 into a turn.
    assert items["FileChanged"][0]["asyncRewake"] is True
    assert json.dumps(items).count("asyncRewake") == 1


def test_claude_hook_items_for_powershell() -> None:
    py, script = _ps(sys.executable), _ps(hook.__file__)
    plain = {
        "type": "command",
        "command": f"& {py} {script}; exit 0",
        "timeout": 5,
        "shell": "powershell",
    }
    start = {**plain, "command": f"& {py} {script} --idle-wake; exit 0"}
    wake = {
        **plain,
        "command": f"if (-not (Test-Path -LiteralPath {script})) {{ exit 0 }}; "
        f"& {py} {script} --wake; if ($LASTEXITCODE -eq 2) {{ exit 2 }}; exit 0",
        "asyncRewake": True,
    }
    assert setup.claude_hook_items(True, True) == {
        "SessionStart": [start],
        "UserPromptSubmit": [plain],
        "Stop": [plain],
        "SessionEnd": [plain],
        "PostToolUse": [plain],
        "FileChanged": [wake, plain],
        "CwdChanged": [plain],
    }


@pytest.mark.parametrize("windows", [False, True])
def test_claude_hook_items_without_idle_wake_are_the_five_plain_entries(
    windows: bool,
) -> None:
    command = (
        setup.powershell_hook_command(hook.__file__)
        if windows
        else setup.hook_command(hook.__file__)
    )
    item = {"type": "command", "command": command, "timeout": 5}
    if windows:
        item["shell"] = "powershell"
    assert setup.claude_hook_items(False, windows) == {e: [item] for e in EVENTS}


@pytest.mark.parametrize("windows", [False, True])
@pytest.mark.parametrize("version", [(2, 1, 321), (2, 2, 0), (3, 0, 0)])
def test_setup_at_the_floor_writes_the_idle_wake_entries(
    home: Path,
    server: LiveServer,
    monkeypatch: pytest.MonkeyPatch,
    windows: bool,
    version: tuple[int, int, int],
) -> None:
    monkeypatch.setattr(setup, "_windows", lambda: windows)
    lines = run_setup(
        server.url,
        server.keys["laptop"],
        ["claude-code"],
        Recorder(),
        claude_version=version,
    )
    items = setup.claude_hook_items(True, windows)
    assert sorted(items) == sorted(EVENTS + IDLE_EVENTS)
    hooks = settings(home)["hooks"]
    for event in EVENTS + IDLE_EVENTS:
        assert hooks[event] == [{"hooks": items[event]}]
    assert (
        "Idle wake is on: an idle Claude Code session, also one in the "
        "background, wakes for mail within a few seconds. Restart open Claude "
        "Code sessions to load it."
    ) in lines
    assert not any(OLD_HINT in line for line in lines)


@pytest.mark.parametrize(
    "version, line",
    [
        (
            (2, 1, 320),
            "Claude Code 2.1.320 is older than 2.1.321, so idle wake is off. "
            "Upgrade Claude Code and run setup again.",
        ),
        (
            None,
            "Could not read the Claude Code version (claude --version), so idle "
            "wake is off. Run setup again once that works.",
        ),
    ],
)
def test_setup_under_the_floor_writes_none_of_them(
    home: Path,
    server: LiveServer,
    monkeypatch: pytest.MonkeyPatch,
    version: tuple[int, int, int] | None,
    line: str,
) -> None:
    monkeypatch.setattr(setup, "_windows", lambda: False)  # POSIX quoting
    lines = run_setup(
        server.url,
        server.keys["laptop"],
        ["claude-code"],
        Recorder(),
        claude_version=version,
    )
    hooks = settings(home)["hooks"]
    assert sorted(hooks) == sorted(EVENTS)
    for event in EVENTS:
        assert hooks[event] == [{"hooks": setup.claude_hook_items(False, False)[event]}]
    assert "--idle-wake" not in json.dumps(hooks)
    # The off line, then today's hint about typing or Channels.
    at = lines.index(line)
    assert OLD_HINT in lines[at + 1] and "--channels" in lines[at + 1]
    assert not any("Idle wake is on" in x for x in lines)


def test_setup_with_channels_and_no_idle_wake_skips_the_typing_hint(
    home: Path, server: LiveServer
) -> None:
    lines = run_setup(
        server.url,
        server.keys["laptop"],
        ["claude-code"],
        Recorder(),
        channels=True,
        claude_version=(2, 1, 320),
    )
    assert any("older than 2.1.321" in x for x in lines)
    assert any("Channels push is on" in x for x in lines)
    assert not any(OLD_HINT in x for x in lines)


def test_setup_with_channels_and_idle_wake_says_both(
    home: Path, server: LiveServer
) -> None:
    lines = run_setup(
        server.url,
        server.keys["laptop"],
        ["claude-code"],
        Recorder(),
        channels=True,
        claude_version=(2, 1, 322),
    )
    assert any("Idle wake is on" in x for x in lines)
    assert any("Channels push is on" in x for x in lines)


def test_setup_under_the_floor_removes_entries_from_an_earlier_run(
    home: Path, server: LiveServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(setup, "_windows", lambda: False)  # POSIX quoting
    other = {"matcher": ".env", "hooks": [{"type": "command", "command": "direnv"}]}
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"hooks": {"CwdChanged": [other]}})
    )
    for version in ((2, 1, 322), (2, 1, 320)):
        run_setup(
            server.url,
            server.keys["laptop"],
            ["claude-code"],
            Recorder(),
            claude_version=version,
        )
    hooks = settings(home)["hooks"]
    # An event left with nothing is removed; another tool's entry stays.
    assert "FileChanged" not in hooks and hooks["CwdChanged"] == [other]
    assert "--idle-wake" not in json.dumps(hooks) and "--wake" not in json.dumps(hooks)
    for event in EVENTS:
        assert hooks[event] == [{"hooks": setup.claude_hook_items(False, False)[event]}]


def test_setup_is_idempotent_with_idle_wake(home: Path, server: LiveServer) -> None:
    seen = []
    for _ in range(2):
        run_setup(
            server.url,
            server.keys["laptop"],
            ["claude-code"],
            Recorder(),
            claude_version=(2, 1, 322),
        )
        seen.append(settings(home))
    assert seen[0] == seen[1]
    assert all(len(seen[1]["hooks"][e]) == 1 for e in EVENTS + IDLE_EVENTS)


def test_setup_keeps_other_tools_hooks_on_the_new_events(
    home: Path, server: LiveServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(setup, "_windows", lambda: False)  # POSIX quoting
    direnv = {
        "matcher": ".env",
        "hooks": [{"type": "command", "command": "direnv reload"}],
    }
    other = {"hooks": [{"type": "command", "command": "other.sh"}]}
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"hooks": {"FileChanged": [direnv], "CwdChanged": [other]}})
    )
    for _ in range(2):
        run_setup(
            server.url,
            server.keys["laptop"],
            ["claude-code"],
            Recorder(),
            claude_version=(2, 1, 322),
        )
    items = setup.claude_hook_items(True, False)
    hooks = settings(home)["hooks"]
    assert hooks["FileChanged"] == [direnv, {"hooks": items["FileChanged"]}]
    assert hooks["CwdChanged"] == [other, {"hooks": items["CwdChanged"]}]


@pytest.mark.parametrize("version", [(2, 1, 322), None])
@pytest.mark.parametrize("event", IDLE_EVENTS)
def test_setup_refuses_a_malformed_new_event(
    home: Path,
    server: LiveServer,
    event: str,
    version: tuple[int, int, int] | None,
) -> None:
    content = json.dumps({"hooks": {event: {"x": 1}}})
    settings_file = home / ".claude" / "settings.json"
    settings_file.parent.mkdir()
    settings_file.write_text(content)
    rec = Recorder()
    with pytest.raises(SetupError, match=f"hooks.{event} should be a list"):
        run_setup(
            server.url,
            server.keys["desktop"],
            ["claude-code"],
            rec,
            claude_version=version,
        )
    assert settings_file.read_text() == content and rec.calls == []


def test_setup_codex_hooks_stay_the_five_events(
    home: Path, server: LiveServer, codex_at: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(setup, "_windows", lambda: False)
    run_setup(
        server.url,
        server.keys["laptop"],
        ["claude-code", "codex"],
        Recorder(),
        claude_version=(2, 1, 322),
    )
    hooks = codex_hooks(home)["hooks"]
    assert sorted(hooks) == sorted(EVENTS)
    assert "--idle-wake" not in json.dumps(hooks) and "--wake" not in json.dumps(hooks)


@pytest.mark.parametrize(
    "text, version",
    [
        ("2.1.322 (Claude Code)", (2, 1, 322)),
        ("claude 2.1.83-beta.1", (2, 1, 83)),
        ("10.20.300\n", (10, 20, 300)),
        ("", None),
        ("unknown", None),
        ("2.1", None),
    ],
)
def test_parse_claude_version(text: str, version: tuple[int, int, int] | None) -> None:
    assert setup.parse_claude_version(text) == version


def test_the_floor_is_2_1_321() -> None:
    assert setup.IDLE_WAKE_FLOOR == (2, 1, 321)
    assert setup.IDLE_WAKE_EVENTS == ("FileChanged", "CwdChanged")
    assert list(setup.EVENTS) == EVENTS


def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))


@pytest.mark.skipif(sys.platform == "win32", reason="sh stand-in for claude")
@pytest.mark.parametrize(
    "body, version",
    [
        ('[ "$1" = --version ] && echo "2.1.322 (Claude Code)"', (2, 1, 322)),
        ("echo 2.1.322; exit 1", None),
        ("echo no version here", None),
        ("exit 0", None),
    ],
)
def test_read_claude_version_asks_claude(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    real_claude_version: ReadVersion,
    body: str,
    version: tuple[int, int, int] | None,
) -> None:
    fake_claude(tmp_path, monkeypatch, body)
    assert real_claude_version() == version


def test_read_claude_version_is_none_without_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, real_claude_version: ReadVersion
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    assert real_claude_version() is None


@pytest.mark.skipif(sys.platform == "win32", reason="sh stand-in for claude")
def test_read_claude_version_gives_up_on_a_claude_that_hangs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, real_claude_version: ReadVersion
) -> None:
    fake_claude(tmp_path, monkeypatch, "exec /bin/sleep 30")
    monkeypatch.setattr(setup, "VERSION_TIMEOUT", 0.3)
    started = time.monotonic()
    assert real_claude_version() is None
    assert time.monotonic() - started < 5


def test_cli_setup_reads_the_claude_version(
    home: Path,
    server: LiveServer,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asked: list[int] = []

    def version() -> tuple[int, int, int]:
        asked.append(1)
        return (2, 1, 322)

    monkeypatch.setattr("kampung_client.setup.default_runner", Recorder())
    monkeypatch.setattr(setup, "read_claude_version", version)
    args = ["setup", "--server", server.url, "--key", server.keys["laptop"]]
    assert cli.main(args + ["--agent", "codex"]) == 0
    assert asked == [] and not (home / ".claude" / "settings.json").exists()
    assert cli.main(args) == 0
    assert asked == [1] and "FileChanged" in settings(home)["hooks"]
    assert "Idle wake is on" in capsys.readouterr().out


def stand_in_hook(tmp_path: Path, code: int) -> Path:
    script = tmp_path / "it's here" / "kampung_client" / "hook.py"
    script.parent.mkdir(parents=True)
    script.write_text(f"import sys; sys.stderr.write('mail'); sys.exit({code})")
    return script


@pytest.mark.skipif(sys.platform == "win32", reason="bash wrapper")
@pytest.mark.parametrize("code, expected", [(2, 2), (0, 0), (1, 0), (3, 0)])
def test_wake_command_passes_only_exit_2_through(
    tmp_path: Path, code: int, expected: int
) -> None:
    script = stand_in_hook(tmp_path, code)
    done = subprocess.run(
        ["sh", "-c", setup.wake_hook_command(str(script))],
        capture_output=True,
        text=True,
    )
    assert done.returncode == expected and done.stderr == "mail"


@pytest.mark.skipif(sys.platform == "win32", reason="bash wrapper")
def test_wake_command_exits_0_when_the_hook_file_is_gone(tmp_path: Path) -> None:
    gone = tmp_path / "it's gone" / "hook.py"
    done = subprocess.run(
        ["sh", "-c", setup.wake_hook_command(str(gone))], capture_output=True
    )
    assert done.returncode == 0


@pytest.mark.skipif(sys.platform == "win32", reason="bash wrapper")
def test_wake_command_exits_0_when_the_interpreter_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = stand_in_hook(tmp_path, 2)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "gone" / "python"))
    done = subprocess.run(
        ["sh", "-c", setup.wake_hook_command(str(script))], capture_output=True
    )
    assert done.returncode == 0


POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")
needs_powershell = pytest.mark.skipif(POWERSHELL is None, reason="no PowerShell here")


@needs_powershell
@pytest.mark.parametrize("code, expected", [(2, 2), (0, 0), (1, 0), (3, 0)])
def test_powershell_wake_command_passes_only_exit_2_through(
    tmp_path: Path, code: int, expected: int
) -> None:
    script = stand_in_hook(tmp_path, code)
    command = setup.powershell_wake_hook_command(str(script))
    done = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
    )
    assert done.returncode == expected and "mail" in done.stderr


@needs_powershell
def test_powershell_wake_command_exits_0_when_the_hook_file_is_gone(
    tmp_path: Path,
) -> None:
    gone = tmp_path / "it's gone" / "hook.py"
    command = setup.powershell_wake_hook_command(str(gone))
    done = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-Command", command], capture_output=True
    )
    assert done.returncode == 0


def test_hook_commands_put_flags_before_the_guard() -> None:
    py, script = shlex.quote(sys.executable), shlex.quote(hook.__file__)
    assert setup.hook_command(hook.__file__) == f"{py} {script} || true"
    assert (
        setup.hook_command(hook.__file__, "--idle-wake")
        == f"{py} {script} --idle-wake || true"
    )
    assert (
        setup.powershell_hook_command(r"C:\it's\hook.py", "--idle-wake")
        == f"& '{sys.executable}' 'C:\\it''s\\hook.py' --idle-wake; exit 0"
    )
