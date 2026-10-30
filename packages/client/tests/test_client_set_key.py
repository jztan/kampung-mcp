import io
import json
import stat
import sys
from pathlib import Path

import pytest

from client_testlib import LiveServer
from kampung_client import cli
from kampung_client.setup import SetupError, set_key


def _config(home: Path) -> dict[str, object]:
    path = home / ".config" / "kampung" / "config.json"
    data: dict[str, object] = json.loads(path.read_text())
    return data


def test_set_key_swaps_only_the_key(home: Path, server: LiveServer) -> None:
    cfg = home / ".config" / "kampung" / "config.json"
    cfg.write_text(json.dumps({**_config(home), "allow_insecure_http": True}))
    new = server.admin.rotate_key("laptop", "admin:cli")
    set_key(new)
    assert _config(home) == {
        "server": server.url,
        "key": new,
        "allow_insecure_http": True,
    }
    if sys.platform != "win32":  # chmod only sets the read-only bit there
        assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
    # Hooks and MCP registration are setup's job; set-key leaves them alone.
    assert not (home / ".claude").exists()


def test_set_key_refuses_a_key_the_server_rejects(
    home: Path, server: LiveServer
) -> None:
    with pytest.raises(SetupError, match="rejected this key"):
        set_key("kmp_wrong")
    assert _config(home)["key"] == server.keys["laptop"]


def test_set_key_needs_setup_first(home: Path) -> None:
    with pytest.raises(SetupError, match="setup"):
        set_key("kmp_any")


def test_cli_set_key_reads_stdin(
    home: Path,
    server: LiveServer,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    new = server.admin.rotate_key("laptop", "admin:cli")
    monkeypatch.delenv("KAMPUNG_KEY", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(new + "\n"))
    assert cli.main(["set-key", "--key", "-"]) == 0
    assert _config(home)["key"] == new
    out = capsys.readouterr().out
    assert "Restart" in out and new not in out


def test_cli_set_key_reports_errors(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("kmp_any\n"))
    assert cli.main(["set-key", "--key", "-"]) == 1
    assert capsys.readouterr().err.startswith("error: ")
