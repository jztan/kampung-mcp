import sqlite3

from kampung_server import mail
from kampung_server.admin import AdminService
from kampung_server.admin_web import queries
from kampung_server.models import Machine, Session
from server_testlib import FakeClock, MakeSession

Machines = dict[str, tuple[Machine, str]]


def drained(conn: sqlite3.Connection, session: Session) -> Session:
    """The session with its mailbox's wake budget used up: its sends are held."""
    conn.execute(
        "UPDATE mailboxes SET wakes_since_refill = 1000 WHERE id = ?",
        (session.mailbox_id,),
    )
    return session


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
    mail.send(conn, drained(conn, a), "desktop/api", "deep", None, clock.now())
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
    held = mail.send(conn, drained(conn, a), "desktop/api", "held", None, clock.now())

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
    mail.send(conn, drained(conn, a), "desktop/api", "held", None, clock.now())
    api = queries.messages(conn, clock.now(), "desktop/api").rows
    assert {r.to for r in api} == {"desktop/api"} and len(api) == 2
    held = queries.messages(conn, clock.now(), None, no_wake_only=True).rows
    assert [r.to for r in held] == ["desktop/api"]


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


def _owner_instruction(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> str:
    from kampung_server import db, oauth_store
    from kampung_server.models import load_machine

    laptop = load_machine(conn, "laptop")
    assert laptop is not None
    with db.transaction(conn):
        oauth_store.replace_grant(conn, laptop.id, clock.now())
        oauth_store.set_owner(conn, laptop.id, True)
    phone = make_session(
        "laptop", repo="claude", agent="claude-app", hooks=False, cwd="Claude app"
    )
    make_session("desktop", "api")
    r = mail.send(
        conn,
        phone,
        "desktop/api",
        "merge it",
        None,
        clock.now(),
        mode="instruct",
        connector=True,
    )
    return r.id


def test_machine_row_shows_grant_and_owner(
    conn: sqlite3.Connection,
    admin: AdminService,
    machines: Machines,
    clock: FakeClock,
) -> None:
    from kampung_server import db, oauth_store

    with db.transaction(conn):
        oauth_store.replace_grant(conn, machines["laptop"][0].id, clock.now())
    rows = {r.name: r for r in queries.machines(conn, clock.now())}
    assert rows["laptop"].grant_created_at is not None
    assert rows["laptop"].owner is False
    assert rows["desktop"].grant_created_at is None
    admin.owner_add("laptop", "admin:cli")
    rows = {r.name: r for r in queries.machines(conn, clock.now())}
    assert rows["laptop"].owner is True


def test_owner_row_mode_and_expiry(
    conn: sqlite3.Connection,
    machines: Machines,
    make_session: MakeSession,
    clock: FakeClock,
) -> None:
    msg_id = _owner_instruction(conn, make_session, clock)
    [row] = queries.messages(conn, clock.now(), None).rows
    assert (row.mode, row.state) == ("instruct", "queued")
    detail = queries.message(conn, clock.now(), msg_id)
    assert detail is not None and detail.mode == "instruct"
    clock.advance(minutes=61)
    now = clock.now()
    assert queries.overview(conn, now).queued == 0
    assert queries.overview(conn, now).oldest_queued_at is None
    box = {m.address: m for m in queries.mailboxes(conn, now)}["desktop/api"]
    assert (box.queued, box.oldest_queued_at) == (0, None)
    assert queries.messages(conn, now, None).rows == []
    [expired] = queries.messages(conn, now, None, state="expired").rows
    assert expired.state == "expired"
    assert all(c.queued == 0 for c in queries.conversations(conn, now))


def test_sessions_state_unreachable(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    make_session("desktop", "blog", idle_wake=True)
    make_session("desktop", "web")
    clock.advance(minutes=6)
    states = {
        r.address.split("#")[0]: r.state
        for r in queries.sessions(conn, clock.now()).rows
    }
    assert states == {"desktop/blog": "unreachable", "desktop/web": "live"}


def test_mailboxes_count_unreachable(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    make_session("desktop", "blog", idle_wake=True)
    clock.advance(minutes=6)
    row = next(
        r for r in queries.mailboxes(conn, clock.now()) if r.address == "desktop/blog"
    )
    assert (row.live, row.unreachable) == (1, 1)
