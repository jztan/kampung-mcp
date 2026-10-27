"""Client test fixtures: an isolated HOME and a live Kampung server."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from client_testlib import LiveServer, start_server, write_config


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    return h


@pytest.fixture
def server(tmp_path: Path, home: Path) -> Iterator[LiveServer]:
    live = start_server(tmp_path)
    write_config(home, live.url, live.keys["laptop"])
    yield live
    live.uvicorn.should_exit = True
