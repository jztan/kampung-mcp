import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from kampung_server import db


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "kampung.db"


@pytest.fixture
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    c = db.connect(db_path)
    db.migrate(c)
    yield c
    c.close()
