import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from client_testlib import LiveServer, scripted_server, write_config
from kampung_client import __version__, cli, doctor, hook, setup


def _registered(cmd: list[str]) -> tuple[int, str]:
    return 0, "kampung:\n  Status: Connected\n"


def _claude_registered(cmd: list[str]) -> tuple[int, str]:
    return _registered(cmd) if cmd[0] == "claude" else _unregistered(cmd)


def _unregistered(cmd: list[str]) -> tuple[int, str]:
    return 1, "No MCP server found with name: kampung\n"


def _no_agent_cli(cmd: list[str]) -> tuple[int, str]:
    raise FileNotFoundError(cmd[0])


def _install_hooks(home: Path, command: str | None = None) -> Path:
    setup._merge_hooks(
        setup._claude_settings_path(),
        {},
        command or setup.hook_command(hook.__file__),
        5,
    )
    return setup._claude_settings_path()


def _by_name(checks: list[doctor.Check], name: str) -> doctor.Check:
    return next(c for c in checks if c.name == name)


def _healthy(
    server: LiveServer, home: Path, query: doctor.Query = _claude_registered
) -> list[doctor.Check]:
    _install_hooks(home)
    (home / ".config" / "kampung" / "config.json").chmod(0o600)
    return doctor.run_checks(query)


def test_a_healthy_machine_passes_every_check(home: Path, server: LiveServer) -> None:
    checks = _healthy(server, home)
    assert {c.status for c in checks} == {doctor.OK}
    names = [c.name for c in checks]
    assert names == [
        "config",
        "server",
        "key",
        "version",
        "claude-code hooks",
        "claude-code mcp",
        "hooks.log",
    ]


def test_missing_config_says_to_run_setup(home: Path) -> None:
    checks = doctor.run_checks(_no_agent_cli)
    config = _by_name(checks, "config")
    assert config.status == doctor.FAIL
    assert "kampung-mcp setup" in config.fix
    assert "server" not in [c.name for c in checks]


def test_plain_http_config_is_refused_before_any_request(home: Path) -> None:
    httpd, hits = scripted_server(404)
    port = httpd.server_address[1]
    # A routable-looking name, so the plain http rule applies.
    d = home / ".config" / "kampung"
    d.mkdir(parents=True)
    (d / "config.json").write_text(
        json.dumps({"server": f"http://example.test:{port}", "key": "kmp_x"})
    )
    checks = doctor.run_checks(_no_agent_cli)
    assert _by_name(checks, "config").status == doctor.FAIL
    assert hits == []
    httpd.shutdown()


def test_allowed_insecure_http_warns(home: Path, server: LiveServer) -> None:
    path = home / ".config" / "kampung" / "config.json"
    data = {"server": "http://example.test:1", "key": "kmp_x"}
    path.write_text(json.dumps({**data, "allow_insecure_http": True}))
    path.chmod(0o600)
    config = _by_name(doctor.run_checks(_no_agent_cli), "config")
    assert config.status == doctor.WARN
    assert "plain http" in config.detail


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_readable_config_warns(home: Path, server: LiveServer) -> None:
    (home / ".config" / "kampung" / "config.json").chmod(0o644)
    config = _by_name(doctor.run_checks(_no_agent_cli), "config")
    assert config.status == doctor.WARN
    assert "chmod 600" in config.fix


def test_unreachable_server_stops_the_server_checks(home: Path) -> None:
    write_config(home, "http://127.0.0.1:1", "kmp_x")
    checks = doctor.run_checks(_no_agent_cli)
    assert _by_name(checks, "server").status == doctor.FAIL
    assert "key" not in [c.name for c in checks]


def test_something_else_on_the_url_fails(home: Path) -> None:
    httpd, _ = scripted_server(503)
    port = httpd.server_address[1]
    write_config(home, f"http://127.0.0.1:{port}", "kmp_x")
    server = _by_name(doctor.run_checks(_no_agent_cli), "server")
    assert server.status == doctor.FAIL
    assert "503" in server.detail
    httpd.shutdown()


def test_rejected_key_fails(home: Path, server: LiveServer) -> None:
    write_config(home, server.url, "kmp_wrong")
    key = _by_name(doctor.run_checks(_no_agent_cli), "key")
    assert key.status == doctor.FAIL
    assert "set-key" in key.fix


