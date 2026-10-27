import sqlite3
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from kampung_server import db, sessions
from kampung_server.admin import AdminService
from kampung_server.models import Machine, Session, load_machine


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "kampung.db"


@pytest.fixture
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    c = db.connect(db_path)
    db.migrate(c)
    yield c
    c.close()


class FakeClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 10, 27, 9, 0, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs: float) -> None:
        self._now += timedelta(**kwargs)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def admin(db_path: Path, conn: sqlite3.Connection, clock: FakeClock) -> AdminService:
    return AdminService(lambda: db.connect(db_path), clock.now)


@pytest.fixture
def machines(admin: AdminService) -> dict[str, tuple[Machine, str]]:
    laptop = admin.add_machine("laptop", "admin:cli")
    desktop = admin.add_machine("desktop", "admin:cli")
    admin.pair("laptop", "desktop", "admin:cli")
    return {"laptop": laptop, "desktop": desktop}


MakeSession = Callable[..., Session]


@pytest.fixture
def make_session(
    conn: sqlite3.Connection,
    machines: dict[str, tuple[Machine, str]],
    clock: FakeClock,
) -> MakeSession:
    def make(machine_name: str, repo: str = "api", **kw: Any) -> Session:
        machine = load_machine(conn, machine_name)
        assert machine is not None
        args: dict[str, Any] = {
            "agent": "claude-code",
            "cwd": f"/home/u/{repo}",
            "repo": repo,
            "hooks": True,
        }
        args.update(kw)
        return sessions.register(conn, machine, clock.now(), **args)

    return make
