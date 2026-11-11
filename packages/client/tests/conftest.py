"""Client test fixtures: an isolated HOME and a live Kampung server."""

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from client_testlib import LiveServer, set_home, start_server, write_config
from kampung_client import setup


@pytest.fixture(autouse=True)
def real_claude_version(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[], setup.Version | None]:
    """`kampung-mcp setup` asks `claude --version`. No test asks the Claude
    Code of the machine it runs on: setup sees no version unless a test gives
    one. The value is the real call, for the tests of it."""
    real = setup.read_claude_version
    monkeypatch.setattr(setup, "read_claude_version", lambda: None)
    return real


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    set_home(monkeypatch, h)
    return h


@pytest.fixture
def server(tmp_path: Path, home: Path) -> Iterator[LiveServer]:
    live = start_server(tmp_path)
    write_config(home, live.url, live.keys["laptop"])
    yield live
    live.uvicorn.should_exit = True
