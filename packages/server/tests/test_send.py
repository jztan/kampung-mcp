import sqlite3
from datetime import timedelta

import pytest

from server_testlib import FakeClock, MakeSession
from kampung_server.admin import AdminService
from kampung_server.clock import ts
from kampung_server.errors import KampungError
from kampung_server.guard import GuardSettings
from kampung_server import mail
from kampung_server.limits import MAX_WAITING_PER_MAILBOX, MAX_WAITING_PER_SENDER
from kampung_server.mail import inbox, send
from kampung_server.models import Session, load_session
from kampung_server.sweep import sweep


def _set(conn: sqlite3.Connection, s: Session, **cols: object) -> Session:
    sets = ", ".join(f"{k} = ?" for k in cols)
    conn.execute(f"UPDATE sessions SET {sets} WHERE id = ?", (*cols.values(), s.id))
    reloaded = load_session(conn, s.id)
    assert reloaded is not None
    return reloaded


def _code(conn: sqlite3.Connection, s: Session, to: str, body: str, now: object) -> str:
    with pytest.raises(KampungError) as e:
        send(conn, s, to, body, None, now)  # type: ignore[arg-type]
    return e.value.code


@pytest.fixture
def pair(make_session: MakeSession) -> tuple[Session, Session]:
    return make_session("laptop", "api"), make_session("desktop", "blog")


def test_body_size_counts_bytes(
    conn: sqlite3.Connection, pair: tuple[Session, Session], clock: FakeClock
) -> None:
    assert send(conn, pair[0], "desktop/blog", "a" * 8192, None, clock.now())
    bad = "€" * 2731
    assert len(bad.encode("utf-8")) == 8193
    assert _code(conn, pair[0], "desktop/blog", bad, clock.now()) == "body_too_large"


def test_empty_body(
    conn: sqlite3.Connection, pair: tuple[Session, Session], clock: FakeClock
) -> None:
    assert _code(conn, pair[0], "desktop/blog", "  \n", clock.now()) == "empty_body"


def test_not_paired(
    conn: sqlite3.Connection,
    admin: AdminService,
    pair: tuple[Session, Session],
    clock: FakeClock,
) -> None:
    admin.unpair("laptop", "desktop", "admin:cli")
    assert _code(conn, pair[0], "desktop/blog", "hi", clock.now()) == "not_paired"
    row = conn.execute("SELECT actor FROM audit WHERE action = 'denied'").fetchone()
    assert row[0] == "machine:laptop"


def test_send_to_revoked_machine(
    conn: sqlite3.Connection,
    admin: AdminService,
    pair: tuple[Session, Session],
    clock: FakeClock,
) -> None:
    admin.revoke("desktop", "admin:cli")
    code = _code(conn, pair[0], "desktop/blog", "hi", clock.now())
    assert code == "machine_revoked"


def test_unknown_machine_and_mailbox(
    conn: sqlite3.Connection, pair: tuple[Session, Session], clock: FakeClock
) -> None:
    assert _code(conn, pair[0], "spare/api", "hi", clock.now()) == "unknown_machine"
    assert _code(conn, pair[0], "desktop/nope", "hi", clock.now()) == (
        "unknown_mailbox"
    )
    assert _code(conn, pair[0], "nope", "hi", clock.now()) == "invalid_address"


def test_targeted_unknown_session(
    conn: sqlite3.Connection, pair: tuple[Session, Session], clock: FakeClock
) -> None:
    code = _code(conn, pair[0], "desktop/blog#ff", "hi", clock.now())
    assert code == "unknown_session"


def test_targeted_sets_to_session(
    conn: sqlite3.Connection, pair: tuple[Session, Session], clock: FakeClock
) -> None:
    r = send(conn, pair[0], pair[1].address, "hi", None, clock.now())
    row = conn.execute(
        "SELECT to_session_id FROM messages WHERE id = ?", (r.id,)
    ).fetchone()
    assert row[0] == pair[1].id


def test_reply_to_unknown(
    conn: sqlite3.Connection, pair: tuple[Session, Session], clock: FakeClock
) -> None:
    with pytest.raises(KampungError) as e:
        send(conn, pair[0], "desktop/blog", "hi", "msg_nope", clock.now())
    assert e.value.code == "unknown_message"


