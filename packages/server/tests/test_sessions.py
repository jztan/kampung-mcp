import re
import sqlite3
from typing import Any

import pytest

from server_testlib import FakeClock, MakeSession
from kampung_server.admin import AdminService
from kampung_server.clock import ts
from kampung_server.errors import KampungError
from kampung_server.models import Machine, load_session
from kampung_server.sessions import end, human_turn, is_live, register, resolve

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


def test_register_other_machines_agent_session_id(
    conn: sqlite3.Connection, machines: Machines, clock: FakeClock
) -> None:
    _reg(conn, machines["desktop"][0], clock, agent_session_id="cc-1")
    with pytest.raises(KampungError) as e:
        _reg(conn, machines["laptop"][0], clock, agent_session_id="cc-1")
    assert e.value.code == "not_owner"


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


def test_resolve_other_machine_is_not_owner(
    conn: sqlite3.Connection,
    machines: Machines,
    make_session: MakeSession,
    clock: FakeClock,
) -> None:
    s = make_session("desktop", "blog")
    with pytest.raises(KampungError) as e:
        resolve(conn, machines["laptop"][0], s.id, clock.now())
    assert e.value.code == "not_owner"
    denied = conn.execute("SELECT count(*) FROM audit WHERE action = 'denied'")
    assert denied.fetchone()[0] == 1


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


def test_human_turn_resets_depth(
    conn: sqlite3.Connection, make_session: MakeSession
) -> None:
    s = make_session("laptop")
    conn.execute("UPDATE sessions SET max_depth_seen = 4 WHERE id = ?", (s.id,))
    human_turn(conn, s)
    reloaded = load_session(conn, s.id)
    assert reloaded is not None and reloaded.max_depth_seen == -1


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


def test_register_not_owner_is_audited(
    conn: sqlite3.Connection, machines: Machines, clock: FakeClock
) -> None:
    _reg(conn, machines["desktop"][0], clock, agent_session_id="cc-1")
    with pytest.raises(KampungError):
        _reg(conn, machines["laptop"][0], clock, agent_session_id="cc-1")
    denied = conn.execute("SELECT actor FROM audit WHERE action = 'denied'")
    assert [r[0] for r in denied] == ["machine:laptop"]
