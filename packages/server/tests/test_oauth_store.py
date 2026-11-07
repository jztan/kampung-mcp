import sqlite3

from kampung_server import db, oauth_store
from kampung_server.keys import hash_key
from kampung_server.models import Machine
from server_testlib import FakeClock

Machines = dict[str, tuple[Machine, str]]


def _laptop(machines: Machines) -> int:
    return machines["laptop"][0].id


def test_connect_code_is_hashed_single_use_and_expires(
    conn: sqlite3.Connection, machines: Machines, clock: FakeClock
) -> None:
    with db.transaction(conn):
        code = oauth_store.new_connect_code(conn, _laptop(machines), clock.now())
    assert len(code) >= 22
    row = conn.execute("SELECT code_hash FROM oauth_connect_codes").fetchone()
    assert bytes(row["code_hash"]) == hash_key(code)
    with db.transaction(conn):
        assert oauth_store.take_connect_code(conn, code, clock.now()) == _laptop(
            machines
        )
        assert oauth_store.take_connect_code(conn, code, clock.now()) is None
        later = oauth_store.new_connect_code(conn, _laptop(machines), clock.now())
    clock.advance(minutes=11)
    with db.transaction(conn):
        assert oauth_store.take_connect_code(conn, later, clock.now()) is None


def test_new_connect_code_invalidates_the_previous_one(
    conn: sqlite3.Connection, machines: Machines, clock: FakeClock
) -> None:
    with db.transaction(conn):
        first = oauth_store.new_connect_code(conn, _laptop(machines), clock.now())
        second = oauth_store.new_connect_code(conn, _laptop(machines), clock.now())
        assert oauth_store.take_connect_code(conn, first, clock.now()) is None
        assert oauth_store.take_connect_code(conn, second, clock.now()) is not None


def test_request_cap_refuses_the_101st(conn: sqlite3.Connection, clock: FakeClock) -> None:
    with db.transaction(conn):
        for i in range(100):
            assert oauth_store.save_request(conn, f"r{i}", "{}", clock.now())
        assert not oauth_store.save_request(conn, "r100", "{}", clock.now())
    clock.advance(minutes=11)
    with db.transaction(conn):
        assert oauth_store.save_request(conn, "r101", "{}", clock.now())


def test_fail_request_burns_at_five(conn: sqlite3.Connection, clock: FakeClock) -> None:
    with db.transaction(conn):
        oauth_store.save_request(conn, "r", '{"a": 1}', clock.now())
        for n in range(1, 5):
            assert oauth_store.fail_request(conn, "r") == n
        assert oauth_store.load_request(conn, "r", clock.now()) == '{"a": 1}'
        assert oauth_store.fail_request(conn, "r") == 5
        assert oauth_store.load_request(conn, "r", clock.now()) is None


def test_take_request_is_single_use(conn: sqlite3.Connection, clock: FakeClock) -> None:
    with db.transaction(conn):
        oauth_store.save_request(conn, "r", "{}", clock.now())
        assert oauth_store.take_request(conn, "r", clock.now()) == "{}"
        assert oauth_store.take_request(conn, "r", clock.now()) is None


def test_expired_request_is_not_loaded(conn: sqlite3.Connection, clock: FakeClock) -> None:
    with db.transaction(conn):
        oauth_store.save_request(conn, "r", "{}", clock.now())
    clock.advance(minutes=11)
    assert oauth_store.load_request(conn, "r", clock.now()) is None


def test_replace_grant_drops_old_tokens(
    conn: sqlite3.Connection, machines: Machines, clock: FakeClock
) -> None:
    now = clock.now()
    with db.transaction(conn):
        g1 = oauth_store.replace_grant(conn, _laptop(machines), now)
        oauth_store.save_token(conn, g1, "access", "kpa_1", "{}", now + db_hour())
        g2 = oauth_store.replace_grant(conn, _laptop(machines), now)
    assert g2 != g1
    assert oauth_store.grant_for_machine(conn, _laptop(machines)) == g2
    assert oauth_store.load_token(conn, "access", "kpa_1", now) is None
    with db.transaction(conn):
        assert oauth_store.delete_grant_for_machine(conn, _laptop(machines))
        assert not oauth_store.delete_grant_for_machine(conn, _laptop(machines))


def db_hour():  # type: ignore[no-untyped-def]
    from datetime import timedelta

    return timedelta(hours=1)


def test_take_token_marks_used_once(
    conn: sqlite3.Connection, machines: Machines, clock: FakeClock
) -> None:
    now = clock.now()
    with db.transaction(conn):
        g = oauth_store.replace_grant(conn, _laptop(machines), now)
        oauth_store.save_token(conn, g, "refresh", "kpr_1", '{"x":1}', now + db_hour())
        assert oauth_store.take_token(conn, "refresh", "kpr_1", now) == (g, '{"x":1}')
        assert oauth_store.take_token(conn, "refresh", "kpr_1", now) is None
    loaded = oauth_store.load_token(conn, "refresh", "kpr_1", now)
    assert loaded is not None and loaded[0] == g and loaded[2] is not None
    assert oauth_store.load_token(conn, "refresh", "kpr_x", now) is None
    with db.transaction(conn):
        oauth_store.delete_token(conn, "refresh", "kpr_1")
    assert oauth_store.load_token(conn, "refresh", "kpr_1", now) is None


def test_live_access_hashes_follow_the_refresh_token(
    conn: sqlite3.Connection, machines: Machines, clock: FakeClock
) -> None:
    now = clock.now()
    with db.transaction(conn):
        g = oauth_store.replace_grant(conn, _laptop(machines), now)
        oauth_store.save_token(conn, g, "access", "kpa_1", "{}", now + db_hour())
        oauth_store.save_token(conn, g, "refresh", "kpr_1", "{}", now + 3 * db_hour())
    clock.advance(hours=2)
    assert hash_key("kpa_1") in oauth_store.live_access_hashes(conn, clock.now())
    clock.advance(hours=2)
    assert hash_key("kpa_1") not in oauth_store.live_access_hashes(conn, clock.now())


def test_sweep_oauth_removes_expired_rows(
    conn: sqlite3.Connection, machines: Machines, clock: FakeClock
) -> None:
    now = clock.now()
    with db.transaction(conn):
        oauth_store.new_connect_code(conn, _laptop(machines), now)
        oauth_store.save_request(conn, "r", "{}", now)
        g = oauth_store.replace_grant(conn, _laptop(machines), now)
        oauth_store.save_token(conn, g, "access", "kpa_1", "{}", now + db_hour())
        oauth_store.save_token(conn, g, "refresh", "kpr_1", "{}", now + 3 * db_hour())
    clock.advance(hours=2)
    with db.transaction(conn):
        oauth_store.sweep_oauth(conn, clock.now())
    counts = [
        conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        for t in ("oauth_connect_codes", "oauth_requests", "oauth_tokens")
    ]
    assert counts == [0, 0, 1]
