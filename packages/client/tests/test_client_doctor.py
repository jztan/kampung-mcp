import json
import os
import shutil
import socket
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from client_testlib import LiveServer, scripted_server, write_config
from kampung_client import __version__, cli, doctor, hook, setup


def _registered(cmd: list[str]) -> tuple[int, str]:
    return 0, "kampung:\n  Status: Connected\n"


def _claude_registered(cmd: list[str]) -> tuple[int, str]:
    if cmd == ["claude", "--version"]:
        return 0, "2.1.322 (Claude Code)"
    return _registered(cmd) if cmd[0] == "claude" else _unregistered(cmd)


def _unregistered(cmd: list[str]) -> tuple[int, str]:
    return 1, "No MCP server found with name: kampung\n"


def _no_agent_cli(cmd: list[str]) -> tuple[int, str]:
    raise FileNotFoundError(cmd[0])


def _install_hooks(
    home: Path, command: str | None = None, idle_wake: bool = True
) -> Path:
    """What setup writes for Claude Code, or `command` on the five events."""
    items = setup.claude_hook_items(idle_wake, setup._windows())
    if command is not None:
        item = {"type": "command", "command": command, "timeout": 5}
        items = {event: [item] for event in setup.EVENTS}
    setup._merge_hooks(setup._claude_settings_path(), {}, items)
    return setup._claude_settings_path()


def _by_name(checks: list[doctor.Check], name: str) -> doctor.Check:
    return next(c for c in checks if c.name == name)


def _set_denied(home: Path, entries: list[dict[str, str]]) -> None:
    path = setup._claude_settings_path()
    data = json.loads(path.read_text())
    data["deniedMcpServers"] = entries
    path.write_text(json.dumps(data))


def _healthy(
    server: LiveServer, home: Path, query: doctor.Query = _claude_registered
) -> list[doctor.Check]:
    _install_hooks(home)
    _set_denied(home, [{"serverUrl": setup.connector_url_pattern(server.url)}])
    (home / ".config" / "kampung" / "config.json").chmod(0o600)
    return doctor.run_checks(query)


def test_an_undenied_connector_warns(home: Path, server: LiveServer) -> None:
    _healthy(server, home)
    _set_denied(home, [])
    check = _by_name(doctor.run_checks(_claude_registered), "claude-code connector")
    assert check.status == doctor.WARN
    assert "phone's mail" in check.detail
    assert "kampung-mcp setup" in check.fix


def test_a_connector_denied_only_by_name_warns(home: Path, server: LiveServer) -> None:
    _healthy(server, home)
    _set_denied(home, [{"serverName": "claude.ai kampung"}])
    check = _by_name(doctor.run_checks(_claude_registered), "claude-code connector")
    assert check.status == doctor.WARN
    assert "another name" in check.detail


@pytest.mark.parametrize(
    "settings",
    [
        {"deniedMcpServers": [{"serverUrl": "http://127.0.0.1:*/*"}]},
        {"disableClaudeAiConnectors": True},
    ],
)
def test_a_wider_deny_or_no_connectors_passes(
    home: Path, server: LiveServer, settings: dict[str, object]
) -> None:
    _healthy(server, home)
    path = setup._claude_settings_path()
    data = json.loads(path.read_text())
    data.pop("deniedMcpServers")
    data.update(settings)
    path.write_text(json.dumps(data))
    check = _by_name(doctor.run_checks(_claude_registered), "claude-code connector")
    assert check.status == doctor.OK


def test_codex_only_machine_skips_the_connector_check(
    home: Path, server: LiveServer
) -> None:
    def codex_only(cmd: list[str]) -> tuple[int, str]:
        return _registered(cmd) if cmd[0] == "codex" else _unregistered(cmd)

    checks = doctor.run_checks(codex_only)
    assert "claude-code connector" not in [c.name for c in checks]


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
        "idle wake",
        "claude-code mcp",
        "claude-code connector",
        "sessions",
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
        ("0.1.0", doctor.OK),
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


