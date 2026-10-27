import sqlite3

from kampung_server.identity import authenticate, is_paired
from kampung_server.models import Machine


def test_authenticate_wrong_key(
    conn: sqlite3.Connection, machines: dict[str, tuple[Machine, str]]
) -> None:
    assert authenticate(conn, "kmp_wrong") is None
    assert authenticate(conn, machines["laptop"][1]) == machines["laptop"][0]


def test_is_paired_self_and_pairs(
    conn: sqlite3.Connection, machines: dict[str, tuple[Machine, str]], admin: object
) -> None:
    laptop, desktop = machines["laptop"][0], machines["desktop"][0]
    assert is_paired(conn, laptop.id, laptop.id)
    assert is_paired(conn, laptop.id, desktop.id)
    assert is_paired(conn, desktop.id, laptop.id)
    admin.unpair("laptop", "desktop", "admin:cli")  # type: ignore[attr-defined]
    assert not is_paired(conn, laptop.id, desktop.id)
