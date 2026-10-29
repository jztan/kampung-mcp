import threading
import time
from pathlib import Path

from client_testlib import LiveServer, db_query, hook_chain
from kampung_client import hook
from kampung_client.adapter import SessionResolver, codex_session_id, pick_session

CHAIN = [(100, "a"), (50, "b"), (10, "c")]  # adapter: uv(100) <- claude(50) <- tmux(10)


def f(
    session: str,
    ancestors: list[list[object]],
    cwd: str = "/x",
    written_at: float = 1.0,
    hooks: bool = True,
    agent: str = "claude-code",
) -> dict[str, object]:
    return {
        "session": session,
        "cwd": cwd,
        "written_at": written_at,
        "hooks": hooks,
        "ancestors": ancestors,
        "agent": agent,
    }


def test_nearest_shared_ancestor_wins() -> None:
    mine = f("1", [[50, "b"], [10, "c"]])
    sibling = f("2", [[60, "z"], [10, "c"]], written_at=9)
    assert pick_session(CHAIN, "/x", [sibling, mine])["session"] == "1"


def test_pid_reuse_does_not_match() -> None:
    assert pick_session(CHAIN, "/x", [f("1", [[50, "other-start"]])]) is None


def test_tie_prefers_cwd_then_newest() -> None:
    old = f("1", [[50, "b"]], written_at=1)
    new = f("2", [[50, "b"]], written_at=2)
    assert pick_session(CHAIN, "/x", [old, new])["session"] == "2"
    elsewhere = f("3", [[50, "b"]], cwd="/elsewhere", written_at=3)
    assert pick_session(CHAIN, "/x", [old, elsewhere])["session"] == "1"


def test_no_shared_ancestor_is_ignored() -> None:
    assert pick_session(CHAIN, "/x", [f("1", [[7, "q"]])]) is None


def test_adapter_fallback_file_is_never_adopted() -> None:
    own = f("9", [[a, b] for a, b in CHAIN], hooks=False, written_at=5)
    hooked = f("1", [[50, "b"], [10, "c"]])
    assert pick_session(CHAIN, "/x", [own, hooked])["session"] == "1"
    assert pick_session(CHAIN, "/x", [own]) is None


def test_resolver_waits_for_file(home: Path, server: LiveServer) -> None:
    def later() -> None:
        time.sleep(0.5)
        hook.write_session_file("cc-1", f("77", hook_chain(), cwd="/w/api"))

    threading.Thread(target=later).start()
    got = SessionResolver("claude-code", "/w/api", wait=2.0).current()
    assert got["session"] == "77"
    assert db_query(server, "SELECT count(*) FROM sessions") == [(0,)]


def test_resolver_self_registers_after_timeout(home: Path, server: LiveServer) -> None:
    t = time.monotonic()
    got = SessionResolver("claude-code", "/w/api", wait=0.3).current()
    assert time.monotonic() - t >= 0.3
    rows = db_query(
        server,
        "SELECT agent, hooks FROM sessions WHERE id = ?",
        int(str(got["session"])),
    )
    assert rows == [("claude-code", 0)]
    assert got["hooks"] is False
    assert SessionResolver("claude-code", "/w/api", wait=0).current()["session"] != (
        got["session"]
    )  # its own fallback file is never adopted by another adapter either


def test_refresh_adopts_new_session_after_clear(home: Path, server: LiveServer) -> None:
    hook.write_session_file("cc-1", f("1", hook_chain(), written_at=1))
    r = SessionResolver("claude-code", "/x", wait=0)
    assert r.current()["session"] == "1"
    hook.delete_session_file("cc-1")
    hook.write_session_file("cc-2", f("2", hook_chain(), written_at=2))
    assert r.refresh()["session"] == "2"
    assert db_query(server, "SELECT count(*) FROM sessions") == [(0,)]


def test_refresh_never_revives(home: Path, server: LiveServer) -> None:
    r = SessionResolver("claude-code", "/w/api", wait=0)
    first = r.current()
    server.admin.end_session(int(str(first["session"])), "admin:cli")
    second = r.refresh()
    assert second["session"] != first["session"]
    ended = db_query(
        server, "SELECT ended_at FROM sessions WHERE id = ?", int(str(first["session"]))
    )
    assert ended[0][0] is not None


def test_sibling_file_alone_is_not_adopted() -> None:
    # Only another window's file exists (ours is missing): sharing a terminal
    # or editor ancestor further up must not be enough.
    sibling = f("2", [[60, "z"], [10, "c"]])
    assert pick_session(CHAIN, "/x", [sibling]) is None


def test_other_agents_file_is_not_adopted() -> None:
    claude = f("1", [[50, "b"], [10, "c"]], agent="claude-code")
    assert pick_session(CHAIN, "/x", [claude], agent="codex") is None
    assert pick_session(CHAIN, "/x", [claude], agent="claude-code") is not None


def test_clear_gap_waits_for_new_file(
    home: Path, server: LiveServer, tmp_path: Path
) -> None:
    old = hook.register("claude-code", "/w/api", True, "cc-1")
    old["ancestors"] = hook_chain()
    hook.write_session_file("cc-1", old)
    r = SessionResolver("claude-code", "/w/api", wait=2.0)
    assert r.current()["session"] == old["session"]
    hook.delete_session_file("cc-1")  # SessionEnd for the old id
    server.admin.end_session(int(str(old["session"])), "admin:cli")

    def start_new() -> None:  # SessionStart (source "clear") lands a moment later
        time.sleep(0.5)
        new = hook.register("claude-code", "/w/api", True, "cc-2")
        new["ancestors"] = hook_chain()
        hook.write_session_file("cc-2", new)

    writer = threading.Thread(target=start_new)
    writer.start()
    got = r.refresh()
    writer.join()
    assert got["session"] != old["session"] and got["hooks"] is True
    assert db_query(server, "SELECT count(*) FROM sessions WHERE hooks = 0") == [(0,)]