def _registered_as(python: str, probe: tuple[int, str] | Exception) -> doctor.Query:
    """claude has kampung registered with this interpreter; running it gives
    probe. codex has nothing registered."""

    def query(cmd: list[str]) -> tuple[int, str]:
        if cmd[0] == "claude":
            return 0, (
                f"kampung:\n  Status: ✘ Failed to connect\n  Type: stdio\n"
                f"  Command: {python}\n"
                "  Args: -m kampung_client adapter --agent claude-code\n"
            )
        if cmd[0] == python:
            if isinstance(probe, Exception):
                raise probe
            return probe
        return _unregistered(cmd)

    return query


PIL_MISSING = (
    1,
    "Traceback (most recent call last):\n"
    '  File "images.py", line 9, in <module>\n'
    "ModuleNotFoundError: No module named 'PIL'\n",
)


def test_checkout_interpreter_missing_a_module_says_uv_sync(
    home: Path, server: LiveServer, tmp_path: Path
) -> None:
    checkout = tmp_path / "src" / "kampung mcp"
    python = str(checkout / ".venv" / "bin" / "python")
    mcp = _by_name(
        _healthy(server, home, _registered_as(python, PIL_MISSING)), "claude-code mcp"
    )
    assert mcp.status == doctor.FAIL
    assert "No module named 'PIL'" in mcp.detail and python in mcp.detail
    assert "uv sync" in mcp.fix and str(checkout) in mcp.fix


def test_installed_interpreter_missing_a_module_says_upgrade(
    home: Path, server: LiveServer, tmp_path: Path
) -> None:
    python = str(tmp_path / "tools" / "kampung-mcp" / "bin" / "python3")
    mcp = _by_name(
        _healthy(server, home, _registered_as(python, PIL_MISSING)), "claude-code mcp"
    )
    assert mcp.status == doctor.FAIL
    assert "uv tool upgrade kampung-mcp" in mcp.fix


def test_gone_registered_interpreter_fails(
    home: Path, server: LiveServer, tmp_path: Path
) -> None:
    python = str(tmp_path / "gone" / "python")
    query = _registered_as(python, FileNotFoundError(python))
    mcp = _by_name(_healthy(server, home, query), "claude-code mcp")
    assert mcp.status == doctor.FAIL
    assert "does not exist" in mcp.detail


def test_registered_interpreter_on_another_version_warns(
    home: Path, server: LiveServer, tmp_path: Path
) -> None:
    python = str(tmp_path / "old" / "python")

    def query(cmd: list[str]) -> tuple[int, str]:
        if cmd[0] == "claude":
            return 0, f"kampung:\n  Status: Connected\n  Command: {python}\n"
        if cmd[0] == python:
            return 0, "0.2.1\n"
        return _unregistered(cmd)

    mcp = _by_name(_healthy(server, home, query), "claude-code mcp")
    assert mcp.status == doctor.WARN
    assert "0.2.1" in mcp.detail and __version__ in mcp.detail


def test_registered_interpreter_that_imports_the_adapter_passes(
    home: Path, server: LiveServer, tmp_path: Path
) -> None:
    python = str(tmp_path / "ok" / "python")

    def query(cmd: list[str]) -> tuple[int, str]:
        if cmd[0] == "claude":
            return 0, f"kampung:\n  Status: Connected\n  Command: {python}\n"
        if cmd[0] == python:
            assert cmd[1] == "-c" and "kampung_client.adapter" in cmd[2]
            return 0, f"{__version__}\n"
        return _unregistered(cmd)

    mcp = _by_name(_healthy(server, home, query), "claude-code mcp")
    assert mcp.status == doctor.OK


def test_real_interpreter_probe_runs(home: Path, server: LiveServer) -> None:
    """The probe against this test run's own interpreter, through the real
    default_query: the adapter imports and the version matches."""

    def query(cmd: list[str]) -> tuple[int, str]:
        if cmd[0] == "claude":
            return 0, f"kampung:\n  Status: Connected\n  Command: {sys.executable}\n"
        if cmd[0] == sys.executable:
            return doctor.default_query(cmd)
        return _unregistered(cmd)

    mcp = _by_name(_healthy(server, home, query), "claude-code mcp")
    assert mcp.status == doctor.OK, mcp.detail


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


