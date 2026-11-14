"""`kampung-mcp uninstall`: remove what setup installed, and nothing else."""

import json
from pathlib import Path

import pytest

from client_testlib import LiveServer
from kampung_client import cli, setup


class Recorder:
    def __init__(self, missing: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.missing = missing

    def __call__(self, cmd: list[str]) -> int:
        self.calls.append(cmd)
        if self.missing:
            raise FileNotFoundError(cmd[0])
        return 0


OTHER_HOOK = {"hooks": [{"type": "command", "command": "echo mine"}]}


def _claude_settings(home: Path) -> Path:
    return home / ".claude" / "settings.json"


def _codex_hooks(home: Path) -> Path:
    return home / ".codex" / "hooks.json"


def test_uninstall_restores_settings_to_what_they_were(
    home: Path, server: LiveServer
) -> None:
    before = {
        "theme": "dark",
        "hooks": {"Stop": [OTHER_HOOK]},
        "deniedMcpServers": [{"serverName": "other"}],
    }
    path = _claude_settings(home)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(before))
    setup.run_setup(
        server.url, server.keys["laptop"], ["claude-code", "codex"], Recorder()
    )
    assert json.loads(path.read_text()) != before

    setup.uninstall(Recorder())
    assert json.loads(path.read_text()) == before


def test_uninstall_removes_files_setup_created(home: Path, server: LiveServer) -> None:
    setup.run_setup(
        server.url, server.keys["laptop"], ["claude-code", "codex"], Recorder()
    )
    assert _codex_hooks(home).exists() and _claude_settings(home).exists()
    setup.uninstall(Recorder())
    assert not _codex_hooks(home).exists()
    assert json.loads(_claude_settings(home).read_text()) == {}
    assert not (home / ".config" / "kampung" / "config.json").exists()


def test_uninstall_removes_both_mcp_registrations(
    home: Path, server: LiveServer
) -> None:
    rec = Recorder()
    setup.uninstall(rec)
    assert ["claude", "mcp", "remove", "--scope", "user", "kampung"] in rec.calls
    assert ["codex", "mcp", "remove", "kampung"] in rec.calls


def test_uninstall_without_the_agent_programs_still_cleans_files(
    home: Path, server: LiveServer
) -> None:
    setup.run_setup(server.url, server.keys["laptop"], ["claude-code"], Recorder())
    lines = setup.uninstall(Recorder(missing=True))
    assert not (home / ".config" / "kampung" / "config.json").exists()
    assert any("claude mcp remove" in line for line in lines)


def test_uninstall_on_a_clean_machine_changes_nothing(home: Path) -> None:
    path = _claude_settings(home)
    path.parent.mkdir(parents=True)
    path.write_text('{"theme": "dark"}')
    setup.uninstall(Recorder())
    assert path.read_text() == '{"theme": "dark"}'
    assert not _codex_hooks(home).exists()


def test_uninstall_refuses_a_broken_settings_file(home: Path) -> None:
    path = _claude_settings(home)
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    with pytest.raises(setup.SetupError, match="not valid JSON"):
        setup.uninstall(Recorder())
    assert path.read_text() == "{not json"


def test_cli_uninstall(
    home: Path,
    server: LiveServer,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(setup, "default_runner", Recorder())
    assert cli.main(["uninstall"]) == 0
    assert "Removed" in capsys.readouterr().out
    assert not (home / ".config" / "kampung" / "config.json").exists()
