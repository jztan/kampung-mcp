"""`kampung-mcp uninstall`: remove what setup installed, and nothing else."""

import json
from pathlib import Path

import pytest

from client_testlib import LiveServer
from kampung_client import cli, hook, setup


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


def test_a_leftover_adapter_logs_nothing_after_uninstall(
    home: Path, server: LiveServer
) -> None:
    """Codex's daemon can keep an adapter running after its window closes; once
    the machine is uninstalled, its failures must not bring hooks.log back."""
    setup.uninstall(Recorder())
    hook.log_from_adapter("events", OSError("peer closed connection"))
    assert not (home / ".cache" / "kampung").exists()


def test_adapter_failures_are_still_logged_while_set_up(
    home: Path, server: LiveServer
) -> None:
    hook.log_from_adapter("events", OSError("peer closed connection"))
    assert "peer closed" in (home / ".cache" / "kampung" / "hooks.log").read_text()


def _codex_config(home: Path) -> Path:
    return home / ".codex" / "config.toml"


def _approvals(home: Path, quote: str) -> str:
    """Codex's own hook approvals, as it writes them: keyed by the hooks file,
    the event, and the entry and hook positions (literal strings on Windows)."""
    hooks = str(_codex_hooks(home))
    return "".join(
        f"[hooks.state.{quote}{hooks}:{event}:{i}:0{quote}]\n"
        f'trusted_hash = "sha256:{event}{i}"\n\n'
        for event, i in (("stop", 0), ("stop", 1), ("session_start", 0))
    )


@pytest.mark.parametrize("quote", ['"', "'"])
def test_uninstall_forgets_codex_approvals_of_our_hooks_only(
    home: Path, server: LiveServer, quote: str
) -> None:
    import tomllib

    mine = {"type": "command", "command": "echo mine"}
    path = _codex_hooks(home)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [mine]}]}}))
    setup.run_setup(server.url, server.keys["laptop"], ["codex"], Recorder())
    # Now Stop holds [mine, ours] and SessionStart holds [ours].
    head = 'model = "gpt"\n\n[projects."/home/dev/api"]\ntrust_level = "trusted"\n\n'
    _codex_config(home).write_text(head + "[hooks.state]\n\n" + _approvals(home, quote))

    setup.uninstall(Recorder())
    left = tomllib.loads(_codex_config(home).read_text())
    assert left["model"] == "gpt"
    assert left["projects"] == {"/home/dev/api": {"trust_level": "trusted"}}
    assert list(left["hooks"]["state"]) == [f"{_codex_hooks(home)}:stop:0:0"]
    assert json.loads(path.read_text()) == {"hooks": {"Stop": [{"hooks": [mine]}]}}


def test_uninstall_leaves_an_unreadable_codex_config_alone(
    home: Path, server: LiveServer
) -> None:
    setup.run_setup(server.url, server.keys["laptop"], ["codex"], Recorder())
    _codex_config(home).write_text("not = [valid toml\n" + _approvals(home, '"'))
    before = _codex_config(home).read_text()
    lines = setup.uninstall(Recorder())
    assert _codex_config(home).read_text() == before
    assert any("config.toml" in line for line in lines)
