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


def test_refill(
    conn: sqlite3.Connection, admin: AdminService, make_session: MakeSession
) -> None:
    s = make_session("laptop", "api")
    conn.execute(
        "UPDATE mailboxes SET wakes_since_refill = 30 WHERE id = ?", (s.mailbox_id,)
    )
    admin.refill(s.id, "admin:web")
    used = conn.execute(
        "SELECT wakes_since_refill FROM mailboxes WHERE id = ?", (s.mailbox_id,)
    ).fetchone()[0]
    assert used == 0
    actor = conn.execute("SELECT actor FROM audit WHERE action = 'refill'").fetchone()
    assert actor[0] == "admin:web"
    assert _code(admin.refill, 9999, "admin:web") == "unknown_session"


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
    for act in (admin.end_session, admin.refill):
        with pytest.raises(KampungError) as e:
            act(s.id, "admin:web")
        assert e.value.code == "session_ended"
    actions = [r[0] for r in conn.execute("SELECT action FROM audit")]
    assert actions.count("end_session") == 1 and "refill" not in actions


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


def test_connect_creates_the_machine_and_a_code(
    admin: AdminService, conn: sqlite3.Connection, clock: FakeClock
) -> None:
    from kampung_server import oauth_store
    from kampung_server.models import load_machine

    code = admin.connect("phone", "admin:cli")
    phone = load_machine(conn, "phone")
    assert phone is not None and len(code) >= 22
    actions = [r[0] for r in conn.execute("SELECT action FROM audit ORDER BY id")]
    assert actions[-2:] == ["add_machine", "connect_code"]
    assert oauth_store.take_connect_code(conn, code, clock.now()) == phone.id
    # A second connect for a known machine adds no machine.
    admin.connect("phone", "admin:cli")
    actions = [r[0] for r in conn.execute("SELECT action FROM audit ORDER BY id")]
    assert actions[-1] == "connect_code" and actions.count("add_machine") == 1


def test_connect_refuses_a_revoked_machine(
    admin: AdminService, machines: dict[str, object]
) -> None:
    admin.revoke("laptop", "admin:cli")
    with pytest.raises(KampungError) as e:
        admin.connect("laptop", "admin:cli")
    assert e.value.code == "already_revoked"


def _grant(admin: AdminService, conn: sqlite3.Connection, clock: FakeClock) -> int:
    from kampung_server import db, oauth_store
    from kampung_server.models import load_machine

    laptop = load_machine(conn, "laptop")
    assert laptop is not None
    with db.transaction(conn):
        grant = oauth_store.replace_grant(conn, laptop.id, clock.now())
        oauth_store.save_token(
            conn, grant, "access", "kpa_1", "{}", clock.now() + timedelta(hours=1)
        )
    return laptop.id


def test_disconnect_drops_the_grant_and_keeps_mail(
    admin: AdminService,
    conn: sqlite3.Connection,
    machines: dict[str, object],
    make_session: MakeSession,
    clock: FakeClock,
) -> None:
    from kampung_server import oauth_store
    from kampung_server.mail import send

    laptop_id = _grant(admin, conn, clock)
    phone = make_session(
        "laptop", repo="claude", agent="claude-app", hooks=False, cwd="Claude app"
    )
    make_session("desktop", "blog")
    send(conn, phone, "desktop/blog", "keep me", None, clock.now())
    admin.disconnect("laptop", "admin:cli")
    assert oauth_store.grant_for_machine(conn, laptop_id) is None
    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 1
    ended = conn.execute(
        "SELECT ended_at FROM sessions WHERE id = ?", (phone.id,)
    ).fetchone()[0]
    assert ended is not None
    assert (
        conn.execute(
            "SELECT revoked_at FROM machines WHERE id = ?", (laptop_id,)
        ).fetchone()[0]
        is None
    )
    with pytest.raises(KampungError) as e:
        admin.disconnect("laptop", "admin:cli")
    assert e.value.code == "not_connected"
    actions = [r[0] for r in conn.execute("SELECT action FROM audit")]
    assert "disconnect" in actions


def test_revoke_and_rotate_drop_the_grant(
    admin: AdminService,
    conn: sqlite3.Connection,
    machines: dict[str, object],
    clock: FakeClock,
) -> None:
    from kampung_server import oauth_store

    laptop_id = _grant(admin, conn, clock)
    admin.rotate_key("laptop", "admin:cli")
    assert oauth_store.grant_for_machine(conn, laptop_id) is None
    _grant(admin, conn, clock)
    admin.revoke("laptop", "admin:cli")
    assert oauth_store.grant_for_machine(conn, laptop_id) is None


from datetime import timedelta  # noqa: E402
