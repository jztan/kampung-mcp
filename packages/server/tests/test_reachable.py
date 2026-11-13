"""Reachable sessions: list_peers and send say when an idle-wake session has
stopped polling (its machine is probably asleep), instead of calling it live."""

import sqlite3
from datetime import timedelta

from kampung_server import guard
from kampung_server.guard import GuardSettings
from kampung_server.limits import OWNER_BIND_WINDOW, POLL_WINDOW
from kampung_server.mail import list_peers, peek, send
from kampung_server.models import Session, load_machine, load_session
from server_testlib import FakeClock, MakeSession

G = GuardSettings()


def _phone(make_session: MakeSession) -> Session:
    return make_session(
        "laptop", repo="claude", agent="claude-app", hooks=False, cwd="Claude app"
    )


def test_list_peers_marks_a_quiet_poller_unreachable(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    make_session("desktop", "blog", idle_wake=True)
    make_session("desktop", "blog", agent="codex")
    clock.advance(minutes=6)
    laptop = load_machine(conn, "laptop")
    assert laptop is not None
    peers = list_peers(conn, laptop, clock.now(), "desktop")
    got = sorted(
        (s["agent"], s["reachable"], s["live"], s["seen"])
        for s in peers[0]["sessions"]  # type: ignore[attr-defined]
    )
    assert got == [
        ("claude-code", False, True, "6 min ago"),
        ("codex", None, True, "6 min ago"),
    ]


def test_send_to_a_quiet_poller_says_unreachable(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    target = make_session("desktop", "blog", idle_wake=True)
    clock.advance(minutes=6)
    a = make_session("laptop", "api")
    to = f"desktop/blog#{target.suffix}"
    r = send(conn, a, to, "urgent", None, clock.now()).to_dict()
    assert r["wake"] is True and r["note"] == "unreachable"
    assert r["reachable"] is False
    assert r["last_seen"] == target.last_seen and r["seen"] == "6 min ago"
    hint = str(r["hint"])
    assert "may be asleep or offline" in hint and "tell your user" in hint
    assert "budget" not in r and "reason" not in r
    assert guard.mailbox_left(conn, a.mailbox_id, G) == G.mailbox_budget


def test_the_message_wakes_the_poller_when_it_is_back(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    target = make_session("desktop", "blog", idle_wake=True)
    clock.advance(minutes=6)
    a = make_session("laptop", "api")
    send(conn, a, "desktop/blog", "urgent", None, clock.now())
    back = load_session(conn, target.id)
    assert back is not None
    assert peek(conn, back, clock.now()).wakeable == 1


def test_one_reachable_session_is_enough(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    make_session("desktop", "blog", idle_wake=True)
    clock.advance(minutes=6)
    make_session("desktop", "blog", idle_wake=True)
    a = make_session("laptop", "api")
    r = send(conn, a, "desktop/blog", "hi", None, clock.now()).to_dict()
    assert "note" not in r and "reachable" not in r
    assert guard.mailbox_left(conn, a.mailbox_id, G) == G.mailbox_budget - 1


def test_an_unknown_session_takes_the_normal_path(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    make_session("desktop", "blog", idle_wake=True)
    make_session("desktop", "blog", agent="codex")
    clock.advance(minutes=6)
    a = make_session("laptop", "api")
    r = send(conn, a, "desktop/blog", "hi", None, clock.now()).to_dict()
    assert "note" not in r and "reachable" not in r


def test_the_senders_own_session_does_not_count(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    a = make_session("laptop", "api", idle_wake=True)
    clock.advance(minutes=6)
    r = send(conn, a, "laptop/api", "note to self", None, clock.now()).to_dict()
    assert r.get("note") != "unreachable" and "reachable" not in r


def test_fyi_to_a_quiet_poller_is_unchanged(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    make_session("desktop", "blog", idle_wake=True)
    clock.advance(minutes=6)
    a = make_session("laptop", "api")
    r = send(conn, a, "desktop/blog", "fyi", None, clock.now(), mode="fyi")
    d = r.to_dict()
    assert d["reason"] == "fyi" and "reachable" not in d and "note" not in d


def test_a_claude_app_send_to_a_quiet_poller_spends_nothing(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    make_session("desktop", "blog", idle_wake=True)
    clock.advance(minutes=6)
    phone = _phone(make_session)
    small = GuardSettings(connector_wake_budget=2)
    for _ in range(3):
        r = send(conn, phone, "desktop/blog", "x", None, clock.now(), guard=small)
        assert r.note == "unreachable"
    make_session("desktop", "blog", idle_wake=True)
    r = send(conn, phone, "desktop/blog", "x", None, clock.now(), guard=small)
    assert r.wake is True and r.reason is None


def test_owner_instruction_bind_window_is_unchanged() -> None:
    assert OWNER_BIND_WINDOW == POLL_WINDOW == timedelta(minutes=2)


def test_a_targeted_send_names_the_session_in_the_hint(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    target = make_session("desktop", "blog", idle_wake=True)
    clock.advance(minutes=6)
    make_session("desktop", "blog", idle_wake=True)
    a = make_session("laptop", "api")
    to = f"desktop/blog#{target.suffix}"
    r = send(conn, a, to, "urgent", None, clock.now())
    assert r.note == "unreachable"
    assert str(r.hint).startswith(f"{to} has not been heard from")


def test_a_claude_app_send_to_a_quiet_poller_still_refills_its_mailbox(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    target = make_session("desktop", "blog", idle_wake=True)
    for _ in range(G.mailbox_budget):
        guard.spend_mailbox(conn, target.mailbox_id)
    assert guard.mailbox_left(conn, target.mailbox_id, G) == 0
    clock.advance(minutes=6)
    phone = _phone(make_session)
    r = send(conn, phone, "desktop/blog", "carry on", None, clock.now())
    assert r.note == "unreachable"
    assert guard.mailbox_left(conn, target.mailbox_id, G) == G.mailbox_budget
