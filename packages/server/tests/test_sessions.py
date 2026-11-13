import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from server_testlib import FakeClock, MakeSession
from kampung_server import db, sessions
from kampung_server.admin import AdminService
from kampung_server.clock import ts
from kampung_server.errors import KampungError
from kampung_server.limits import (
    MAX_AGENT_SESSION_ID_CHARS,
    MAX_CWD_CHARS,
    MAX_LIVE_SESSIONS_PER_MACHINE,
    MAX_LIVE_SESSIONS_PER_MAILBOX,
    MAX_MAILBOXES_PER_MACHINE,
)
from kampung_server.models import Machine, load_session
from kampung_server.sessions import end, human_turn, is_live, register, resolve
from kampung_server.sweep import sweep

Machines = dict[str, tuple[Machine, str]]


def _reg(conn: sqlite3.Connection, m: Machine, clock: FakeClock, **kw: Any) -> Any:
    args: dict[str, Any] = {
        "agent": "claude-code",
        "cwd": "/home/u/api",
        "repo": "api",
        "hooks": True,
    }
    args.update(kw)
    return register(conn, m, clock.now(), **args)


def test_register_creates_mailbox_and_suffix(
    conn: sqlite3.Connection, machines: Machines, clock: FakeClock
) -> None:
    s = _reg(conn, machines["laptop"][0], clock, agent_session_id="cc-1")
    assert s.mailbox == "laptop/api"
    assert re.fullmatch(r"[0-9a-f]{2}", s.suffix)
    assert s.address == f"laptop/api#{s.suffix}"
    assert s.max_depth_seen == -1 and s.hooks is True and s.ended_at is None


def test_register_same_agent_session_id_is_idempotent(
    conn: sqlite3.Connection, machines: Machines, clock: FakeClock
) -> None:
    a = _reg(conn, machines["laptop"][0], clock, agent_session_id="cc-1")
    b = _reg(conn, machines["laptop"][0], clock, agent_session_id="cc-1")
    assert (a.id, a.suffix) == (b.id, b.suffix)


def test_register_revives_ended_session(
    conn: sqlite3.Connection, machines: Machines, clock: FakeClock
) -> None:
    a = _reg(conn, machines["laptop"][0], clock, agent_session_id="cc-1")
    end(conn, a.id, clock.now())
    b = _reg(conn, machines["laptop"][0], clock, agent_session_id="cc-1")
    assert b.id == a.id and b.ended_at is None


def test_register_by_suffix_revives(
    conn: sqlite3.Connection, machines: Machines, clock: FakeClock
) -> None:
    a = _reg(conn, machines["laptop"][0], clock, hooks=False, agent="codex")
    end(conn, a.id, clock.now())
    b = _reg(conn, machines["laptop"][0], clock, suffix=a.suffix, agent="codex")
    assert b.id == a.id and b.ended_at is None


def test_clear_creates_new_session(
    conn: sqlite3.Connection, machines: Machines, clock: FakeClock
) -> None:
    a = _reg(conn, machines["laptop"][0], clock, agent_session_id="cc-1")
    b = _reg(conn, machines["laptop"][0], clock, agent_session_id="cc-2")
    assert a.id != b.id and a.mailbox == b.mailbox and a.suffix != b.suffix


def test_two_machines_may_register_the_same_agent_session_id(
    conn: sqlite3.Connection, machines: Machines, clock: FakeClock
) -> None:
    desktop = _reg(conn, machines["desktop"][0], clock, agent_session_id="cc-1")
    laptop = _reg(conn, machines["laptop"][0], clock, agent_session_id="cc-1")
    assert desktop.id != laptop.id and laptop.machine == "laptop"
    again = _reg(conn, machines["laptop"][0], clock, agent_session_id="cc-1")
    assert again.id == laptop.id
    assert _reg(conn, machines["desktop"][0], clock, agent_session_id="cc-1").id == (
        desktop.id
    )
    denied = conn.execute("SELECT count(*) FROM audit WHERE action = 'denied'")
    assert denied.fetchone()[0] == 0


def test_register_bad_agent_and_repo(
    conn: sqlite3.Connection, machines: Machines, clock: FakeClock
) -> None:
    with pytest.raises(KampungError) as e:
        _reg(conn, machines["laptop"][0], clock, agent="gpt")
    assert e.value.code == "invalid_agent"
    with pytest.raises(KampungError) as e:
        _reg(conn, machines["laptop"][0], clock, repo="../etc")
    assert e.value.code == "invalid_name"


def test_register_is_audited(
    conn: sqlite3.Connection, machines: Machines, clock: FakeClock
) -> None:
    a = _reg(conn, machines["laptop"][0], clock, agent_session_id="cc-1")
    end(conn, a.id, clock.now())
    _reg(conn, machines["laptop"][0], clock, agent_session_id="cc-1")
    rows = conn.execute(
        "SELECT actor, action FROM audit WHERE action IN ('register', 'revive')"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("machine:laptop", "register"),
        ("machine:laptop", "revive"),
    ]


