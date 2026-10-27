import json
import sqlite3

import pytest

from kampung_server.admin import AdminService
from kampung_server.errors import KampungError
from kampung_server.identity import authenticate
from kampung_server.keys import hash_key


def _code(fn: object, *args: object) -> str:
    with pytest.raises(KampungError) as e:
        fn(*args)  # type: ignore[operator]
    return e.value.code


def test_add_machine_returns_key_once(
    admin: AdminService, conn: sqlite3.Connection
) -> None:
    m, key = admin.add_machine("laptop", "admin:cli")
    assert key.startswith("kmp_")
    assert authenticate(conn, key) == m
    stored = conn.execute("SELECT key_hash FROM machines").fetchone()[0]
    assert stored == hash_key(key)
    assert key.encode() not in stored


def test_add_machine_duplicate(admin: AdminService) -> None:
    admin.add_machine("laptop", "admin:cli")
    assert _code(admin.add_machine, "laptop", "admin:cli") == "name_taken"


def test_add_machine_bad_name(admin: AdminService) -> None:
    assert _code(admin.add_machine, "../x", "admin:cli") == "invalid_name"


def test_pair_is_ordered_and_idempotent(
    admin: AdminService, conn: sqlite3.Connection
) -> None:
    admin.add_machine("laptop", "admin:cli")
    admin.add_machine("desktop", "admin:cli")
    admin.pair("desktop", "laptop", "admin:cli")
    admin.pair("desktop", "laptop", "admin:cli")
    rows = conn.execute("SELECT machine_a, machine_b FROM pairings").fetchall()
    assert len(rows) == 1 and rows[0][0] < rows[0][1]
    assert admin.list_pairs() == [("desktop", "laptop")]


def test_pair_self_rejected(admin: AdminService) -> None:
    admin.add_machine("laptop", "admin:cli")
    assert _code(admin.pair, "laptop", "laptop", "admin:cli") == "same_machine"


def test_pair_unknown(admin: AdminService) -> None:
    admin.add_machine("laptop", "admin:cli")
    assert _code(admin.pair, "laptop", "nope", "admin:cli") == "unknown_machine"


def test_unpair(admin: AdminService, machines: dict[str, object]) -> None:
    admin.unpair("laptop", "desktop", "admin:cli")
    admin.unpair("laptop", "desktop", "admin:cli")
    assert admin.list_pairs() == []


def test_revoke_blocks_auth(admin: AdminService, conn: sqlite3.Connection) -> None:
    _, key = admin.add_machine("laptop", "admin:cli")
    admin.revoke("laptop", "admin:cli")
    assert authenticate(conn, key) is None
    assert admin.list_machines()[0].revoked_at is not None


def test_revoke_unknown(admin: AdminService) -> None:
    assert _code(admin.revoke, "nope", "admin:cli") == "unknown_machine"


def test_every_admin_write_is_audited(
    admin: AdminService, conn: sqlite3.Connection
) -> None:
    admin.add_machine("laptop", "admin:cli")
    admin.add_machine("desktop", "admin:cli")
    admin.pair("laptop", "desktop", "admin:web")
    admin.revoke("desktop", "admin:cli")
    rows = conn.execute(
        "SELECT actor, action, detail FROM audit ORDER BY id"
    ).fetchall()
    assert [(r[0], r[1]) for r in rows] == [
        ("admin:cli", "add_machine"),
        ("admin:cli", "add_machine"),
        ("admin:web", "pair"),
        ("admin:cli", "revoke"),
    ]
    assert json.loads(rows[0][2]) == {"machine": "laptop"}
