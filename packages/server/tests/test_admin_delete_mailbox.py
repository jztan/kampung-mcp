import sqlite3

import pytest

from server_testlib import FakeClock, MakeSession
from kampung_server.admin import AdminService
from kampung_server.errors import KampungError
from kampung_server.limits import MESSAGE_TTL
from kampung_server.mail import inbox, send
from kampung_server.sessions import end


def _code(fn: object, *args: object) -> str:
    with pytest.raises(KampungError) as e:
        fn(*args)  # type: ignore[operator]
    return e.value.code


def _has_box(conn: sqlite3.Connection, repo: str) -> bool:
    row = conn.execute("SELECT 1 FROM mailboxes WHERE repo = ?", (repo,)).fetchone()
    return row is not None


def _count(conn: sqlite3.Connection, sql: str) -> int:
    return int(conn.execute(sql).fetchone()[0])


def test_delete_mailbox_removes_everything(
    conn: sqlite3.Connection,
    admin: AdminService,
    make_session: MakeSession,
    clock: FakeClock,
) -> None:
    a, b = make_session("laptop", "old"), make_session("desktop", "blog")
    m = send(conn, b, "laptop/old", "hi", None, clock.now())
    inbox(conn, a, 20, clock.now())
    reply = send(conn, a, "desktop/blog", "re", m.id, clock.now())
    inbox(conn, b, 20, clock.now())
    end(conn, a.id, clock.now())
    make_session("desktop", "other")  # so a is not the newest session row
    assert _code(admin.delete_mailbox, "laptop/old", "admin:cli") == (
        "mailbox_referenced"
    )
    conn.execute("DELETE FROM messages WHERE id = ?", (reply.id,))
    assert admin.delete_mailbox("laptop/old", "admin:cli") == (1, 1)
    assert not _has_box(conn, "old")
    assert _count(conn, f"SELECT count(*) FROM sessions WHERE id = {a.id}") == 0
    row = conn.execute(
        "SELECT actor, detail FROM audit WHERE action = 'delete_mailbox'"
    ).fetchone()
    assert row[0] == "admin:cli"
    assert '"mailbox": "laptop/old"' in row[1]


def test_reply_into_deleted_mailbox_is_cleared(
    conn: sqlite3.Connection,
    admin: AdminService,
    make_session: MakeSession,
    clock: FakeClock,
) -> None:
    a, b = make_session("laptop", "old"), make_session("desktop", "blog")
    m = send(conn, b, "laptop/old", "hi", None, clock.now())
    inbox(conn, a, 20, clock.now())
    # desktop answers its own message into its own mailbox: a reply that
    # points into laptop/old without laptop/old's session sending anything
    c = make_session("desktop", "other")
    note = send(conn, c, "desktop/blog", "see that", m.id, clock.now())
    end(conn, a.id, clock.now())
    make_session("desktop", "third")
    admin.delete_mailbox("laptop/old", "admin:web")
    got = conn.execute("SELECT reply_to FROM messages WHERE id = ?", (note.id,))
    assert got.fetchone()[0] is None


def test_refusals(
    conn: sqlite3.Connection,
    admin: AdminService,
    make_session: MakeSession,
    clock: FakeClock,
) -> None:
    assert _code(admin.delete_mailbox, "laptop/nope", "admin:cli") == (
        "unknown_mailbox"
    )
    assert _code(admin.delete_mailbox, "laptop/api#3f", "admin:cli") == (
        "unknown_mailbox"
    )
    busy = make_session("laptop", "busy")
    assert _code(admin.delete_mailbox, "laptop/busy", "admin:cli") == ("mailbox_in_use")
    end(conn, busy.id, clock.now())
    sender = make_session("desktop", "blog")
    send(conn, sender, "laptop/busy", "waiting", None, clock.now())
    assert _code(admin.delete_mailbox, "laptop/busy", "admin:cli") == (
        "mailbox_has_mail"
    )
    newest = make_session("laptop", "last")
    end(conn, newest.id, clock.now())
    assert _code(admin.delete_mailbox, "laptop/last", "admin:cli") == (
        "mailbox_newest_session"
    )


def test_expired_unswept_mail_does_not_block(
    conn: sqlite3.Connection,
    admin: AdminService,
    make_session: MakeSession,
    clock: FakeClock,
) -> None:
    a, b = make_session("laptop", "old"), make_session("desktop", "blog")
    send(conn, b, "laptop/old", "late", None, clock.now())
    end(conn, a.id, clock.now())
    make_session("desktop", "other")
    clock.advance(days=MESSAGE_TTL.days + 1)
    assert admin.delete_mailbox("laptop/old", "admin:cli") == (1, 1)


def test_refused_delete_changes_nothing_and_is_not_audited(
    conn: sqlite3.Connection,
    admin: AdminService,
    make_session: MakeSession,
    clock: FakeClock,
) -> None:
    make_session("laptop", "busy")
    before = _count(conn, "SELECT count(*) FROM audit")
    _code(admin.delete_mailbox, "laptop/busy", "admin:cli")
    assert _has_box(conn, "busy")
    assert _count(conn, "SELECT count(*) FROM audit") == before
