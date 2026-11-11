"""Background sessions (Claude Code's `claude --bg`, `/background`, agents):
tagged at registration, listed as such, and kept from taking mailbox mail
while a foreground window is working in the same mailbox."""

import sqlite3

from kampung_server import sessions
from kampung_server.mail import inbox, list_peers, peek, send
from kampung_server.models import load_machine
from server_testlib import FakeClock, MakeSession


def test_register_records_background_and_a_revive_updates_it(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    bg = make_session("laptop", agent_session_id="cc-1", background=True)
    assert bg.background is True
    machine = load_machine(conn, "laptop")
    assert machine is not None
    again = sessions.register(
        conn,
        machine,
        clock.now(),
        agent="claude-code",
        cwd="/home/u/api",
        repo="api",
        hooks=True,
        agent_session_id="cc-1",
    )
    assert again.id == bg.id and again.background is False
    details = [
        r[0]
        for r in conn.execute(
            "SELECT detail FROM audit WHERE action IN ('register', 'revive') "
            "ORDER BY id"
        )
    ]
    assert '"background": true' in details[0] and "background" not in details[1]
    assert make_session("laptop", "blog").background is False


def test_list_peers_tags_background_sessions(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    make_session("desktop", "blog")
    make_session("desktop", "blog", background=True)
    machine = load_machine(conn, "laptop")
    assert machine is not None
    peers = list_peers(conn, machine, clock.now(), "desktop")
    tags = [s["background"] for s in peers[0]["sessions"]]  # type: ignore[index]
    assert sorted(tags) == [False, True]


def test_background_session_leaves_mailbox_mail_to_the_window(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    a = make_session("laptop", "api")
    window = make_session("desktop", "blog")
    bg = make_session("desktop", "blog", background=True)
    send(conn, a, "desktop/blog", "for the mailbox", None, clock.now())
    send(conn, a, bg.address, "for the background one", None, clock.now())
    assert peek(conn, bg, clock.now()).waiting == 1
    got = inbox(conn, bg, 20, clock.now()).messages
    assert [m["body"] for m in got] == ["for the background one"]
    assert [m["body"] for m in inbox(conn, window, 20, clock.now()).messages] == [
        "for the mailbox"
    ]


def test_background_session_reads_mailbox_mail_when_no_window_is_live(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    a = make_session("laptop", "api")
    window = make_session("desktop", "blog")
    bg = make_session("desktop", "blog", background=True)
    sessions.end(conn, window.id, clock.now())
    send(conn, a, "desktop/blog", "anyone there", None, clock.now())
    assert [m["body"] for m in inbox(conn, bg, 20, clock.now()).messages] == [
        "anyone there"
    ]


def test_send_to_a_background_only_mailbox_says_the_reply_is_late(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    a = make_session("laptop", "api")
    make_session("desktop", "blog", background=True)
    r = send(conn, a, "desktop/blog", "x", None, clock.now())
    assert r.wake is True and r.note == "background"
    assert r.hint and "background" in r.hint and "attach" in r.hint
    make_session("desktop", "blog")  # a window opens
    r = send(conn, a, "desktop/blog", "y", None, clock.now())
    assert r.note is None
