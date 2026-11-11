import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from fastmcp import Client

from client_testlib import (
    LiveServer,
    db_query,
    git_repo,
    hook_chain,
    peer_send,
    run_hook,
    set_home,
    write_config,
)
from kampung_client import adapter, hook
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
    set_home(monkeypatch, desktop_home)
    blog_repo = git_repo(tmp_path, "blog")
    blog = hooked_session(blog_repo, "cc-desktop")

    set_home(monkeypatch, home)
    async with Client(build_adapter("claude-code", str(api_repo))) as c:
        sent = await c.call_tool("send", {"to": "desktop/blog", "body": "hi blog"})
        assert sent.data["wake"] is True and "depth" not in sent.data
    set_home(monkeypatch, desktop_home)
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


async def test_list_peers_include_empty_reaches_the_server(
    home: Path, server: LiveServer, tmp_path: Path
) -> None:
    repo = git_repo(tmp_path, "api")
    hooked_session(repo, "cc-1")
    old = hooked_session(git_repo(tmp_path, "old-eval"), "cc-2")
    server.admin.end_session(int(str(old["session"])), "admin:cli")
    async with Client(build_adapter("claude-code", str(repo))) as c:
        default = await c.call_tool("list_peers", {})
        shown = await c.call_tool("list_peers", {"include_empty": True})
    assert [p["mailbox"] for p in default.data["peers"]] == ["laptop/api"]
    assert [p["mailbox"] for p in shown.data["peers"]] == [
        "laptop/api",
        "laptop/old-eval",
    ]


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


def _hooks_log(home: Path) -> str:
    return (home / ".cache" / "kampung" / "hooks.log").read_text()


async def test_adapter_logs_why_the_server_was_unreachable(home: Path) -> None:
    write_config(home, "http://127.0.0.1:9", "kmp_x")
    async with Client(build_adapter("claude-code", "/w/api", wait=0)) as c:
        await c.call_tool("send", {"to": "desktop/blog", "body": "x"})
    assert "adapter send:" in _hooks_log(home)


@pytest.fixture
def answers_503() -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_POST = do_GET

        def log_message(self, *args: object) -> None:
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


async def test_adapter_reports_an_http_failure_as_its_own_error(
    home: Path, answers_503: str
) -> None:
    write_config(home, answers_503, "kmp_x")
    async with Client(build_adapter("claude-code", "/w/api", wait=0)) as c:
        r = await c.call_tool("inbox", {})
    assert r.data["error"] == "server_error"
    assert "HTTP 503" in r.data["hint"]


