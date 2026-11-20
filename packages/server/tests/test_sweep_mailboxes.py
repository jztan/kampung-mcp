import sqlite3

import pytest

from server_testlib import FakeClock, MakeSession
from kampung_server import sweep as sweep_module
from kampung_server.clock import ts
from kampung_server.errors import KampungError
from kampung_server.limits import ENDED_SESSION_RETENTION
from kampung_server.mail import send
from kampung_server.models import Session
from kampung_server.sessions import end
from kampung_server.sweep import housekeep, sweep


def _boxes(conn: sqlite3.Connection) -> set[str]:
    return {
        f"{r[0]}/{r[1]}"
        for r in conn.execute(
            "SELECT m.name, mb.repo FROM mailboxes mb "
            "JOIN machines m ON m.id = mb.machine_id"
        )
    }


def _age_out(conn: sqlite3.Connection, clock: FakeClock, *ended: Session) -> None:
    for s in ended:
        end(conn, s.id, clock.now())
    clock.advance(days=ENDED_SESSION_RETENTION.days + 1)


def _bare_mailbox(
    conn: sqlite3.Connection, machine: str, repo: str, clock: FakeClock
) -> None:
    conn.execute(
        "INSERT INTO mailboxes (machine_id, repo, created_at) "
        "SELECT id, ?, ? FROM machines WHERE name = ?",
        (repo, ts(clock.now()), machine),
    )


def _audits(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return [
        (r[0], r[1])
        for r in conn.execute(
            "SELECT actor, detail FROM audit WHERE action = 'delete_mailboxes'"
        )
    ]


def test_empty_mailbox_is_deleted(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    old = make_session("laptop", "old")
    make_session("laptop", "keep")  # the newest row, so it stays
    _age_out(conn, clock, old)
    housekeep(conn, clock.now())
    assert "laptop/old" not in _boxes(conn)
    assert "laptop/keep" in _boxes(conn)


def test_mailbox_with_newest_session_row_stays(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    only = make_session("laptop", "old")
    _age_out(conn, clock, only)
    housekeep(conn, clock.now())
    assert "laptop/old" in _boxes(conn)


def test_mailbox_with_unexpired_mail_stays(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    _bare_mailbox(conn, "laptop", "old", clock)
    sender = make_session("desktop", "blog")
    send(conn, sender, "laptop/old", "hi", None, clock.now())
    housekeep(conn, clock.now())
    assert "laptop/old" in _boxes(conn)


def test_connector_mailbox_kept_while_grant_exists(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    make_session("desktop", "blog")  # pulls in the machines fixture
    _bare_mailbox(conn, "laptop", "claude", clock)
    conn.execute(
        "INSERT INTO oauth_grants (machine_id, created_at) "
        "SELECT id, ? FROM machines WHERE name = 'laptop'",
        (ts(clock.now()),),
    )
    housekeep(conn, clock.now())
    assert "laptop/claude" in _boxes(conn)
    conn.execute("DELETE FROM oauth_grants")
    housekeep(conn, clock.now())
    assert "laptop/claude" not in _boxes(conn)


def test_batch_limit(
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_session("desktop", "blog")
    for repo in ("a", "b", "c"):
        _bare_mailbox(conn, "laptop", repo, clock)
    monkeypatch.setattr(sweep_module, "MAILBOX_DELETE_BATCH", 2)
    housekeep(conn, clock.now())
    assert len({"laptop/a", "laptop/b", "laptop/c"} & _boxes(conn)) == 1
    housekeep(conn, clock.now())
    assert not {"laptop/a", "laptop/b", "laptop/c"} & _boxes(conn)


def test_sweep_audits_deleted_mailboxes(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    make_session("desktop", "blog")
    housekeep(conn, clock.now())
    assert _audits(conn) == []
    _bare_mailbox(conn, "laptop", "old", clock)
    housekeep(conn, clock.now())
    [(actor, detail)] = _audits(conn)
    assert actor == "server"
    assert "laptop/old" in detail and '"count": 1' in detail


def test_reregister_recreates_mailbox_and_send_works(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    old = make_session("laptop", "old")
    sender = make_session("desktop", "blog")
    _age_out(conn, clock, old)
    housekeep(conn, clock.now())
    assert "laptop/old" not in _boxes(conn)
    make_session("laptop", "old")
    sender = make_session("desktop", "blog")
    assert send(conn, sender, "laptop/old", "back", None, clock.now()).id


def test_send_to_removed_mailbox_hint(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    sender = make_session("desktop", "blog")
    with pytest.raises(KampungError) as e:
        send(conn, sender, "laptop/gone", "hi", None, clock.now())
    assert e.value.code == "unknown_mailbox"
    assert "unused for about a month" in e.value.hint


def test_prune_does_not_scan_messages_per_mailbox(conn: sqlite3.Connection) -> None:
    plan = conn.execute(
        "EXPLAIN QUERY PLAN " + sweep_module.PRUNE_MAILBOXES_SQL,
        {"batch": 100, "connector": "claude"},
    ).fetchall()
    details = [r[3] for r in plan]
    assert not any("CORRELATED" in d for d in details), details


def test_the_per_read_sweep_leaves_mailboxes_alone(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    make_session("desktop", "blog")
    _bare_mailbox(conn, "laptop", "old", clock)
    sweep(conn, clock.now())
    assert "laptop/old" in _boxes(conn)