def _session_file(
    sid: str,
    mailbox: str,
    background: bool,
    pid: int,
    agent: str = "claude-code",
    hooks: bool = True,
) -> None:
    start = hook._lookup(pid)
    assert start is not None
    hook.write_session_file(
        sid,
        {
            "session": "1",
            "suffix": sid[-2:],
            "mailbox": mailbox,
            "hooks": hooks,
            "agent": agent,
            "agent_session_id": sid,
            "background": background,
            "ancestors": [[pid, start[1]]],
        },
    )


def test_sessions_lists_open_windows_and_background_sessions(
    home: Path, server: LiveServer
) -> None:
    _session_file("cc-aa", "laptop/api", False, os.getpid())
    _session_file("cc-bb", "laptop/api", True, os.getpid())
    # The same pid with another start time: a process that has since ended.
    hook.write_session_file(
        "cc-cc",
        {
            "session": "2",
            "suffix": "cc",
            "mailbox": "laptop/blog",
            "hooks": True,
            "background": True,
            "ancestors": [[os.getpid(), "not its start time"]],
        },
    )
    check = _by_name(_healthy(server, home), "sessions")
    assert check.status == doctor.OK
    assert check.detail.startswith("1 window(s), 1 background")
    assert "laptop/api#bb" in check.detail and "laptop/blog" not in check.detail
    assert "Channels" in check.detail


def test_no_open_sessions_is_fine(home: Path, server: LiveServer) -> None:
    check = _by_name(_healthy(server, home), "sessions")
    assert check.status == doctor.OK and check.detail == "no open sessions"


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
    _set_denied(home, [{"serverUrl": setup.connector_url_pattern(server.url)}])
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


def test_try_hook_reads_the_powershell_form() -> None:
    command = setup.powershell_hook_command(hook.__file__)
    assert doctor._try_hook(command) == (doctor.OK, "")


def test_try_hook_finds_a_missing_script_in_the_powershell_form(
    tmp_path: Path,
) -> None:
    gone = tmp_path / "gone" / "hook.py"
    status, detail = doctor._try_hook(setup.powershell_hook_command(str(gone)))
    assert status == doctor.FAIL
    assert "gone" in detail


