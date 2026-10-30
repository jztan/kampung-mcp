import sqlite3

from kampung_server import mail
from kampung_server.admin import AdminService
from kampung_server.admin_web import queries
from kampung_server.models import Machine, Session, load_session
from server_testlib import FakeClock, MakeSession

Machines = dict[str, tuple[Machine, str]]


def deep(conn: sqlite3.Connection, session: Session, depth: int) -> Session:
    """The session as if it had read mail of this depth."""
    conn.execute(
        "UPDATE sessions SET max_depth_seen = ?, last_delivered_at = last_seen "
        "WHERE id = ?",
        (depth, session.id),
    )
    conn.commit()
    loaded = load_session(conn, session.id)
    assert loaded is not None
    return loaded


def test_overview_counts(
    conn: sqlite3.Connection,
    admin: AdminService,
    make_session: MakeSession,
    clock: FakeClock,
) -> None:
    a = make_session("laptop", "api")
    make_session("desktop", "api")
    mail.send(conn, a, "desktop/api", "one", None, clock.now())
    mail.send(conn, a, "desktop/api", "two", None, clock.now())
    mail.send(conn, deep(conn, a, 3), "desktop/api", "deep", None, clock.now())
    admin.add_machine("old", "admin:cli")
    admin.revoke("old", "admin:cli")
    o = queries.overview(conn, clock.now())
    assert (o.machines, o.revoked) == (3, 1)
    assert o.live_sessions == 2
    assert (o.queued, o.no_wake, o.sent_24h) == (3, 1, 3)
    assert o.oldest_queued_at is not None
    assert 0 < len(o.recent_audit) <= 20


def test_session_states(
    conn: sqlite3.Connection,
    admin: AdminService,
    make_session: MakeSession,
    clock: FakeClock,
) -> None:
    s = make_session("laptop", "api")
    assert queries.sessions(conn, clock.now()).rows[0].state == "live"
    clock.advance(minutes=31)
    assert queries.sessions(conn, clock.now()).rows[0].state == "idle"
    admin.end_session(s.id, "admin:cli")
    assert queries.sessions(conn, clock.now()).rows == []
    ended = queries.sessions(conn, clock.now(), state="ended").rows
    assert [r.state for r in ended] == ["ended"]
    assert len(queries.sessions(conn, clock.now(), state="all").rows) == 1