@pytest.mark.parametrize(
    "theirs, status",
    [
        (__version__, doctor.OK),
        ("0.1.99", doctor.OK),  # patch releases stay compatible
        ("9.9.0", doctor.WARN),
        ("", doctor.WARN),
    ],
)
def test_version_comparison(
    home: Path,
    server: LiveServer,
    monkeypatch: pytest.MonkeyPatch,
    theirs: str,
    status: str,
) -> None:
    monkeypatch.setattr(doctor, "__version__", "0.1.0")
    monkeypatch.setattr(doctor, "_health", lambda url: (200, theirs))
    version = _by_name(doctor.run_checks(_no_agent_cli), "version")
    assert version.status == status
    assert "0.1.0" in version.detail


def test_no_agent_set_up_fails(home: Path, server: LiveServer) -> None:
    agents = _by_name(doctor.run_checks(_unregistered), "agents")
    assert agents.status == doctor.FAIL
    assert "kampung-mcp setup" in agents.fix


def test_missing_hook_event_fails(home: Path, server: LiveServer) -> None:
    path = _install_hooks(home)
    data = json.loads(path.read_text())
    del data["hooks"]["Stop"]
    path.write_text(json.dumps(data))
    hooks = _by_name(doctor.run_checks(_claude_registered), "claude-code hooks")
    assert hooks.status == doctor.FAIL
    assert "Stop" in hooks.detail
    assert "kampung-mcp setup" in hooks.fix


def test_moved_install_fails(home: Path, server: LiveServer, tmp_path: Path) -> None:
    gone = tmp_path / "gone" / "kampung_client" / "hook.py"
    _install_hooks(home, setup.hook_command(str(gone)))
    hooks = _by_name(doctor.run_checks(_claude_registered), "claude-code hooks")
    assert hooks.status == doctor.FAIL
    assert str(gone) in hooks.detail


def test_gone_interpreter_fails(home: Path, server: LiveServer, tmp_path: Path) -> None:
    command = f"{tmp_path / 'gone' / 'python'} {hook.__file__} || true"
    _install_hooks(home, command)
    hooks = _by_name(doctor.run_checks(_claude_registered), "claude-code hooks")
    assert hooks.status == doctor.FAIL
    assert "gone" in hooks.detail


def test_hook_that_crashes_fails(
    home: Path, server: LiveServer, tmp_path: Path
) -> None:
    broken = tmp_path / "kampung_client" / "hook.py"
    broken.parent.mkdir()
    broken.write_text("raise SystemExit(3)\n")
    _install_hooks(home, setup.hook_command(str(broken)))
    hooks = _by_name(doctor.run_checks(_claude_registered), "claude-code hooks")
    assert hooks.status == doctor.FAIL
    assert "exit 3" in hooks.detail


def test_hooks_from_another_install_warn(
    home: Path, server: LiveServer, tmp_path: Path
) -> None:
    other = tmp_path / "kampung_client" / "hook.py"
    other.parent.mkdir()
    other.write_text("")
    _install_hooks(home, setup.hook_command(str(other)))
    hooks = _by_name(doctor.run_checks(_claude_registered), "claude-code hooks")
    assert hooks.status == doctor.WARN
    assert str(other) in hooks.detail


def test_broken_settings_file_fails(home: Path, server: LiveServer) -> None:
    path = setup._claude_settings_path()
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    hooks = _by_name(doctor.run_checks(_claude_registered), "claude-code hooks")
    assert hooks.status == doctor.FAIL


def test_unregistered_mcp_server_fails(home: Path, server: LiveServer) -> None:
    mcp = _by_name(_healthy(server, home, _unregistered), "claude-code mcp")
    assert mcp.status == doctor.FAIL
    assert "kampung-mcp setup" in mcp.fix


def test_mcp_server_that_will_not_start_fails(home: Path, server: LiveServer) -> None:
    def failed(cmd: list[str]) -> tuple[int, str]:
        return 0, "kampung:\n  Status: ✘ Failed to connect\n"

    assert _by_name(_healthy(server, home, failed), "claude-code mcp").status == (
        doctor.FAIL
    )


def test_disabled_codex_server_fails(home: Path, server: LiveServer) -> None:
    def disabled(cmd: list[str]) -> tuple[int, str]:
        return 0, "kampung\n  enabled: false\n"

    setup._merge_codex_hooks({})
    assert _by_name(doctor.run_checks(disabled), "codex mcp").status == doctor.FAIL


