import io
from typing import Any

import pytest

from kampung_client import cli, setup

SERVER = ["setup", "--server", "https://kampung.example.com"]


@pytest.fixture
def used(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    keys: list[str] = []

    def fake_run_setup(server: str, key: str, *rest: Any) -> list[str]:
        keys.append(key)
        return ["ok"]

    monkeypatch.setattr(setup, "run_setup", fake_run_setup)
    monkeypatch.delenv("KAMPUNG_KEY", raising=False)
    return keys


def test_key_flag_still_works(used: list[str]) -> None:
    assert cli.main([*SERVER, "--key", "kmp_flag"]) == 0
    assert used == ["kmp_flag"]


def test_key_from_stdin(used: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("kmp_stdin\n"))
    assert cli.main([*SERVER, "--key", "-"]) == 0
    assert used == ["kmp_stdin"]


def test_key_from_env(used: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAMPUNG_KEY", " kmp_env ")
    assert cli.main(SERVER) == 0
    assert used == ["kmp_env"]


def test_key_prompted_without_echo(
    used: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    stdin = io.StringIO("")
    monkeypatch.setattr(stdin, "isatty", lambda: True)
    monkeypatch.setattr("sys.stdin", stdin)
    monkeypatch.setattr("getpass.getpass", lambda prompt: "kmp_typed")
    assert cli.main(SERVER) == 0
    assert used == ["kmp_typed"]


def test_no_key_anywhere_fails(
    used: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert cli.main(SERVER) == 1
    assert "KAMPUNG_KEY" in capsys.readouterr().err and used == []
