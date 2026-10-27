from pathlib import Path

import os

from client_testlib import (
    LiveServer,
    RawMCP,
    db_query,
    git_repo,
    peer_send,
    run_hook,
    write_config,
)
from kampung_client import hook


def hook_chain_for_child() -> list[list[object]]:
    """What a hook run by this test process records: this process first."""
    me = hook._process(os.getpid())
    assert me is not None
    return [[os.getpid(), me[1]]] + [list(a) for a in hook.ancestors()]


REASON = "1 Kampung message(s) waiting. Call the kampung inbox tool to read them."
MODERN_DISCOVER = {
    "jsonrpc": "2.0",
    "id": "disc",
    "method": "server/discover",
    "params": {
        "_meta": {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "0"},
            "io.modelcontextprotocol/clientCapabilities": {},
        }
    },
}


def experimental(reply: dict[str, object]) -> object:
    return reply["result"]["capabilities"].get("experimental")  # type: ignore[index]


def test_channel_capability_only_with_flag(home: Path, tmp_path: Path) -> None:
    plain = RawMCP(home, tmp_path)
    try:
        assert not experimental(plain.initialize())
    finally:
        plain.close()
    channel = RawMCP(home, tmp_path, "--channels")
    try:
        assert experimental(channel.initialize()) == {"claude/channel": {}}
    finally:
        channel.close()


def test_channel_nudge_on_new_wakeable_mail(
    home: Path, server: LiveServer, tmp_path: Path
) -> None:
    repo = git_repo(tmp_path, "api")
    start = run_hook(
        home,
        {
            "hook_event_name": "SessionStart",
            "session_id": "cc-1",
            "cwd": str(repo),
            "source": "startup",
        },
    )
    assert start.returncode == 0
    mcp = RawMCP(home, repo, "--channels")
    try:
        mcp.initialize()
        mcp.send({"jsonrpc": "2.0", "id": "tl", "method": "tools/list"})
        mcp.read(10, until="tl")
        assert not [m for m in mcp.read(3) if m.get("method")]  # nothing waiting yet
        peer_send(server, "laptop/api", "hello")
        pushed = [m for m in mcp.read(8) if m.get("method")]
        assert pushed == [
            {
                "jsonrpc": "2.0",
                "method": "notifications/claude/channel",
                "params": {"content": REASON},
            }
        ]
        peer_send(server, "laptop/api", "deep one", depth_seen=3)  # wake = false
        assert not [m for m in mcp.read(5) if m.get("method")]
    finally:
        mcp.close()


def test_handshake_era_flag_accepts_initialize_after_discover(
    home: Path, tmp_path: Path
) -> None:
    default = RawMCP(home, tmp_path, "--channels")
    try:
        default.send(MODERN_DISCOVER)
        default.read(10, until="disc")
        assert "error" in default.initialize()  # the SDK locked the modern era
    finally:
        default.close()
    legacy = RawMCP(home, tmp_path, "--channels", "--handshake-era")
    try:
        legacy.send(MODERN_DISCOVER)
        answer = [m for m in legacy.read(10, until="disc") if m.get("id") == "disc"]
        assert answer and answer[0]["error"]["code"] == -32601  # type: ignore[index]
        assert experimental(legacy.initialize()) == {"claude/channel": {}}
    finally:
        legacy.close()


def hook_start(home: Path, repo: Path, sid: str = "cc-1") -> None:
    r = run_hook(
        home,
        {
            "hook_event_name": "SessionStart",
            "session_id": sid,
            "cwd": str(repo),
            "source": "startup",
        },
    )
    assert r.returncode == 0


def test_no_push_before_initialized(
    home: Path, server: LiveServer, tmp_path: Path
) -> None:
    # Review fix: a push before notifications/initialized breaks MCP ordering
    # and can be dropped, leaving startup mail unannounced.
    repo = git_repo(tmp_path, "api")
    hook_start(home, repo)
    peer_send(server, "laptop/api", "waiting at startup")
    mcp = RawMCP(home, repo, "--channels")
    try:
        mcp.send(
            {
                "jsonrpc": "2.0",
                "id": "init",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            }
        )
        mcp.read(15, until="init")
        assert not [m for m in mcp.read(4) if m.get("method")]
        mcp.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        pushed = [m for m in mcp.read(6) if m.get("method")]
        assert [m["method"] for m in pushed] == ["notifications/claude/channel"]
    finally:
        mcp.close()


def test_poller_never_registers_or_revives(
    home: Path, server: LiveServer, tmp_path: Path
) -> None:
    # Review fix: after SessionEnd (or an admin end) the poller must not
    # create a stray no-hooks session or bring the ended one back.
    repo = git_repo(tmp_path, "api")
    hook_start(home, repo)
    mcp = RawMCP(home, repo, "--channels")
    try:
        mcp.initialize()
        mcp.read(3)
        end = run_hook(
            home,
            {"hook_event_name": "SessionEnd", "session_id": "cc-1", "reason": "clear"},
        )
        assert end.returncode == 0
        mcp.read(7)
    finally:
        mcp.close()
    assert db_query(server, "SELECT count(*) FROM sessions WHERE ended_at IS NULL") == [
        (0,)
    ]


def test_server_down_is_logged_once(home: Path, tmp_path: Path) -> None:
    write_config(home, "http://127.0.0.1:9", "kmp_x")
    repo = git_repo(tmp_path, "api")
    hook.write_session_file(
        "cc-1",
        {
            "session": "1",
            "suffix": "ab",
            "cwd": str(repo),
            "written_at": 1.0,
            "hooks": True,
            "agent": "claude-code",
            "ancestors": hook_chain_for_child(),
        },
    )
    mcp = RawMCP(home, repo, "--channels")
    try:
        mcp.initialize()
        mcp.read(9)
    finally:
        mcp.close()
    log = hook.cache_dir() / "hooks.log"
    lines = [x for x in log.read_text().splitlines() if " channel:" in x]
    assert len(lines) == 1


def test_handshake_era_patch_needs_the_sdk_name(monkeypatch: object) -> None:
    import mcp.server.runner as runner

    from kampung_client.adapter import build_adapter, serve_handshake_era_only

    server = build_adapter("claude-code", "/w/api", channels=True)
    monkeypatch.delattr(runner, "_has_modern_envelope")  # type: ignore[attr-defined]
    serve_handshake_era_only(server)
    assert "server/discover" in server._mcp_server._request_handlers