async def test_adapter_failure_with_a_good_key_is_not_called_unreachable(
    home: Path, server: LiveServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = git_repo(tmp_path, "api")
    hooked_session(repo, "cc-1")

    class Broken:
        def __init__(self, *args: object) -> None:
            raise RuntimeError("protocol mismatch")

    async with Client(build_adapter("claude-code", str(repo))) as c:
        monkeypatch.setattr(adapter, "Client", Broken)
        r = await c.call_tool("inbox", {})
    assert r.data["error"] == "server_error"
    assert "accepted" in r.data["hint"]
    assert "protocol mismatch" in _hooks_log(home)


async def test_adapter_lists_the_four_tools(home: Path) -> None:
    async with Client(build_adapter("claude-code", "/w/api")) as c:
        names = {t.name for t in await c.list_tools()}
    assert names == {"list_peers", "send", "inbox", "read_attachment"}


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


def codex_meta(sid: str) -> dict[str, object]:
    return {"x-codex-turn-metadata": {"session_id": sid, "thread_id": sid}}


async def test_codex_tool_call_meta_binds_the_session(
    home: Path, server: LiveServer, tmp_path: Path
) -> None:
    repo = git_repo(tmp_path, "api")
    for sid in ("cx-1", "cx-2"):
        hook.write_session_file(
            sid, hook.register("codex", str(repo), True, sid, ancestry=False)
        )
    two = hook.read_session_file("cx-2")
    assert two is not None
    peer_send(server, f"laptop/api#{two['suffix']}", "for two only")
    async with Client(build_adapter("codex", str(repo), wait=0)) as c:
        got = await c.call_tool("inbox", {}, meta=codex_meta("cx-2"))
        assert [m["body"] for m in got.data["messages"]] == ["for two only"]
    codex_rows = "SELECT count(*) FROM sessions WHERE agent = 'codex'"
    assert db_query(server, codex_rows) == [(2,)]  # no extra session registered


async def test_codex_unapproved_hooks_then_start_keeps_one_session(
    home: Path, server: LiveServer, tmp_path: Path
) -> None:
    # Tool calls before the hooks are trusted register the id; the first
    # SessionStart after trusting them revives that same session.
    repo = git_repo(tmp_path, "api")
    async with Client(build_adapter("codex", str(repo), wait=0)) as c:
        await c.call_tool("inbox", {}, meta=codex_meta("cx-9"))
    run_hook(
        home,
        {
            "hook_event_name": "SessionStart",
            "session_id": "cx-9",
            "cwd": str(repo),
            "source": "startup",
        },
        "--agent",
        "codex",
    )
    assert db_query(server, "SELECT count(*), max(hooks) FROM sessions") == [(1, 1)]


async def test_codex_malformed_meta_falls_back_to_polling(
    home: Path, server: LiveServer, tmp_path: Path
) -> None:
    repo = git_repo(tmp_path, "api")
    async with Client(build_adapter("codex", str(repo), wait=0)) as c:
        bad = {"x-codex-turn-metadata": {"session_id": 7}}
        assert "peers" in (await c.call_tool("list_peers", {}, meta=bad)).data
    assert db_query(server, "SELECT agent, hooks FROM sessions") == [("codex", 0)]


async def test_send_passes_fyi_only_when_true(
    home: Path, server: LiveServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = git_repo(tmp_path, "api")
    hooked_session(repo, "cc-1")
    peer_send(server, "laptop/api", "hello")  # registers desktop/blog
    seen: list[dict[str, object]] = []

    class Spy(Client):  # type: ignore[type-arg]
        async def call_tool(  # type: ignore[no-untyped-def]
            self, name, arguments=None, *a, **kw
        ):
            seen.append(dict(arguments or {}))
            return await super().call_tool(name, arguments, *a, **kw)

    # Only the adapter's own client to the server, not this test's client.
    monkeypatch.setattr(adapter, "Client", Spy)
    async with Client(build_adapter("claude-code", str(repo))) as c:
        plain = await c.call_tool("send", {"to": "desktop/blog", "body": "x"})
        fyi = await c.call_tool(
            "send", {"to": "desktop/blog", "body": "noted", "fyi": True}
        )
        ask = await c.call_tool(
            "send", {"to": "desktop/blog", "body": "q", "mode": "ask"}
        )
        tell = await c.call_tool(
            "send", {"to": "desktop/blog", "body": "do it", "mode": "tell"}
        )
        note = await c.call_tool(
            "send", {"to": "desktop/blog", "body": "fine", "mode": "fyi"}
        )
    assert "fyi" not in seen[0] and seen[1]["fyi"] is True
    assert plain.data["wake"] is True
    assert fyi.data["wake"] is False and fyi.data["reason"] == "fyi"
    # ask is the default and fyi has its older spelling, so both still work
    # with a server that predates mode; only tell needs a new one.
    assert "mode" not in seen[2] and "fyi" not in seen[2] and ask.data["wake"]
    assert seen[3]["mode"] == "tell" and tell.data["wake"] is True
    assert "mode" not in seen[4] and seen[4]["fyi"] is True
    assert note.data["reason"] == "fyi"


async def test_fyi_with_another_mode_is_refused(
    home: Path, server: LiveServer, tmp_path: Path
) -> None:
    repo = git_repo(tmp_path, "api")
    hooked_session(repo, "cc-1")
    async with Client(build_adapter("claude-code", str(repo))) as c:
        r = await c.call_tool(
            "send", {"to": "desktop/blog", "body": "x", "fyi": True, "mode": "tell"}
        )
    assert r.data["error"] == "mode_conflict" and "fyi" in r.data["hint"]


def test_unknown_argument_matches_the_whole_name() -> None:
    from fastmcp.exceptions import ToolError

    model = ToolError(
        "1 validation error for call[send]\nmodel\n  Unexpected keyword "
        "argument [type=unexpected_keyword_argument, input_value='x']"
    )
    assert not adapter._unknown_argument(model, "mode")
    assert adapter._unknown_argument(
        ToolError(str(model).replace("\nmodel\n", "\nmode\n")), "mode"
    )


async def test_tell_against_an_old_server_is_a_clear_error(
    home: Path, server: LiveServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastmcp.exceptions import ToolError

    repo = git_repo(tmp_path, "api")
    hooked_session(repo, "cc-1")

    class OldServer(Client):  # type: ignore[type-arg]
        async def call_tool(  # type: ignore[no-untyped-def]
            self, name, arguments=None, *a, **kw
        ):
            raise ToolError(
                "1 validation error for call[send]\nmode\n  Unexpected keyword "
                "argument [type=unexpected_keyword_argument, input_value='tell']"
            )

    monkeypatch.setattr(adapter, "Client", OldServer)
    async with Client(build_adapter("claude-code", str(repo))) as c:
        r = await c.call_tool(
            "send", {"to": "desktop/blog", "body": "do it", "mode": "tell"}
        )
    assert r.data["error"] == "mode_unsupported"
    assert "too old" in r.data["hint"] and "no reply" in r.data["hint"]


async def test_fyi_against_an_old_server_is_a_clear_error(
    home: Path, server: LiveServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastmcp.exceptions import ToolError

    repo = git_repo(tmp_path, "api")
    hooked_session(repo, "cc-1")
    calls: list[str] = []

    class OldServer(Client):  # type: ignore[type-arg]
        async def call_tool(  # type: ignore[no-untyped-def]
            self, name, arguments=None, *a, **kw
        ):
            # What a server without fyi answers (FastMCP argument validation).
            calls.append(name)
            raise ToolError(
                "1 validation error for call[send]\nfyi\n  Unexpected keyword "
                "argument [type=unexpected_keyword_argument, input_value=True]"
            )

    monkeypatch.setattr(adapter, "Client", OldServer)
    async with Client(build_adapter("claude-code", str(repo))) as c:
        r = await c.call_tool(
            "send", {"to": "desktop/blog", "body": "noted", "fyi": True}
        )
    assert r.data["error"] == "fyi_unsupported" and "too old" in r.data["hint"]
    assert calls == ["send"]


async def test_send_description_explains_the_budget_without_numbers(
    home: Path, tmp_path: Path
) -> None:
    import re

    async with Client(build_adapter("claude-code", str(tmp_path))) as c:
        tools = {t.name: t for t in await c.list_tools()}
    text = tools["send"].description or ""
    assert "fyi" in text and "wake" in text and "hint" in text
    assert '"tell"' in text and '"ask"' in text and "not woken" in text
    assert "background" in text
    budget = text[text.index("wake") :]
    assert not re.search(r"[0-9]", budget)