@pytest.mark.skipif(sys.platform == "win32", reason="no launch binding on Windows")
def test_codex_launch_check(
    home: Path, server: LiveServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_client_codex_launch import FakeDaemon

    _install_hooks(home)
    setup._merge_codex_hooks({})
    checks = doctor.run_checks(_registered)
    assert _by_name(checks, "codex launch").status == doctor.OK
    assert "not running" in _by_name(checks, "codex launch").detail
    daemon = FakeDaemon(home, [])
    try:
        checks = doctor.run_checks(_registered)
        assert _by_name(checks, "codex launch").status == doctor.OK
        assert "before their first prompt" in _by_name(checks, "codex launch").detail
    finally:
        daemon.close()
    # Codex exited and left its socket file behind: not running, no warning.
    stale_dir = tempfile.mkdtemp(prefix="kcs-", dir="/tmp")
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(os.path.join(stale_dir, "s"))
    stale.close()
    link = home / ".codex" / "app-server-control" / "app-server-control.sock"
    link.unlink()
    link.symlink_to(os.path.join(stale_dir, "s"))
    checks = doctor.run_checks(_registered)
    assert _by_name(checks, "codex launch").status == doctor.OK
    assert "not running" in _by_name(checks, "codex launch").detail
    shutil.rmtree(stale_dir, ignore_errors=True)
    # A socket path that exists but refuses connections: a broken daemon.
    link = home / ".codex" / "app-server-control" / "app-server-control.sock"
    link.unlink()
    link.write_text("not a socket")
    checks = doctor.run_checks(_registered)
    launch = _by_name(checks, "codex launch")
    assert launch.status == doctor.WARN and "first prompt" in launch.detail


@pytest.mark.skipif(sys.platform == "win32", reason="no launch binding on Windows")
def test_codex_launch_check_survives_an_unreadable_socket_folder(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A sandbox or odd permissions make the stat itself fail: one warning,
    # never a crash that hides every other check.
    locked = home / "locked"
    locked.mkdir()
    monkeypatch.setattr(
        doctor.codex_launch, "control_socket", lambda: locked / "control.sock"
    )
    locked.chmod(0)
    try:
        check = doctor._codex_launch_check()
    finally:
        locked.chmod(0o700)
    assert check.status == doctor.WARN


@pytest.mark.parametrize(
    "status_line", ["Status: ✘ Failed to connect", "enabled: false"]
)
def test_a_broken_registration_still_fails_when_the_version_differs(
    home: Path, server: LiveServer, tmp_path: Path, status_line: str
) -> None:
    python = str(tmp_path / "old" / "python")

    def query(cmd: list[str]) -> tuple[int, str]:
        if cmd[0] == "claude":
            return 0, f"kampung:\n  {status_line}\n  Command: {python}\n"
        if cmd[0] == python:
            return 0, "0.2.1\n"
        return _unregistered(cmd)

    mcp = _by_name(_healthy(server, home, query), "claude-code mcp")
    assert mcp.status == doctor.FAIL
    assert "not working" in mcp.detail and "0.2.1" in mcp.detail


@pytest.mark.parametrize(
    "pattern, denied",
    [
        ("https://mail.example.com/MCP", False),  # paths keep their case
        ("https://mail.example.com/mcp", True),
        ("HTTPS://Mail.Example.COM/*", True),  # scheme and host do not
        ("https://mail.example.com", True),  # no path: any path
        ("https://*.example.com/*", True),
        ("https://other.example.com/*", False),
    ],
)
def test_url_deny_matching_follows_claude_code(pattern: str, denied: bool) -> None:
    assert doctor._url_denied(pattern, "https://mail.example.com/mcp") is denied


# Idle wake ----------------------------------------------------------------

IDLE_RESTART = (
    "Idle wake needs the Claude Code terminal CLI (not the VS Code extension or "
    "the desktop app). Restart the session; see hooks.log."
)


def _claude_says(answer: tuple[int, str] | OSError) -> doctor.Query:
    """Claude Code set up, with this answer to `claude --version`."""

    def query(cmd: list[str]) -> tuple[int, str]:
        if cmd == ["claude", "--version"]:
            if isinstance(answer, OSError):
                raise answer
            return answer
        return _claude_registered(cmd)

    return query


def _claude_at(version: str) -> doctor.Query:
    return _claude_says((0, f"{version} (Claude Code)"))


UNREADABLE = [
    (1, "2.1.322 (Claude Code)"),
    (0, "no version here"),
    FileNotFoundError("claude"),
    TimeoutError("claude --version"),
]


def _wake(sid: str, acked: bool, age: float = 0.0) -> None:
    """A wake sent to the session `age` seconds ago, answered or not."""
    hook.ensure_wake_file(sid)
    assert hook.write_wake_token(sid)
    if acked:
        hook.write_ack(sid)
    at = time.time() - age
    os.utime(hook.wake_path(sid), (at, at))


def _idle_wake(query: doctor.Query) -> doctor.Check:
    return _by_name(doctor.run_checks(query), "idle wake")


def test_idle_wake_comes_right_after_the_hooks_check(
    home: Path, server: LiveServer
) -> None:
    names = [c.name for c in _healthy(server, home)]
    assert names.index("idle wake") == names.index("claude-code hooks") + 1


def test_idle_wake_off_on_old_claude_code(home: Path, server: LiveServer) -> None:
    _install_hooks(home, idle_wake=False)
    assert _idle_wake(_claude_at("2.1.320")) == doctor.Check(
        doctor.WARN,
        "idle wake",
        "off: Claude Code 2.1.320 is older than 2.1.321",
        "Upgrade Claude Code, then run kampung-mcp setup again.",
    )


@pytest.mark.parametrize("version", ["2.1.321", "2.1.322", "3.0.0"])
def test_idle_wake_off_when_not_installed(
    home: Path, server: LiveServer, version: str
) -> None:
    _install_hooks(home, idle_wake=False)
    assert _idle_wake(_claude_at(version)) == doctor.Check(
        doctor.WARN,
        "idle wake",
        "off: the idle wake hooks are not installed",
        "Run kampung-mcp setup again.",
    )


@pytest.mark.parametrize("answer", UNREADABLE, ids=repr)
def test_idle_wake_off_when_the_version_is_unreadable(
    home: Path, server: LiveServer, answer: tuple[int, str] | OSError
) -> None:
    _install_hooks(home, idle_wake=False)
    assert _idle_wake(_claude_says(answer)) == doctor.Check(
        doctor.WARN,
        "idle wake",
        "off: could not read the Claude Code version",
        "Run kampung-mcp setup again once claude --version works.",
    )


def test_idle_wake_installed_on_old_claude_code_warns(
    home: Path, server: LiveServer
) -> None:
    _install_hooks(home)
    assert _idle_wake(_claude_at("2.1.320")) == doctor.Check(
        doctor.WARN,
        "idle wake",
        "Claude Code 2.1.320 is older than 2.1.321; idle wake may not work",
        "Upgrade Claude Code.",
    )


@pytest.mark.parametrize(
    "query",
    [_claude_at("2.1.321"), _claude_at("2.2.0"), _claude_says((1, ""))],
    ids=["floor", "newer", "unreadable"],
)
def test_idle_wake_on_counts_confirmed_sessions(
    home: Path, server: LiveServer, query: doctor.Query
) -> None:
    _install_hooks(home)
    _session_file("cc-aa", "laptop/api", False, os.getpid())
    _wake("cc-aa", acked=True, age=60)
    _session_file("cc-bb", "laptop/api", True, os.getpid())
    hook.ensure_wake_file("cc-bb")  # no wake sent yet
    _session_file("cc-dd", "laptop/blog", False, os.getpid())  # no wake file
    # Not counted: Codex, a session without hooks, and a process that ended.
    _session_file("cx-ee", "laptop/api", False, os.getpid(), agent="codex")
    _wake("cx-ee", acked=False, age=60)
    _session_file("cc-ff", "laptop/api", False, os.getpid(), hooks=False)
    _wake("cc-ff", acked=False, age=60)
    hook.write_session_file(
        "cc-gg",
        {
            "session": "9",
            "suffix": "gg",
            "mailbox": "laptop/old",
            "hooks": True,
            "agent": "claude-code",
            "agent_session_id": "cc-gg",
            "background": False,
            "ancestors": [[os.getpid(), "not its start time"]],
        },
    )
    _wake("cc-gg", acked=False, age=60)
    assert _idle_wake(query) == doctor.Check(
        doctor.OK, "idle wake", "on; 3 open session(s), 1 with a confirmed wake"
    )


def test_idle_wake_on_with_no_open_sessions(home: Path, server: LiveServer) -> None:
    _install_hooks(home)
    assert _idle_wake(_claude_at("2.1.322")) == doctor.Check(
        doctor.OK, "idle wake", "on; 0 open session(s), 0 with a confirmed wake"
    )


def test_idle_wake_warns_for_a_session_whose_last_wake_was_missed(
    home: Path, server: LiveServer
) -> None:
    _install_hooks(home)
    _session_file("cc-aa", "laptop/api", False, os.getpid())
    _wake("cc-aa", acked=False, age=60)
    _session_file("cc-bb", "laptop/api", False, os.getpid())
    _wake("cc-bb", acked=True, age=60)
    assert _idle_wake(_claude_at("2.1.322")) == doctor.Check(
        doctor.WARN,
        "idle wake",
        "laptop/api#aa: the last wake was not confirmed",
        IDLE_RESTART,
    )


def test_idle_wake_names_every_session_that_missed_its_wake(
    home: Path, server: LiveServer
) -> None:
    _install_hooks(home)
    _session_file("cc-aa", "laptop/api", False, os.getpid())
    _wake("cc-aa", acked=False, age=60)
    # An earlier wake was answered, the latest one was not.
    _session_file("cc-bb", "laptop/blog", True, os.getpid())
    _wake("cc-bb", acked=True, age=600)
    _wake("cc-bb", acked=False, age=60)
    check = _idle_wake(_claude_says((1, "")))
    assert check.status == doctor.WARN
    assert check.detail == (
        "laptop/api#aa, laptop/blog#bb: the last wake was not confirmed"
    )
    assert check.fix == IDLE_RESTART


def test_a_wake_in_flight_is_not_a_miss(home: Path, server: LiveServer) -> None:
    _install_hooks(home)
    _session_file("cc-aa", "laptop/api", False, os.getpid())
    _wake("cc-aa", acked=False, age=doctor.WAKE_SETTLE - 5)
    assert _idle_wake(_claude_at("2.1.322")) == doctor.Check(
        doctor.OK, "idle wake", "on; 1 open session(s), 0 with a confirmed wake"
    )
    _wake("cc-aa", acked=False, age=doctor.WAKE_SETTLE + 5)
    assert _idle_wake(_claude_at("2.1.322")).status == doctor.WARN


def test_the_wake_settle_time_is_15_seconds() -> None:
    assert doctor.WAKE_SETTLE == 15.0


def test_idle_wake_reads_a_session_file_without_an_id(
    home: Path, server: LiveServer
) -> None:
    _install_hooks(home)
    hook.write_session_file(
        "cc-aa",
        {
            "session": "1",
            "suffix": "aa",
            "mailbox": "laptop/api",
            "hooks": True,
            "agent": "claude-code",
            "agent_session_id": None,
            "ancestors": [],
        },
    )
    assert _idle_wake(_claude_at("2.1.322")) == doctor.Check(
        doctor.OK, "idle wake", "on; 1 open session(s), 0 with a confirmed wake"
    )


def _drop(event: str, keep: Callable[[dict[str, Any]], bool]) -> Callable[..., None]:
    def change(hooks: dict[str, Any]) -> None:
        entry = hooks[event][0]
        entry["hooks"] = [item for item in entry["hooks"] if keep(item)]

    return change


def _plain_session_start(hooks: dict[str, Any]) -> None:
    item = hooks["SessionStart"][0]["hooks"][0]
    item["command"] = item["command"].replace(" --idle-wake", "")


@pytest.mark.parametrize(
    "change",
    [
        _plain_session_start,
        lambda hooks: hooks.pop("FileChanged"),
        lambda hooks: hooks.pop("CwdChanged"),
        _drop("FileChanged", lambda item: "asyncRewake" not in item),
        _drop("FileChanged", lambda item: "asyncRewake" in item),
        _drop("CwdChanged", lambda item: False),
    ],
    ids=[
        "SessionStart without the flag",
        "no FileChanged",
        "no CwdChanged",
        "no wake hook",
        "no plain FileChanged hook",
        "CwdChanged emptied",
    ],
)
def test_idle_wake_is_off_when_part_of_it_is_missing(
    home: Path, server: LiveServer, change: Callable[[dict[str, Any]], object]
) -> None:
    path = _install_hooks(home)
    data = json.loads(path.read_text())
    change(data["hooks"])
    path.write_text(json.dumps(data))
    check = _idle_wake(_claude_at("2.1.322"))
    assert check.status == doctor.WARN
    assert check.detail == "off: the idle wake hooks are not installed"


def test_idle_wake_is_on_when_the_hooks_come_from_another_install(
    home: Path, server: LiveServer, tmp_path: Path
) -> None:
    """A doctor run from a checkout, with hooks that a tool install wrote."""
    other = tmp_path / "tool" / "kampung_client" / "hook.py"
    other.parent.mkdir(parents=True)
    other.write_text("")
    path = _install_hooks(home)
    # In the file a Windows path has every backslash doubled, so swap the
    # JSON spelling of the path, and make sure something was swapped.
    ours, theirs = json.dumps(hook.__file__)[1:-1], json.dumps(str(other))[1:-1]
    before = path.read_text()
    assert ours in before
    path.write_text(before.replace(ours, theirs))
    checks = doctor.run_checks(_claude_at("2.1.322"))
    assert _by_name(checks, "claude-code hooks").status == doctor.WARN
    assert _by_name(checks, "idle wake") == doctor.Check(
        doctor.OK, "idle wake", "on; 0 open session(s), 0 with a confirmed wake"
    )


def test_idle_wake_ignores_other_tools_hooks_on_its_events(
    home: Path, server: LiveServer
) -> None:
    path = _install_hooks(home, idle_wake=False)
    data = json.loads(path.read_text())
    other = {"type": "command", "command": "direnv reload --wake", "asyncRewake": True}
    data["hooks"]["FileChanged"] = [{"matcher": ".envrc", "hooks": [other]}, "junk"]
    data["hooks"]["CwdChanged"] = [{"hooks": [other]}, {"hooks": "junk"}]
    path.write_text(json.dumps(data))
    assert _idle_wake(_claude_at("2.1.322")).detail == (
        "off: the idle wake hooks are not installed"
    )


def test_codex_only_machine_has_no_idle_wake_check(
    home: Path, server: LiveServer
) -> None:
    def codex_only(cmd: list[str]) -> tuple[int, str]:
        return _registered(cmd) if cmd[0] == "codex" else _unregistered(cmd)

    setup._merge_codex_hooks({})
    names = [c.name for c in doctor.run_checks(codex_only)]
    assert "codex mcp" in names
    assert "idle wake" not in names


def test_no_idle_wake_check_without_claude_code_hooks(
    home: Path, server: LiveServer
) -> None:
    """Registered with Claude Code but no hooks of ours: the hooks check says so."""
    checks = doctor.run_checks(_claude_registered)
    assert _by_name(checks, "claude-code hooks").status == doctor.FAIL
    assert "idle wake" not in [c.name for c in checks]


@pytest.mark.parametrize("windows", [False, True])
def test_the_hooks_check_runs_a_command_with_flags(
    home: Path, server: LiveServer, monkeypatch: pytest.MonkeyPatch, windows: bool
) -> None:
    monkeypatch.setattr(setup, "_windows", lambda: windows)
    path = _install_hooks(home)
    start = json.loads(path.read_text())["hooks"]["SessionStart"][0]["hooks"][0]
    assert " --idle-wake" in start["command"]
    assert ("shell" in start) is windows
    checks = doctor.run_checks(_claude_registered)
    assert _by_name(checks, "claude-code hooks") == doctor.Check(
        doctor.OK, "claude-code hooks", "all 5 events run"
    )
    assert _by_name(checks, "idle wake").status == doctor.OK


def _flag_checker(tmp_path: Path) -> Path:
    """A stand-in hook that fails unless it is run with exactly --idle-wake."""
    script = tmp_path / "it's here" / "kampung_client" / "hook.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        "import sys\nraise SystemExit(0 if sys.argv[1:] == ['--idle-wake'] else 3)\n"
    )
    return script


@pytest.mark.parametrize(
    "command", [setup.hook_command, setup.powershell_hook_command], ids=["sh", "ps"]
)
def test_try_hook_passes_the_flags_on(
    tmp_path: Path, command: Callable[..., str]
) -> None:
    script = _flag_checker(tmp_path)
    # WARN, not FAIL: it ran and exited 0, and it is not this install's hook.
    status, detail = doctor._try_hook(command(str(script), "--idle-wake"))
    assert (status, "hooks run" in detail) == (doctor.WARN, True)
    status, detail = doctor._try_hook(command(str(script)))
    assert (status, detail) == (doctor.FAIL, "the hook failed with exit 3")


def test_try_hook_does_not_read_the_wake_command_as_the_powershell_form() -> None:
    """Only the plain form is run by doctor; the wake wrapper must not match."""
    command = setup.powershell_wake_hook_command(hook.__file__)
    assert doctor.POWERSHELL_HOOK.fullmatch(command) is None


def test_background_sessions_note_says_idle_wake_reaches_them(
    home: Path, server: LiveServer
) -> None:
    _session_file("cc-bb", "laptop/api", True, os.getpid())
    check = _by_name(_healthy(server, home), "sessions")
    assert check.detail == (
        "0 window(s), 1 background (laptop/api#bb: background sessions get no "
        "Channels push and leave mailbox mail to a window there; with idle wake "
        "on they wake like any other)"
    )


def test_codex_hooks_are_checked_on_windows_without_the_launch_check(
    home: Path, server: LiveServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Setup writes Codex hooks on Windows too (PowerShell form); the launch
    # binding needs the daemon's Unix socket, which Windows does not have.
    monkeypatch.setattr(setup, "_windows", lambda: True)
    setup._merge_codex_hooks({})
    checks = doctor.run_checks(
        lambda cmd: _registered(cmd) if cmd[0] == "codex" else _unregistered(cmd)
    )
    assert _by_name(checks, "codex hooks").status == doctor.OK
    assert "codex launch" not in [c.name for c in checks]
