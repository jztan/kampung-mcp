"""A lost owner mark: recorded when an owner grant goes, cleared by owner add,
dismissed by owner remove."""

import json
import sqlite3
from importlib import resources
from pathlib import Path

import pytest

from kampung_server import db, oauth_store
from kampung_server.admin import AdminService
from kampung_server.errors import KampungError
from kampung_server.models import Machine
from server_testlib import FakeClock

Machines = dict[str, tuple[Machine, str]]


def _owner_grant(
    conn: sqlite3.Connection, machine_id: int, clock: FakeClock, owner: bool = True
) -> int:
    with db.transaction(conn):
        grant = oauth_store.replace_grant(conn, machine_id, clock.now())
        oauth_store.set_owner(conn, machine_id, owner)
    return grant


def _cleared(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = conn.execute(
        "SELECT actor, detail FROM audit WHERE action = 'owner_cleared' ORDER BY id"
    ).fetchall()
    return [{"actor": r[0], **json.loads(r[1])} for r in rows]


def _laptop(machines: Machines) -> int:
    return machines["laptop"][0].id


def test_reconnect_of_an_owner_grant_is_recorded(
    conn: sqlite3.Connection, machines: Machines, clock: FakeClock
) -> None:
    m = _laptop(machines)
    _owner_grant(conn, m, clock)
    _owner_grant(conn, m, clock, owner=False)  # the reconnect
    lost = oauth_store.owner_lost(conn, m)
    assert lost is not None and lost[1] == "reconnect"
    assert _cleared(conn) == [
        {"actor": "server", "machine": "laptop", "reason": "reconnect"}
    ]


@pytest.mark.parametrize("reason", ["token_reuse", "app_revoke"])
def test_deleting_an_owner_grant_is_recorded(
    conn: sqlite3.Connection, machines: Machines, clock: FakeClock, reason: str
) -> None:
    m = _laptop(machines)
    grant = _owner_grant(conn, m, clock)
    with db.transaction(conn):
        oauth_store.delete_grant(conn, grant, clock.now(), reason)
    lost = oauth_store.owner_lost(conn, m)
    assert lost is not None and lost[1] == reason
    assert [c["reason"] for c in _cleared(conn)] == [reason]


def test_key_rotate_records_the_loss(
    conn: sqlite3.Connection,
    admin: AdminService,
    machines: Machines,
    clock: FakeClock,
) -> None:
    m = _laptop(machines)
    _owner_grant(conn, m, clock)
    admin.rotate_key("laptop", "admin:cli")
    lost = oauth_store.owner_lost(conn, m)
    assert lost is not None and lost[1] == "key_rotate"
    assert [c["reason"] for c in _cleared(conn)] == ["key_rotate"]


@pytest.mark.parametrize("action", ["disconnect", "revoke"])
def test_deliberate_removal_is_not_a_loss(
    conn: sqlite3.Connection,
    admin: AdminService,
    machines: Machines,
    clock: FakeClock,
    action: str,
) -> None:
    # The operator removed the connection or the machine on purpose: the mark
    # goes, but nobody needs telling, and "owner add" could not restore it.
    m = _laptop(machines)
    _owner_grant(conn, m, clock)
    getattr(admin, action)("laptop", "admin:cli")
    assert oauth_store.owner_lost(conn, m) is None and _cleared(conn) == []


def test_a_non_owner_grant_going_records_nothing(
    conn: sqlite3.Connection,
    admin: AdminService,
    machines: Machines,
    clock: FakeClock,
) -> None:
    m = _laptop(machines)
    _owner_grant(conn, m, clock, owner=False)
    _owner_grant(conn, m, clock, owner=False)
    admin.disconnect("laptop", "admin:cli")
    assert oauth_store.owner_lost(conn, m) is None and _cleared(conn) == []


def test_owner_add_clears_and_a_second_loss_overwrites(
    conn: sqlite3.Connection,
    admin: AdminService,
    machines: Machines,
    clock: FakeClock,
) -> None:
    m = _laptop(machines)
    _owner_grant(conn, m, clock)
    _owner_grant(conn, m, clock, owner=False)
    admin.owner_add("laptop", "admin:cli")
    assert oauth_store.owner_lost(conn, m) is None
    grant = oauth_store.grant_for_machine(conn, m)
    assert grant is not None
    with db.transaction(conn):
        oauth_store.delete_grant(conn, grant, clock.now(), "token_reuse")
    lost = oauth_store.owner_lost(conn, m)
    assert lost is not None and lost[1] == "token_reuse"


def test_owner_remove_dismisses_a_lost_mark_without_a_grant(
    conn: sqlite3.Connection,
    admin: AdminService,
    machines: Machines,
    clock: FakeClock,
) -> None:
    m = _laptop(machines)
    _owner_grant(conn, m, clock)
    admin.rotate_key("laptop", "admin:cli")
    admin.owner_remove("laptop", "admin:cli")
    assert oauth_store.owner_lost(conn, m) is None


def test_owner_remove_dismisses_with_a_non_owner_grant(
    conn: sqlite3.Connection,
    admin: AdminService,
    machines: Machines,
    clock: FakeClock,
) -> None:
    m = _laptop(machines)
    _owner_grant(conn, m, clock)
    _owner_grant(conn, m, clock, owner=False)
    admin.owner_remove("laptop", "admin:cli")
    assert oauth_store.owner_lost(conn, m) is None


def test_owner_remove_with_nothing_keeps_its_errors(
    admin: AdminService, machines: Machines
) -> None:
    with pytest.raises(KampungError) as e:
        admin.owner_remove("laptop", "admin:cli")
    assert e.value.code == "no_grant"


def test_words_cover_every_reason() -> None:
    for reason in (
        "reconnect",
        "token_reuse",
        "app_revoke",
        "disconnect",
        "key_rotate",
        "revoke",
    ):
        assert (
            oauth_store.owner_lost_words(reason) == oauth_store.OWNER_LOST_WORDS[reason]
        )
    assert oauth_store.owner_lost_words("odd") == "odd"


def test_migrate_10_to_11_keeps_machines(tmp_path: Path) -> None:
    old = db.connect(tmp_path / "old.db")
    v1 = resources.files("kampung_server").joinpath("schema.sql").read_text()
    for statement in db._statements(v1):
        old.execute(statement)
    for target in range(2, 11):
        for statement in db._statements(db._script(target)):
            old.execute(statement)
    old.execute("PRAGMA user_version=10")
    old.execute(
        "INSERT INTO machines (name, key_hash, created_at) VALUES ('m', x'00', 't')"
    )
    db.migrate(old)
    row = old.execute("SELECT owner_lost_at, owner_lost_reason FROM machines")
    assert tuple(row.fetchone()) == (None, None)
