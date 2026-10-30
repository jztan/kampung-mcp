import sqlite3
import threading
from pathlib import Path

from server_testlib import FakeClock, MakeSession
from kampung_server.mail import send
from kampung_server.sweep import start_sweeper


def test_sweeper_expires_mail_without_any_reads(
    conn: sqlite3.Connection,
    db_path: Path,
    make_session: MakeSession,
    clock: FakeClock,
) -> None:
    a, _ = make_session("laptop", "api"), make_session("desktop", "blog")
    send(conn, a, "desktop/blog", "work notes", None, clock.now())
    clock.advance(days=8)
    stop = threading.Event()
    thread = start_sweeper(db_path, clock.now, interval=0.01, stop=stop)
    try:
        for _ in range(200):
            if conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0:
                break
            stop.wait(0.01)
        assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
    finally:
        stop.set()
        thread.join(2)
    assert not thread.is_alive()


def test_sweeper_survives_a_failed_pass(tmp_path: Path, clock: FakeClock) -> None:
    stop = threading.Event()
    missing = tmp_path / "no-such-dir" / "kampung.db"
    thread = start_sweeper(missing, clock.now, interval=0.01, stop=stop)
    stop.wait(0.05)
    assert thread.is_alive()
    stop.set()
    thread.join(2)