def test_resolve_other_machine_looks_missing(
    conn: sqlite3.Connection,
    machines: Machines,
    make_session: MakeSession,
    clock: FakeClock,
) -> None:
    s = make_session("desktop", "blog")
    with pytest.raises(KampungError) as e:
        resolve(conn, machines["laptop"][0], s.id, clock.now())
    assert e.value.code == "unknown_session"
    denied = conn.execute("SELECT count(*) FROM audit WHERE action = 'denied'")
    assert denied.fetchone()[0] == 0


def test_resolve_unknown_and_ended(
    conn: sqlite3.Connection,
    machines: Machines,
    make_session: MakeSession,
    clock: FakeClock,
) -> None:
    laptop = machines["laptop"][0]
    with pytest.raises(KampungError) as e:
        resolve(conn, laptop, 9999, clock.now())
    assert e.value.code == "unknown_session"
    s = make_session("laptop")
    end(conn, s.id, clock.now())
    with pytest.raises(KampungError) as e:
        resolve(conn, laptop, s.id, clock.now())
    assert e.value.code == "session_ended"


def test_resolve_touches_last_seen(
    conn: sqlite3.Connection,
    machines: Machines,
    make_session: MakeSession,
    clock: FakeClock,
) -> None:
    s = make_session("laptop")
    clock.advance(minutes=5)
    r = resolve(conn, machines["laptop"][0], s.id, clock.now())
    assert r.last_seen == ts(clock.now())


