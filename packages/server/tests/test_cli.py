import sqlite3
from pathlib import Path

import pytest

from kampung_server import app, cli, db
from kampung_server.config import Settings


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "data" / "kampung.db"
    path.parent.mkdir()
    monkeypatch.setenv("KAMPUNG_DB_PATH", str(path))
    return path


def test_key_add_prints_key_once(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["key", "add", "laptop"]) == 0
    out = capsys.readouterr().out
    assert "Machine laptop created. Key (shown once): kmp_" in out


def test_key_add_duplicate_exit_1(capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["key", "add", "laptop"])
    assert cli.main(["key", "add", "laptop"]) == 1
    assert capsys.readouterr().err.startswith("error: name_taken: ")


def test_pair_list_unpair(capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["key", "add", "laptop"])
    cli.main(["key", "add", "desktop"])
    capsys.readouterr()
    assert cli.main(["pair", "laptop", "desktop"]) == 0
    assert "Paired laptop and desktop." in capsys.readouterr().out
    assert cli.main(["key", "list"]) == 0
    listing = capsys.readouterr().out.splitlines()
    assert [line.split()[0] for line in listing] == ["desktop", "laptop"]
    assert all(line.split()[-1] == "active" for line in listing)
    assert cli.main(["unpair", "laptop", "desktop"]) == 0


def test_revoke(capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["key", "add", "laptop"])
    assert cli.main(["revoke", "laptop"]) == 0
    capsys.readouterr()
    cli.main(["key", "list"])
    assert capsys.readouterr().out.split()[-1] == "revoked"
    assert cli.main(["revoke", "nope"]) == 1


def test_cli_audits_as_admin_cli(env: Path) -> None:
    cli.main(["key", "add", "laptop"])
    conn = sqlite3.connect(env)
    assert conn.execute("SELECT actor FROM audit").fetchone()[0] == "admin:cli"


def test_serve_warns_when_no_keys(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], env: Path
) -> None:
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        app, "serve", lambda deps, host, port: calls.append((host, port))
    )
    assert cli.main(["serve"]) == 0
    assert "No machine keys yet" in capsys.readouterr().err
    assert calls == [("127.0.0.1", 8000)]
    assert env.exists()


def test_serve_quiet_with_keys(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(app, "serve", lambda deps, host, port: None)
    cli.main(["key", "add", "laptop"])
    capsys.readouterr()
    cli.main(["serve"])
    assert "No machine keys yet" not in capsys.readouterr().err


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAMPUNG_DB_PATH", "/tmp/x.db")
    monkeypatch.setenv("KAMPUNG_HTTP_HOST", "0.0.0.0")
    monkeypatch.setenv("KAMPUNG_HTTP_PORT", "9000")
    assert Settings.from_env() == Settings(Path("/tmp/x.db"), "0.0.0.0", 9000)


def test_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("KAMPUNG_DB_PATH", "KAMPUNG_HTTP_HOST", "KAMPUNG_HTTP_PORT"):
        monkeypatch.delenv(name, raising=False)
    assert Settings.from_env() == Settings(Path("/data/kampung.db"), "127.0.0.1", 8000)


def test_cli_migrates_fresh_db(env: Path) -> None:
    cli.main(["key", "list"])
    conn = db.connect(env)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