def test_agent_cli_not_on_path_warns(home: Path, server: LiveServer) -> None:
    mcp = _by_name(_healthy(server, home, _no_agent_cli), "claude-code mcp")
    assert mcp.status == doctor.WARN
    assert "claude" in mcp.detail


def test_agent_cli_that_hangs_warns(home: Path, server: LiveServer) -> None:
    def hangs(cmd: list[str]) -> tuple[int, str]:
        raise TimeoutError

    assert _by_name(_healthy(server, home, hangs), "claude-code mcp").status == (
        doctor.WARN
    )


@pytest.mark.skipif(sys.platform == "win32", reason="Codex hooks are POSIX only")
def test_both_agents_are_checked(home: Path, server: LiveServer) -> None:
    _install_hooks(home)
    setup._merge_codex_hooks({})
    seen: list[list[str]] = []

    def query(cmd: list[str]) -> tuple[int, str]:
        seen.append(cmd)
        return _registered(cmd)

    checks = doctor.run_checks(query)
    assert _by_name(checks, "codex hooks").status == doctor.OK
    assert "/hooks" in _by_name(checks, "codex hooks").detail
    assert ["claude", "mcp", "get", "kampung"] in seen
    assert ["codex", "mcp", "get", "kampung"] in seen


def test_agent_with_only_an_mcp_registration_is_checked(
    home: Path, server: LiveServer
) -> None:
    def codex_only(cmd: list[str]) -> tuple[int, str]:
        return _registered(cmd) if cmd[0] == "codex" else _unregistered(cmd)

    checks = doctor.run_checks(codex_only)
    assert "claude-code hooks" not in [c.name for c in checks]
    if os.name == "nt":  # Codex on Windows polls: it has no hooks to check
        assert "codex hooks" not in [c.name for c in checks]
    else:
        assert _by_name(checks, "codex hooks").status == doctor.FAIL


def _log(home: Path, *lines: str) -> None:
    d = home / ".cache" / "kampung"
    d.mkdir(parents=True, exist_ok=True)
    (d / "hooks.log").write_text("".join(line + "\n" for line in lines))


def _stamp(hours_ago: float) -> str:
    moment = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return moment.isoformat(timespec="seconds")


def test_recent_hook_failures_warn_with_the_latest(
    home: Path, server: LiveServer
) -> None:
    _log(
        home,
        f"{_stamp(30)} Stop: HookError: too old to count",
        f"{_stamp(2)} channel: HookError: GET /v1/sessions/1/peek: HTTP 502",
        f"{_stamp(1)} Stop: HookError: GET /v1/sessions/1/peek: timed out",
    )
    log = _by_name(_healthy(server, home), "hooks.log")
    assert log.status == doctor.WARN
    assert "2 failure(s)" in log.detail
    assert "timed out" in log.detail
    assert "too old" not in log.detail


def test_expected_and_unreadable_log_lines_do_not_warn(
    home: Path, server: LiveServer
) -> None:
    _log(
        home,
        f"{_stamp(1)} SessionEnd: HookError: no session file for this session",
        "not a log line",
    )
    assert _by_name(_healthy(server, home), "hooks.log").status == doctor.OK


def test_the_key_is_never_printed(
    home: Path,
    server: LiveServer,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(doctor, "default_query", _claude_registered)
    _install_hooks(home)
    cli.main(["doctor"])
    captured = capsys.readouterr()
    assert server.keys["laptop"] not in captured.out + captured.err


def test_cli_exits_0_when_healthy(
    home: Path,
    server: LiveServer,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(doctor, "default_query", _claude_registered)
    _install_hooks(home)
    (home / ".config" / "kampung" / "config.json").chmod(0o600)
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert f"kampung-mcp {__version__}" in out
    assert "FAIL" not in out
    assert "All checks passed." in out


def test_cli_warnings_still_exit_0(
    home: Path,
    server: LiveServer,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(doctor, "default_query", _no_agent_cli)
    _install_hooks(home)
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "WARN" in out and "warning(s)" in out


def test_cli_exits_1_and_prints_the_fix(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(doctor, "default_query", _no_agent_cli)
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "Fix: " in out and "kampung-mcp setup" in out
