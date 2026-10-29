import sqlite3

from server_testlib import FakeClock, MakeSession
from kampung_server.limits import ENDED_SESSION_RETENTION
from kampung_server.mail import inbox, send
from kampung_server.models import load_session
from kampung_server.sessions import end
from kampung_server.sweep import sweep


def _ids(conn: sqlite3.Connection) -> list[int]:
    return [r[0] for r in conn.execute("SELECT id FROM sessions ORDER BY id")]


def test_long_ended_sessions_are_deleted(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    old, recent, newest = (make_session("laptop", "api") for _ in range(3))
    end(conn, old.id, clock.now())
    clock.advance(seconds=ENDED_SESSION_RETENTION.total_seconds() + 60)
    end(conn, recent.id, clock.now())
    end(conn, newest.id, clock.now())
    sweep(conn, clock.now())
    assert _ids(conn) == [recent.id, newest.id]


def test_newest_session_is_kept_so_ids_never_repeat(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    a, b = make_session("laptop", "api"), make_session("laptop", "api")
    end(conn, a.id, clock.now())
    end(conn, b.id, clock.now())
    clock.advance(days=ENDED_SESSION_RETENTION.days + 1)
    sweep(conn, clock.now())
    assert _ids(conn) == [b.id]
    assert make_session("laptop", "api").id > b.id


def test_sessions_referenced_by_mail_are_kept(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    sender = make_session("laptop", "api")
    make_session("desktop", "blog")
    send(conn, sender, "desktop/blog", "hi", None, clock.now())
    end(conn, sender.id, clock.now())
    make_session("laptop", "api")  # so the sender is not the newest row
    # Mail lives 7 days, so jump past retention without expiring it.
    conn.execute(
        "UPDATE sessions SET ended_at = '2000-01-01T00:00:00.000000Z' WHERE id = ?",
        (sender.id,),
    )
    sweep(conn, clock.now())
    assert load_session(conn, sender.id) is not None


def test_live_sessions_are_never_deleted(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    s = make_session("laptop", "api")
    make_session("laptop", "api")
    conn.execute(
        "UPDATE sessions SET started_at = '2000-01-01T00:00:00.000000Z' WHERE id = ?",
        (s.id,),
    )
    sweep(conn, clock.now())
    assert load_session(conn, s.id) is not None


def test_sessions_mail_was_sent_to_or_read_by_are_kept(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    sender = make_session("laptop", "api")
    target = make_session("desktop", "blog")
    reader = make_session("desktop", "blog")
    send(conn, sender, f"desktop/blog#{target.suffix}", "for you", None, clock.now())
    send(conn, sender, "desktop/blog", "for anyone", None, clock.now())
    inbox(conn, reader, 20, clock.now())  # delivers the untargeted one to reader
    make_session("laptop", "api")  # none of them is the newest row
    for s in (target, reader):
        conn.execute(
            "UPDATE sessions SET ended_at = '2000-01-01T00:00:00.000000Z' "
            "WHERE id = ?",
            (s.id,),
        )
    sweep(conn, clock.now())
    assert load_session(conn, target.id) is not None
    assert load_session(conn, reader.id) is not None
