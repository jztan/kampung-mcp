import json
import shlex
import stat
import sys
from pathlib import Path

import pytest

from client_testlib import LiveServer
from kampung_client import cli, hook
from kampung_client.setup import SetupError, run_setup

EVENTS = ["SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"]


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
    copy = home / ".config" / "kampung" / "kampung_hook.py"
    assert copy.read_bytes() == Path(hook.__file__).read_bytes()


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
    hook_path = home / ".config" / "kampung" / "kampung_hook.py"
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(hook_path))}"
    for event in EVENTS:
        ours = [
            e
            for e in s["hooks"][event]
            if "kampung_hook.py" in e["hooks"][0]["command"]
        ]
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
    run_setup(server.url, server.keys["laptop"], ["claude-code"], Recorder())
    cmd = settings(spaced)["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert shlex.split(cmd)[1] == str(
        spaced / ".config" / "kampung" / "kampung_hook.py"
    )


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
