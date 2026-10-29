import sqlite3
from pathlib import Path

from kampung_server import db
from kampung_server.admin import AdminService
from kampung_server.keyindex import KeyIndex
from kampung_server.models import Machine

Machines = dict[str, tuple[Machine, str]]


class Counting:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.calls = 0
        self.now = 0.0

    def connect(self) -> sqlite3.Connection:
        self.calls += 1
        return db.connect(self.db_path)

    def monotonic(self) -> float:
        return self.now


def test_wrong_keys_skip_database_while_fresh(
    db_path: Path, machines: Machines
) -> None:
    c = Counting(db_path)
    index = KeyIndex(c.connect, c.monotonic)
    assert index.lookup("kmp_wrong") is None
    calls = c.calls
    for i in range(100):
        assert index.is_unknown(f"kmp_wrong{i}")
    assert c.calls == calls
    assert not index.is_unknown(machines["laptop"][1])


def test_valid_key_looks_up_machine(db_path: Path, machines: Machines) -> None:
    c = Counting(db_path)
    index = KeyIndex(c.connect, c.monotonic)
    m = index.lookup(machines["laptop"][1])
    assert m is not None and m.name == "laptop"


def test_new_key_seen_after_reload(
    db_path: Path, machines: Machines, admin: AdminService
) -> None:
    c = Counting(db_path)
    index = KeyIndex(c.connect, c.monotonic)
    index.lookup("kmp_wrong")
    _, key = admin.add_machine("tablet", "admin:cli")
    assert index.is_unknown(key)
    c.now += 1.5
    assert not index.is_unknown(key)
    m = index.lookup(key)
    assert m is not None and m.name == "tablet"


def test_revoked_key_rejected_at_once(
    db_path: Path, machines: Machines, admin: AdminService
) -> None:
    c = Counting(db_path)
    index = KeyIndex(c.connect, c.monotonic)
    key = machines["laptop"][1]
    assert index.lookup(key) is not None
    admin.revoke("laptop", "admin:cli")
    assert index.lookup(key) is None
