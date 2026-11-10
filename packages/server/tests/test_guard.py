"""The loop guard's budgets: a bucket per pair of mailboxes, a counter per
mailbox, and refills."""

import sqlite3
from datetime import timedelta

from kampung_server import guard
from kampung_server.admin import AdminService
from kampung_server.guard import GuardSettings
from server_testlib import FakeClock, MakeSession

G = GuardSettings()


def _boxes(make_session: MakeSession) -> tuple[int, int]:
    a = make_session("laptop", "api")
    b = make_session("desktop", "blog")
    return a.mailbox_id, b.mailbox_id


def test_fresh_pair_has_full_budget(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    a, b = _boxes(make_session)
    assert guard.pair_left(conn, a, b, clock.now(), G) == 9


def test_nine_spends_then_empty(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    a, b = _boxes(make_session)
    lefts = [guard.spend_pair(conn, a, b, clock.now(), G) for _ in range(9)]
    assert lefts == [8, 7, 6, 5, 4, 3, 2, 1, 0]
    assert guard.pair_left(conn, b, a, clock.now(), G) == 0


def test_one_token_back_after_one_refill_interval(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    a, b = _boxes(make_session)
    for _ in range(9):
        guard.spend_pair(conn, a, b, clock.now(), G)
    clock.advance(minutes=10)
    assert guard.pair_left(conn, a, b, clock.now(), G) == 1
    clock.advance(minutes=80)
    assert guard.pair_left(conn, a, b, clock.now(), G) == 9


def test_clock_step_back_mints_nothing(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    a, b = _boxes(make_session)
    for _ in range(9):
        guard.spend_pair(conn, a, b, clock.now(), G)
    assert guard.pair_left(conn, a, b, clock.now() - timedelta(hours=1), G) == 0


def test_lowered_size_clamps(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    a, b = _boxes(make_session)
    for _ in range(9):
        guard.spend_pair(conn, a, b, clock.now(), G)
    small = GuardSettings(pair_size=3)
    assert guard.pair_left(conn, a, b, clock.now(), small) == 0
    clock.advance(minutes=60)
    assert 0 <= guard.pair_left(conn, a, b, clock.now(), small) <= 3
    clock.advance(minutes=30)
    assert guard.pair_left(conn, a, b, clock.now(), small) == 3


def test_pair_key_is_unordered_and_self_pair_works(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    assert guard.pair_key(5, 2) == guard.pair_key(2, 5) == (2, 5)
    assert guard.pair_key(3, 3) == (3, 3)
    a, _ = _boxes(make_session)
    assert guard.spend_pair(conn, a, a, clock.now(), G) == 8
    assert guard.pair_left(conn, a, a, clock.now(), G) == 8


def test_mailbox_budget_counts_down(
    conn: sqlite3.Connection, make_session: MakeSession
) -> None:
    a, _ = _boxes(make_session)
    assert guard.mailbox_left(conn, a, G) == 30
    for _ in range(30):
        guard.spend_mailbox(conn, a)
    assert guard.mailbox_left(conn, a, G) == 0
    guard.spend_mailbox(conn, a)
    assert guard.mailbox_left(conn, a, G) == 0


def test_refill_mailbox_clears_pairs_and_counter(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    a, b = _boxes(make_session)
    for _ in range(9):
        guard.spend_pair(conn, a, b, clock.now(), G)
        guard.spend_mailbox(conn, a)
    assert guard.refill_mailbox(conn, b) == 1
    assert guard.pair_left(conn, a, b, clock.now(), G) == 9
    assert guard.mailbox_left(conn, b, G) == 30
    # The mailbox that typed is the one refilled; a's own count stays.
    assert guard.mailbox_left(conn, a, G) == 21
    assert guard.refill_mailbox(conn, a) == 0
    assert guard.mailbox_left(conn, a, G) == 30


def test_refill_reachable_from_skips_unpaired_machines(
    conn: sqlite3.Connection,
    admin: AdminService,
    make_session: MakeSession,
    clock: FakeClock,
) -> None:
    admin.add_machine("phone", "admin:cli")
    admin.add_machine("work", "admin:cli")
    admin.pair("phone", "laptop", "admin:cli")
    admin.pair("work", "laptop", "admin:cli")
    phone = conn.execute("SELECT id FROM machines WHERE name = 'phone'").fetchone()[0]
    api = make_session("laptop", "api").mailbox_id
    other = make_session("work", "x").mailbox_id
    blog = make_session("desktop", "blog").mailbox_id
    admin.pair("phone", "desktop", "admin:cli")
    for _ in range(9):
        guard.spend_pair(conn, api, other, clock.now(), G)
        guard.spend_pair(conn, api, blog, clock.now(), G)
    assert guard.refill_mailbox(conn, api, reachable_from=phone) == 1
    assert guard.pair_left(conn, api, blog, clock.now(), G) == 9
    assert guard.pair_left(conn, api, other, clock.now(), G) == 0


def test_sweep_deletes_full_rows_only(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    from kampung_server.sweep import sweep

    a, b = _boxes(make_session)
    guard.spend_pair(conn, a, b, clock.now(), G)
    sweep(conn, clock.now())
    assert conn.execute("SELECT count(*) FROM pair_budgets").fetchone()[0] == 1
    clock.advance(minutes=10)
    sweep(conn, clock.now())
    assert conn.execute("SELECT count(*) FROM pair_budgets").fetchone()[0] == 0


def test_cap_pairs_holds_old_debt_to_the_new_interval(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    a, b = _boxes(make_session)
    slow = GuardSettings(pair_refill=timedelta(minutes=1440))
    for _ in range(9):
        guard.spend_pair(conn, a, b, clock.now(), slow)
    # Restarted with a 10 minute refill: the pair owes 90 minutes at most.
    guard.cap_pairs(conn, clock.now(), G)
    assert guard.pair_left(conn, a, b, clock.now(), G) == 0
    clock.advance(minutes=10)
    assert guard.pair_left(conn, a, b, clock.now(), G) == 1


def test_cap_pairs_leaves_debt_within_the_interval_alone(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    a, b = _boxes(make_session)
    for _ in range(3):
        guard.spend_pair(conn, a, b, clock.now(), G)
    guard.cap_pairs(conn, clock.now(), G)
    assert guard.pair_left(conn, a, b, clock.now(), G) == 6
