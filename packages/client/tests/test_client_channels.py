from pathlib import Path

from client_testlib import LiveServer, RawMCP, git_repo, peer_send, run_hook

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