def test_message_states(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    a = make_session("laptop", "api")
    b = make_session("desktop", "api")
    first = mail.send(conn, a, "desktop/api", "read me", None, clock.now())
    clock.advance(seconds=1)  # inbox reads oldest first
    held = mail.send(conn, deep(conn, a, 3), "desktop/api", "held", None, clock.now())

    def state(message_id: str) -> str:
        rows = queries.messages(conn, clock.now(), None, state="all").rows
        return next(r.state for r in rows if r.id == message_id)

    assert state(first.id) == "queued"
    assert state(held.id) == "no-wake"
    mail.inbox(conn, b, 1, clock.now())
    assert state(first.id) == "delivered"
    clock.advance(days=7, seconds=1)
    assert state(held.id) == "expired"
    assert queries.messages(conn, clock.now(), None).rows == []  # default: queued


def test_messages_filters(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    a = make_session("laptop", "api")
    make_session("desktop", "api")
    make_session("desktop", "blog")
    mail.send(conn, a, "desktop/api", "to api", None, clock.now())
    mail.send(conn, a, "desktop/blog", "to blog", None, clock.now())
    mail.send(conn, deep(conn, a, 3), "desktop/api", "held", None, clock.now())
    api = queries.messages(conn, clock.now(), "desktop/api").rows
    assert {r.to for r in api} == {"desktop/api"} and len(api) == 2
    held = queries.messages(conn, clock.now(), None, no_wake_only=True).rows
    assert [r.depth for r in held] == [4]


def test_lists_cap_at_100(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    a = make_session("laptop", "api")
    make_session("desktop", "api")
    for i in range(105):
        clock.advance(seconds=1)
        mail.send(conn, a, "desktop/api", f"m{i}", None, clock.now())
    page = queries.messages(conn, clock.now(), None)
    assert len(page.rows) == 100 and page.total == 105
    assert page.rows[0].sent_at > page.rows[-1].sent_at  # newest first


def test_audit_filters(
    conn: sqlite3.Connection, admin: AdminService, machines: Machines
) -> None:
    admin.unpair("laptop", "desktop", "admin:web")
    cli = queries.audit(conn, actor="admin:cli", action=None).rows
    assert cli and {r.actor for r in cli} == {"admin:cli"}
    pairs = queries.audit(conn, actor=None, action="pair").rows
    assert [r.action for r in pairs] == ["pair"]


def test_message_detail(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    a = make_session("laptop", "api")
    make_session("desktop", "api")
    sent = mail.send(conn, a, "desktop/api", "hello", None, clock.now())
    d = queries.message(conn, clock.now(), sent.id)
    assert d is not None
    assert (d.sender, d.to, d.body, d.state) == (
        a.address,
        "desktop/api",
        "hello",
        "queued",
    )
    assert queries.message(conn, clock.now(), "nope") is None


def test_machines_and_mailboxes(
    conn: sqlite3.Connection,
    admin: AdminService,
    make_session: MakeSession,
    clock: FakeClock,
) -> None:
    a = make_session("laptop", "api")
    admin.end_session(make_session("desktop", "blog").id, "admin:cli")
    mail.send(conn, a, "desktop/blog", "queued for later", None, clock.now())
    admin.add_machine("vps", "admin:cli")
    rows = {m.name: m for m in queries.machines(conn, clock.now())}
    assert rows["laptop"].paired_with == ["desktop"] and rows["laptop"].active
    assert rows["laptop"].last_seen is not None and rows["vps"].last_seen is None
    boxes = {b.address: b for b in queries.mailboxes(conn, clock.now())}
    assert boxes["laptop/api"].live == 1
    assert boxes["desktop/blog"].queued == 1 and boxes["desktop/blog"].live == 0


def test_audit_notable_hides_routine_traffic(
    conn: sqlite3.Connection,
    admin: AdminService,
    make_session: MakeSession,
    clock: FakeClock,
) -> None:
    a = make_session("laptop", "api")
    b = make_session("desktop", "api")
    mail.send(conn, a, "desktop/api", "hi", None, clock.now())
    mail.inbox(conn, b, 20, clock.now())
    admin.unpair("laptop", "desktop", "admin:web")
    every = {r.action for r in queries.audit(conn, None, None).rows}
    notable = {r.action for r in queries.audit(conn, None, None, notable=True).rows}
    assert {"send", "inbox", "register"} <= every
    assert notable == every - set(queries.ROUTINE)
    assert "unpair" in notable


def test_audit_fields_name_sessions_and_link_messages(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    a = make_session("laptop", "api")
    b = make_session("desktop", "api")
    sent = mail.send(conn, a, "desktop/api", "hi", None, clock.now())
    mail.inbox(conn, b, 20, clock.now())
    rows = {r.action: r for r in queries.audit(conn, None, None).rows}
    send = {f.key: f for f in rows["send"].fields}
    assert send["to"].text == "desktop/api" and send["to"].href is None
    assert send["id"].href == f"/admin/messages/{sent.id}"
    read = {f.key: f for f in rows["inbox"].fields}
    assert read["session"].text == b.address
    assert read["ids"].text == sent.id
    assert read["ids"].href == f"/admin/messages/{sent.id}"


def test_conversations_pair_both_directions(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    a = make_session("laptop", "api")
    b = make_session("desktop", "blog")
    mail.send(conn, a, "desktop/blog", "one", None, clock.now())
    clock.advance(seconds=5)
    mail.send(conn, b, "laptop/api", "two", None, clock.now())
    clock.advance(seconds=5)
    last = mail.send(conn, a, f"desktop/blog#{b.suffix}", "three", None, clock.now())
    [c] = queries.conversations(conn, clock.now())
    assert (c.a, c.b, c.messages) == ("desktop/blog", "laptop/api", 3)
    detail = queries.message(conn, clock.now(), last.id)
    assert detail is not None and c.last_at == detail.sent_at


def test_thread_is_oldest_first_with_sides(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    a = make_session("laptop", "api")
    b = make_session("desktop", "blog")
    other = make_session("laptop", "web")
    mail.send(conn, a, "desktop/blog", "one", None, clock.now())
    clock.advance(seconds=5)
    mail.send(conn, b, "laptop/api", "two", None, clock.now())
    mail.send(conn, other, "desktop/blog", "elsewhere", None, clock.now())
    page = queries.thread(conn, clock.now(), "desktop/blog", "laptop/api")
    assert [m.body for m in page.rows] == ["one", "two"]
    assert [m.side for m in page.rows] == ["b", "a"]
    assert page.total == 2


def test_thread_cuts_long_bodies(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    a = make_session("laptop", "api")
    make_session("desktop", "blog")
    mail.send(conn, a, "desktop/blog", "x" * 5000, None, clock.now())
    [m] = queries.thread(conn, clock.now(), "desktop/blog", "laptop/api").rows
    assert len(m.body) == queries.THREAD_PREVIEW and m.cut == 5000 - len(m.body)
