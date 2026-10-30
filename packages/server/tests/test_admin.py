import json
import sqlite3

import pytest

from kampung_server.admin import AdminService
from kampung_server.errors import KampungError
from kampung_server.identity import authenticate
from kampung_server.keys import hash_key
from kampung_server.models import Machine


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


def test_pair_revoked_rejected(admin: AdminService) -> None:
    admin.add_machine("laptop", "admin:cli")
    admin.add_machine("desktop", "admin:cli")
    admin.revoke("desktop", "admin:cli")
    assert _code(admin.pair, "laptop", "desktop", "admin:cli") == "already_revoked"
    assert admin.list_pairs() == []


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


# Session and message actions (Task 8)

from server_testlib import FakeClock, MakeSession  # noqa: E402
from kampung_server.mail import inbox, send  # noqa: E402
from kampung_server.models import load_session  # noqa: E402


def test_end_session_falls_back_targeted_mail(
    conn: sqlite3.Connection,
    admin: AdminService,
    make_session: MakeSession,
    clock: FakeClock,
) -> None:
    sender = make_session("desktop", "blog")
    s1, s2 = make_session("laptop", "api"), make_session("laptop", "api")
    send(conn, sender, s1.address, "for s1", None, clock.now())
    admin.end_session(s1.id, "admin:web")
    assert inbox(conn, s2, 20, clock.now()).messages[0]["body"] == "for s1"
    assert _code(admin.end_session, 9999, "admin:web") == "unknown_session"


def test_delete_message(
    conn: sqlite3.Connection,
    admin: AdminService,
    make_session: MakeSession,
    clock: FakeClock,
) -> None:
    a, b = make_session("laptop", "api"), make_session("desktop", "blog")
    first = send(conn, a, "desktop/blog", "oops", None, clock.now())
    reply = send(conn, b, "laptop/api", "re", first.id, clock.now())
    admin.delete_message(first.id, "admin:web")
    assert inbox(conn, b, 20, clock.now()).messages == []
    got = inbox(conn, a, 20, clock.now()).messages
    assert got[0]["id"] == reply.id and got[0]["reply_to"] is None
    row = conn.execute("SELECT detail FROM audit WHERE action = 'delete_message'")
    assert first.id in row.fetchone()[0]
    assert _code(admin.delete_message, "msg_nope", "admin:web") == "unknown_message"


def test_delete_delivered_message_rejected(
    conn: sqlite3.Connection,
    admin: AdminService,
    make_session: MakeSession,
    clock: FakeClock,
) -> None:
    a, b = make_session("laptop", "api"), make_session("desktop", "blog")
    m = send(conn, a, "desktop/blog", "hi", None, clock.now())
    inbox(conn, b, 20, clock.now())
    assert _code(admin.delete_message, m.id, "admin:web") == "already_delivered"


def test_reset_depth(
    conn: sqlite3.Connection, admin: AdminService, make_session: MakeSession
) -> None:
    s = make_session("laptop", "api")
    conn.execute("UPDATE sessions SET max_depth_seen = 4 WHERE id = ?", (s.id,))
    admin.reset_depth(s.id, "admin:web")
    reloaded = load_session(conn, s.id)
    assert reloaded is not None and reloaded.max_depth_seen == -1
    assert _code(admin.reset_depth, 9999, "admin:web") == "unknown_session"


def test_repeat_revoke_is_an_error(
    admin: AdminService, conn: sqlite3.Connection
) -> None:
    admin.add_machine("box", "admin:cli")
    admin.revoke("box", "admin:cli")
    with pytest.raises(KampungError) as e:
        admin.revoke("box", "admin:web")
    assert e.value.code == "already_revoked"
    revokes = conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'revoke'")
    assert revokes.fetchone()[0] == 1


def test_ended_session_cannot_be_ended_or_reset(
    admin: AdminService, conn: sqlite3.Connection, make_session: MakeSession
) -> None:
    s = make_session("laptop")
    admin.end_session(s.id, "admin:web")
    for act in (admin.end_session, admin.reset_depth):
        with pytest.raises(KampungError) as e:
            act(s.id, "admin:web")
        assert e.value.code == "session_ended"
    actions = [r[0] for r in conn.execute("SELECT action FROM audit")]
    assert actions.count("end_session") == 1 and "reset_depth" not in actions


def test_rotate_key_replaces_the_key_and_keeps_the_machine(
    admin: AdminService,
    conn: sqlite3.Connection,
    machines: dict[str, tuple[Machine, str]],
    make_session: MakeSession,
) -> None:
    laptop, old = machines["laptop"]
    session = make_session("laptop", "api")
    mailboxes = conn.execute("SELECT id, repo FROM mailboxes").fetchall()
    new = admin.rotate_key("laptop", "admin:cli")
    assert new != old and new.startswith("kmp_")
    assert authenticate(conn, old) is None
    assert authenticate(conn, new) == laptop
    assert admin.list_pairs() == [("desktop", "laptop")]
    assert conn.execute("SELECT id, repo FROM mailboxes").fetchall() == mailboxes
    # Live sessions belong to whoever held the old key, so they end.
    loaded = load_session(conn, session.id)
    assert loaded is not None and loaded.ended_at is not None
    row = conn.execute(
        "SELECT actor, action, detail FROM audit ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert (row[0], row[1], json.loads(row[2])) == (
        "admin:cli",
        "rotate_key",
        {"machine": "laptop"},
    )


def test_rotate_key_refuses_unknown_and_revoked_machines(
    admin: AdminService, machines: dict[str, tuple[Machine, str]]
) -> None:
    assert _code(admin.rotate_key, "nope", "admin:cli") == "unknown_machine"
    admin.revoke("laptop", "admin:cli")
    assert _code(admin.rotate_key, "laptop", "admin:cli") == "already_revoked"
