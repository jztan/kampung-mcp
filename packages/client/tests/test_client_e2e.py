import json
import sys
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from client_testlib import (
    LiveServer,
    db_query,
    git_repo,
    home_env,
    peer_send,
    run_hook,
)
from kampung_client import hook

REASON = "1 Kampung message(s) waiting. Call the kampung inbox tool to read them."


async def test_hook_and_adapter_exchange(
    home: Path, server: LiveServer, tmp_path: Path
) -> None:
    repo = git_repo(tmp_path, "api")
    start = run_hook(
        home,
        {
            "hook_event_name": "SessionStart",
            "session_id": "cc-laptop",
            "cwd": str(repo),
            "source": "startup",
        },
    )
    assert start.returncode == 0 and start.stdout == ""
    session = hook.read_session_file("cc-laptop")
    assert session is not None

    peer_send(server, "laptop/api", "build is green")
    stop = run_hook(
        home,
        {
            "hook_event_name": "Stop",
            "session_id": "cc-laptop",
            "stop_hook_active": False,
        },
    )
    assert json.loads(stop.stdout) == {"decision": "block", "reason": REASON}

    transport = StdioTransport(
        sys.executable,
        ["-m", "kampung_client", "adapter", "--agent", "claude-code"],
        env=home_env(home),
        cwd=str(repo),
    )
    async with Client(transport) as c:
        got = await c.call_tool("inbox", {})
    msg = got.data["messages"][0]
    assert msg["body"] == "build is green"
    assert str(msg["from"]).startswith("desktop/blog#")
    delivered = db_query(server, "SELECT delivered_to FROM messages")
    assert delivered == [
        (int(str(session["session"])),)
    ]  # the hook's session, not a new one
    assert db_query(server, "SELECT count(*) FROM sessions WHERE hooks = 0") == [(0,)]
