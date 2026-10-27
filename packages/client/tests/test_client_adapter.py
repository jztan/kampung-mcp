from pathlib import Path

import pytest
from fastmcp import Client

from client_testlib import LiveServer, db_query, git_repo, hook_chain, write_config
from kampung_client import hook
from kampung_client.adapter import build_adapter
from kampung_server.tools import SERVER_INSTRUCTIONS


def hooked_session(cwd: Path, sid: str) -> dict[str, object]:
    """What the SessionStart hook would leave behind for this test process tree."""
    data = hook.register("claude-code", str(cwd), True, sid)
    data["ancestors"] = hook_chain()
    hook.write_session_file(sid, data)
    return data


async def test_send_and_inbox_through_two_adapters(
    home: Path, server: LiveServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api_repo = git_repo(tmp_path, "api")
    hooked_session(api_repo, "cc-laptop")
    desktop_home = tmp_path / "desktop-home"
    desktop_home.mkdir()
    write_config(desktop_home, server.url, server.keys["desktop"])
    monkeypatch.setenv("HOME", str(desktop_home))
    blog_repo = git_repo(tmp_path, "blog")
    blog = hooked_session(blog_repo, "cc-desktop")

    monkeypatch.setenv("HOME", str(home))
    async with Client(build_adapter("claude-code", str(api_repo))) as c:
        sent = await c.call_tool("send", {"to": "desktop/blog", "body": "hi blog"})
        assert sent.data["depth"] == 0
    monkeypatch.setenv("HOME", str(desktop_home))
    async with Client(build_adapter("claude-code", str(blog_repo))) as c:
        got = await c.call_tool("inbox", {})
        assert got.data["messages"][0]["body"] == "hi blog"
        assert got.data["messages"][0]["to"] == "desktop/blog"
        peers = await c.call_tool("list_peers", {})
        assert [p["mailbox"] for p in peers.data["peers"]] == [
            "desktop/blog",
            "laptop/api",
        ]
    assert blog["mailbox"] == "desktop/blog"


async def test_admin_end_keeps_one_session_per_window(
    home: Path, server: LiveServer, tmp_path: Path
) -> None:
    repo = git_repo(tmp_path, "api")
    first = hooked_session(repo, "cc-1")
    sid = int(str(first["session"]))
    async with Client(build_adapter("claude-code", str(repo))) as c:
        assert "peers" in (await c.call_tool("list_peers", {})).data
        server.admin.end_session(sid, "admin:cli")
        assert "peers" in (await c.call_tool("list_peers", {})).data
    # The window's own session is revived; no parallel no-hooks session appears.
    assert db_query(server, "SELECT ended_at FROM sessions WHERE id = ?", sid) == [
        (None,)
    ]
    assert db_query(server, "SELECT count(*) FROM sessions WHERE hooks = 0") == [(0,)]


async def test_adapter_revoked_key_is_unauthorized(
    home: Path, server: LiveServer, tmp_path: Path
) -> None:
    repo = git_repo(tmp_path, "api")
    hooked_session(repo, "cc-1")
    server.admin.revoke("laptop", "admin:cli")
    async with Client(build_adapter("claude-code", str(repo))) as c:
        r = await c.call_tool("inbox", {})
    assert r.data == {
        "error": "unauthorized",
        "hint": "The Kampung server rejected this machine key; run kampung-mcp "
        "setup again.",
    }


async def test_adapter_server_unreachable(home: Path) -> None:
    write_config(home, "http://127.0.0.1:9", "kmp_x")
    async with Client(build_adapter("claude-code", "/w/api", wait=0)) as c:
        r = await c.call_tool("send", {"to": "desktop/blog", "body": "x"})
    assert r.data["error"] == "server_unreachable"


async def test_adapter_lists_the_three_tools(home: Path) -> None:
    async with Client(build_adapter("claude-code", "/w/api")) as c:
        names = {t.name for t in await c.list_tools()}
    assert names == {"list_peers", "send", "inbox"}


async def test_adapter_instructions_match_server(home: Path) -> None:
    async with Client(build_adapter("claude-code", "/w/api")) as c:
        assert c.initialize_result.instructions == SERVER_INSTRUCTIONS


async def test_codex_agent_registers_without_hooks(
    home: Path, server: LiveServer, tmp_path: Path
) -> None:
    repo = git_repo(tmp_path, "api")
    async with Client(build_adapter("codex", str(repo), wait=0)) as c:
        assert "peers" in (await c.call_tool("list_peers", {})).data
    assert db_query(server, "SELECT agent, hooks FROM sessions") == [("codex", 0)]