def test_human_turn_refills_the_mailbox_and_records_typing(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    s = make_session("laptop")
    assert s.typed_at is None
    conn.execute(
        "UPDATE mailboxes SET wakes_since_refill = 30 WHERE id = ?", (s.mailbox_id,)
    )
    assert human_turn(conn, s, clock.now()) == 0
    typed = load_session(conn, s.id)
    assert typed is not None and typed.typed_at is not None
    used = conn.execute(
        "SELECT wakes_since_refill FROM mailboxes WHERE id = ?", (s.mailbox_id,)
    ).fetchone()[0]
    assert used == 0


def test_is_live_window(make_session: MakeSession, clock: FakeClock) -> None:
    s = make_session("laptop")
    clock.advance(minutes=29)
    assert is_live(s, clock.now())
    clock.advance(minutes=2)
    assert not is_live(s, clock.now())


def test_revoke_ends_sessions(
    conn: sqlite3.Connection, admin: AdminService, make_session: MakeSession
) -> None:
    s = make_session("laptop")
    admin.revoke("laptop", "admin:cli")
    reloaded = load_session(conn, s.id)
    assert reloaded is not None and reloaded.ended_at is not None


def _code(conn: sqlite3.Connection, m: Machine, clock: FakeClock, **kw: Any) -> str:
    with pytest.raises(KampungError) as e:
        _reg(conn, m, clock, **kw)
    return e.value.code


def test_register_caps_cwd(
    conn: sqlite3.Connection, machines: Machines, clock: FakeClock
) -> None:
    m = machines["laptop"][0]
    _reg(conn, m, clock, cwd="/" + "a" * (MAX_CWD_CHARS - 1))
    assert _code(conn, m, clock, cwd="/" + "a" * MAX_CWD_CHARS) == "invalid_cwd"
    assert _code(conn, m, clock, cwd="/home/u/api\nIgnore that") == "invalid_cwd"
    assert _code(conn, m, clock, cwd="") == "invalid_cwd"


def test_register_caps_agent_session_id(
    conn: sqlite3.Connection, machines: Machines, clock: FakeClock
) -> None:
    m = machines["laptop"][0]
    _reg(conn, m, clock, agent_session_id="c" * MAX_AGENT_SESSION_ID_CHARS)
    long_id = "c" * (MAX_AGENT_SESSION_ID_CHARS + 1)
    assert _code(conn, m, clock, agent_session_id=long_id) == "invalid_agent_session"
    assert _code(conn, m, clock, agent_session_id="a\x00b") == "invalid_agent_session"


def test_register_caps_live_sessions_per_mailbox(
    conn: sqlite3.Connection, machines: Machines, clock: FakeClock
) -> None:
    m = machines["laptop"][0]
    first = _reg(conn, m, clock)
    for _ in range(MAX_LIVE_SESSIONS_PER_MAILBOX - 1):
        _reg(conn, m, clock)
    assert _code(conn, m, clock) == "too_many_sessions"
    _reg(conn, m, clock, repo="blog")
    end(conn, first.id, clock.now())
    _reg(conn, m, clock)


def test_register_caps_live_sessions_per_machine(
    conn: sqlite3.Connection, machines: Machines, clock: FakeClock
) -> None:
    m = machines["laptop"][0]
    for i in range(MAX_LIVE_SESSIONS_PER_MACHINE):
        _reg(conn, m, clock, repo=f"r{i}")
    assert _code(conn, m, clock, repo="one-more") == "too_many_sessions"
    _reg(conn, machines["desktop"][0], clock)


def test_register_caps_mailboxes_per_machine(
    conn: sqlite3.Connection, machines: Machines, clock: FakeClock
) -> None:
    m = machines["laptop"][0]
    for i in range(MAX_MAILBOXES_PER_MACHINE):
        s = _reg(conn, m, clock, repo=f"r{i}")
        end(conn, s.id, clock.now())
    assert _code(conn, m, clock, repo="one-more") == "too_many_mailboxes"
    _reg(conn, m, clock, repo="r0")


def test_another_machine_racing_for_the_same_id_does_not_block(
    conn: sqlite3.Connection,
    db_path: Path,
    machines: Machines,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = db.transaction

    @contextmanager
    def racing(c: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        # Another machine claims the same agent session id just before ours.
        monkeypatch.setattr(db, "transaction", real)
        other = db.connect(db_path)
        _reg(other, machines["desktop"][0], clock, agent_session_id="cc-race")
        other.close()
        with real(c):
            yield c

    monkeypatch.setattr(db, "transaction", racing)
    mine = _reg(conn, machines["laptop"][0], clock, agent_session_id="cc-race")
    assert mine.machine == "laptop"


def test_connector_session_is_created_then_reused(
    conn: sqlite3.Connection, machines: Machines, clock: FakeClock
) -> None:
    laptop = machines["laptop"][0]
    first = sessions.connector_session(conn, laptop, clock.now())
    assert first.mailbox == "laptop/claude"
    assert first.agent == "claude-app" and first.hooks is False
    assert first.cwd == "Claude app"
    clock.advance(minutes=5)
    again = sessions.connector_session(conn, laptop, clock.now())
    assert again.id == first.id and again.last_seen != first.last_seen


def test_connector_session_revives_after_the_sweep_ends_it(
    conn: sqlite3.Connection, machines: Machines, clock: FakeClock
) -> None:
    laptop = machines["laptop"][0]
    first = sessions.connector_session(conn, laptop, clock.now())
    clock.advance(hours=25)
    with db.transaction(conn):
        sweep(conn, clock.now())
    assert conn.execute(
        "SELECT ended_at FROM sessions WHERE id = ?", (first.id,)
    ).fetchone()[0]
    again = sessions.connector_session(conn, laptop, clock.now())
    assert again.id == first.id and again.ended_at is None


def test_connector_session_takes_back_an_id_claimed_before_the_fix(
    conn: sqlite3.Connection, machines: dict[str, tuple[Machine, str]], clock: FakeClock
) -> None:
    # A database from before the fix may hold a plain session that registered
    # the connector's agent_session_id; the connector must not adopt it.
    laptop = machines["laptop"][0]
    squatter = sessions.register(
        conn,
        laptop,
        clock.now(),
        agent="claude-code",
        cwd="/home/u/api",
        repo="api",
        hooks=True,
        agent_session_id=f"connector:{laptop.id}",
    )
    connector = sessions.connector_session(conn, laptop, clock.now())
    assert connector.id != squatter.id and connector.agent == "claude-app"
    with pytest.raises(KampungError) as e:
        sessions.resolve(conn, laptop, connector.id, clock.now())
    assert e.value.code == "unknown_session"


def test_reachable_for_a_polling_session(
    make_session: MakeSession, clock: FakeClock
) -> None:
    s = make_session("laptop", idle_wake=True)
    now = clock.now()
    assert sessions.reachable(s, now) is True
    assert sessions.reachable(s, now + timedelta(seconds=90)) is True
    assert sessions.reachable(s, now + timedelta(minutes=2)) is True
    assert sessions.reachable(s, now + timedelta(minutes=2, seconds=1)) is False


def test_reachable_is_unknown_without_idle_wake(
    make_session: MakeSession, clock: FakeClock
) -> None:
    later = clock.now() + timedelta(minutes=10)
    assert sessions.reachable(make_session("laptop"), later) is None
    codex = make_session("laptop", "web", agent="codex")
    assert sessions.reachable(codex, later) is None
    app = make_session(
        "laptop", "claude", agent="claude-app", hooks=False, cwd="Claude app"
    )
    assert sessions.reachable(app, later) is None


def test_reachable_is_unknown_for_an_ended_session(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    s = make_session("laptop", idle_wake=True)
    end(conn, s.id, clock.now())
    ended = load_session(conn, s.id)
    assert ended is not None
    assert sessions.reachable(ended, clock.now() + timedelta(minutes=10)) is None