def test_send_to_own_mailbox_allowed(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    a = make_session("laptop", "api")
    make_session("laptop", "api")
    assert send(conn, a, "laptop/api", "hi", None, clock.now()).wake is True


def test_audit_never_holds_body(
    conn: sqlite3.Connection, pair: tuple[Session, Session], clock: FakeClock
) -> None:
    send(conn, pair[0], "desktop/blog", "SECRET-123", None, clock.now())
    details = [r[0] for r in conn.execute("SELECT detail FROM audit")]
    assert any('"bytes": 10' in d for d in details)
    assert not any("SECRET-123" in d for d in details)


def test_message_expiry_is_ttl(
    conn: sqlite3.Connection, pair: tuple[Session, Session], clock: FakeClock
) -> None:
    r = send(conn, pair[0], "desktop/blog", "hi", None, clock.now())
    row = conn.execute("SELECT expires_at FROM messages WHERE id = ?", (r.id,))
    assert row.fetchone()[0] == ts(clock.now() + timedelta(days=7))


def test_sweep_deletes_expired_and_ends_stale(
    conn: sqlite3.Connection, pair: tuple[Session, Session], clock: FakeClock
) -> None:
    send(conn, pair[0], "desktop/blog", "hi", None, clock.now())
    clock.advance(days=7)
    sweep(conn, clock.now())
    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
    ended = load_session(conn, pair[0].id)
    assert ended is not None and ended.ended_at == ts(clock.now())


def test_sweep_keeps_recent_sessions(
    conn: sqlite3.Connection, pair: tuple[Session, Session], clock: FakeClock
) -> None:
    clock.advance(hours=23, minutes=59)
    sweep(conn, clock.now())
    s = load_session(conn, pair[0].id)
    assert s is not None and s.ended_at is None


def test_sweep_expires_original_before_its_reply(
    conn: sqlite3.Connection, pair: tuple[Session, Session], clock: FakeClock
) -> None:
    first = send(conn, pair[0], "desktop/blog", "question", None, clock.now())
    clock.advance(days=1)
    reply = send(conn, pair[1], "laptop/api", "answer", first.id, clock.now())
    clock.advance(days=6)
    sweep(conn, clock.now())
    ids = [r[0] for r in conn.execute("SELECT id FROM messages")]
    assert ids == [reply.id]


def test_mailbox_full_counts_only_waiting_mail(
    conn: sqlite3.Connection,
    pair: tuple[Session, Session],
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One sender stands in for many here, so lift its own share.
    monkeypatch.setattr(mail, "MAX_WAITING_PER_SENDER", MAX_WAITING_PER_MAILBOX)
    for _ in range(MAX_WAITING_PER_MAILBOX):
        send(conn, pair[0], "desktop/blog", "hi", None, clock.now())
    assert _code(conn, pair[0], "desktop/blog", "hi", clock.now()) == "mailbox_full"
    inbox(conn, pair[1], 1, clock.now())
    send(conn, pair[0], "desktop/blog", "hi", None, clock.now())
    clock.advance(days=8)
    send(conn, pair[0], "desktop/blog", "hi", None, clock.now())


def test_reply_to_only_mail_the_sender_has_seen(
    conn: sqlite3.Connection,
    pair: tuple[Session, Session],
    admin: AdminService,
    make_session: MakeSession,
    clock: FakeClock,
) -> None:
    admin.add_machine("tablet", "admin:cli")
    admin.pair("tablet", "laptop", "admin:cli")
    tablet = make_session("tablet", "web")
    laptop, desktop = pair
    original = send(conn, desktop, "laptop/api", "hi", None, clock.now()).id
    assert send(conn, laptop, "desktop/blog", "re", original, clock.now())
    assert send(conn, desktop, "laptop/api", "and", original, clock.now())
    with pytest.raises(KampungError) as e:
        send(conn, tablet, "laptop/api", "x", original, clock.now())
    assert e.value.code == "unknown_message"


def test_one_sender_cannot_fill_a_mailbox_for_everyone(
    conn: sqlite3.Connection,
    pair: tuple[Session, Session],
    admin: AdminService,
    make_session: MakeSession,
    clock: FakeClock,
) -> None:
    admin.add_machine("tablet", "admin:cli")
    admin.pair("tablet", "desktop", "admin:cli")
    tablet = make_session("tablet", "web")
    flooder, reader = pair
    for _ in range(MAX_WAITING_PER_SENDER):
        send(conn, flooder, "desktop/blog", "spam", None, clock.now())
    assert _code(conn, flooder, "desktop/blog", "more", clock.now()) == "mailbox_full"
    assert send(conn, tablet, "desktop/blog", "still gets through", None, clock.now())
    inbox(conn, reader, 1, clock.now())
    assert send(conn, flooder, "desktop/blog", "room again", None, clock.now())


def _phone(make_session: MakeSession) -> Session:
    return make_session(
        "laptop", repo="claude", agent="claude-app", hooks=False, cwd="Claude app"
    )


SMALL = GuardSettings(connector_wake_budget=3)


def test_wake_past_the_budget_is_stored_without_wake(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    phone = _phone(make_session)
    peer = make_session("desktop", "blog")
    for _ in range(3):
        r = send(conn, phone, "desktop/blog", "x", None, clock.now(), guard=SMALL)
        assert r.wake is True and r.reason is None
    r = send(conn, phone, "desktop/blog", "x", None, clock.now(), guard=SMALL)
    assert r.wake is False and r.reason == "wake_budget"
    assert r.to_dict()["reason"] == "wake_budget"
    assert "3 times in 10 minutes" in str(r.to_dict()["hint"])
    assert "desktop/blog" in str(r.to_dict()["hint"])
    assert len(inbox(conn, peer, 20, clock.now()).messages) == 4


def test_default_budget_is_sixty_per_target() -> None:
    assert GuardSettings().connector_wake_budget == 60


def test_budget_is_counted_per_target_mailbox(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    phone = _phone(make_session)
    make_session("desktop", "blog")
    make_session("desktop", "api")
    for _ in range(3):
        send(conn, phone, "desktop/blog", "x", None, clock.now(), guard=SMALL)
    blocked = send(conn, phone, "desktop/blog", "x", None, clock.now(), guard=SMALL)
    assert blocked.wake is False
    other = send(conn, phone, "desktop/api", "x", None, clock.now(), guard=SMALL)
    assert other.wake is True


def test_budget_frees_after_the_window(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    phone = _phone(make_session)
    make_session("desktop", "blog")
    for _ in range(3):
        send(conn, phone, "desktop/blog", "x", None, clock.now(), guard=SMALL)
    assert not send(
        conn, phone, "desktop/blog", "x", None, clock.now(), guard=SMALL
    ).wake
    clock.advance(minutes=11)
    assert send(conn, phone, "desktop/blog", "x", None, clock.now(), guard=SMALL).wake


def test_budget_does_not_apply_to_adapters(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    laptop = make_session("laptop", "api")
    make_session("desktop", "blog")
    reasons = [
        send(conn, laptop, "desktop/blog", "x", None, clock.now()).reason
        for _ in range(11)
    ]
    assert "wake_budget" not in reasons