def test_fallback_switches_to_hook_file_when_it_appears(
    home: Path, server: LiveServer
) -> None:
    r = SessionResolver("claude-code", "/w/api", wait=0)
    assert r.current()["hooks"] is False
    later = hook.register("claude-code", "/w/api", True, "cc-1")
    later["ancestors"] = hook_chain()
    hook.write_session_file("cc-1", later)
    assert r.current()["session"] == later["session"]


def test_without_ancestry_the_only_hook_session_in_cwd_is_adopted() -> None:
    # Windows before process ancestry worked: the chain is empty.
    only = f("1", [])
    assert pick_session([], "/x", [only])["session"] == "1"


def test_without_ancestry_two_hook_sessions_in_cwd_adopt_neither() -> None:
    assert pick_session([], "/x", [f("1", []), f("2", [])]) is None


def test_without_ancestry_other_cwd_agent_or_adapter_files_are_skipped() -> None:
    files = [
        f("1", [], cwd="/elsewhere"),
        f("2", [], agent="codex"),
        f("3", [], hooks=False),
    ]
    assert pick_session([], "/x", files, agent="claude-code") is None


def test_without_ancestry_a_dead_window_does_not_count() -> None:
    live = f("1", hook_chain())
    dead = f("2", [[999999, "x"]])
    assert pick_session([], "/x", [live, dead])["session"] == "1"


def test_with_ancestry_the_fallback_is_not_used() -> None:
    # Two windows in one repo: a window whose own file is not written yet must
    # not adopt the other window's session.
    other = f("2", [[60, "z"], [10, "c"]])
    assert pick_session(CHAIN, "/x", [other]) is None


def test_hook_and_adapter_launchers_do_not_block_a_match() -> None:
    # Each side's own launcher is dropped, so both chains start at claude.exe.
    rows = {
        1500: (1532, "t1500", "python.exe"),  # hook
        1532: (9364, "t1532", "python.exe"),  # hook's launcher
        2816: (1412, "t2816", "python.exe"),  # adapter
        1412: (9364, "t1412", "python.exe"),  # adapter's launcher
        9364: (8884, "t9364", "claude.exe"),
        8884: (4, "t8884", "WindowsTerminal.exe"),
    }
    hook_file = f("1", [list(a) for a in hook._walk(1500, rows.get)])
    adapter_chain = hook._walk(2816, rows.get)
    assert pick_session(adapter_chain, "/x", [hook_file])["session"] == "1"


# Codex: bound by the session id in each tool call, never by ancestry ------


def codex_file(sid: str, cwd: str = "/w/api") -> dict[str, object]:
    data = hook.register("codex", cwd, True, sid, ancestry=False)
    hook.write_session_file(sid, data)
    return data


def test_codex_bind_uses_hook_file_for_that_id(home: Path, server: LiveServer) -> None:
    codex_file("cx-1")
    two = codex_file("cx-2")
    r = SessionResolver("codex", "/w/api", wait=0)
    assert r.bind("cx-2")["session"] == two["session"]
    assert r.bound == "cx-2"


def test_codex_bind_registers_when_file_missing(home: Path, server: LiveServer) -> None:
    got = SessionResolver("codex", "/w/api", wait=0).bind("cx-9")
    assert db_query(server, "SELECT agent, hooks, agent_session_id FROM sessions") == [
        ("codex", 1, "cx-9")
    ]
    f = hook.read_session_file("cx-9")
    assert f is not None and f["ancestors"] == [] and f["session"] == got["session"]


def test_codex_without_id_self_registers_without_hooks(
    home: Path, server: LiveServer
) -> None:
    codex_file("cx-1")
    hook.write_session_file("cc-1", f("99", hook_chain(), cwd="/w/api"))
    got = SessionResolver("codex", "/w/api", wait=0).bind(None)
    assert got["hooks"] is False
    rows = db_query(server, "SELECT agent, hooks FROM sessions ORDER BY id")
    assert rows == [("codex", 1), ("codex", 0)]


def test_codex_never_adopts_by_ancestry(home: Path, server: LiveServer) -> None:
    data = codex_file("cx-1")
    data["ancestors"] = hook_chain()  # as if it shared this process tree
    hook.write_session_file("cx-1", data)
    r = SessionResolver("codex", "/w/api", wait=0)
    assert r.target() is None
    r.bind("cx-1")
    assert r.target() is not None and r.target()["session"] == data["session"]


def test_codex_refresh_revives_the_same_session(home: Path, server: LiveServer) -> None:
    data = codex_file("cx-1")
    r = SessionResolver("codex", "/w/api", wait=0)
    r.bind("cx-1")
    server.admin.end_session(int(str(data["session"])), "admin:cli")
    assert r.refresh()["session"] == data["session"]
    assert db_query(server, "SELECT count(*), max(ended_at) FROM sessions") == [
        (1, None)
    ]


def test_codex_session_id_parsing() -> None:
    assert codex_session_id({"x-codex-turn-metadata": {"session_id": "a"}}) == "a"
    for bad in (
        None,
        {},
        {"x-codex-turn-metadata": "x"},
        {"x-codex-turn-metadata": {"session_id": 3}},
        {"x-codex-turn-metadata": {"session_id": ""}},
    ):
        assert codex_session_id(bad) is None
