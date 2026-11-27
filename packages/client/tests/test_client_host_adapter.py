"""`kampung-mcp --version` and `kampung-mcp adapter --agent other`."""

import json
import threading
import time
from pathlib import Path

import pytest
from fastmcp import Client

from client_testlib import LiveServer, db_query, git_repo, set_home, write_config
from kampung_client import __version__, cli, hook, outbox
from kampung_client.adapter import _WakePoller, build_adapter


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0
    assert capsys.readouterr().out.strip() == f"kampung-mcp {__version__}"


def test_adapter_accepts_agent_other():
    args = cli._parser().parse_args(["adapter", "--agent", "other"])
    assert args.agent == "other"


def _host_session(repo: Path, path: Path) -> dict[str, object]:
    data = hook.register("other", str(repo), True, "blueclaw-x")
    path.write_text(json.dumps(data))
    return data


async def test_session_file_mode_acts_as_the_hosts_session(
    home: Path, server: LiveServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    desktop_home = tmp_path / "desktop-home"
    desktop_home.mkdir()
    write_config(desktop_home, server.url, server.keys["desktop"])
    blog_repo = git_repo(tmp_path, "blog")
    set_home(monkeypatch, desktop_home)
    blog_file = tmp_path / "blog.json"
    blog_file.write_text(
        json.dumps(hook.register("claude-code", str(blog_repo), True, "cc-blog"))
    )
    repo = git_repo(tmp_path, "bc")
    f = tmp_path / "s.json"
    set_home(monkeypatch, home)
    data = _host_session(repo, f)
    monkeypatch.setenv("KAMPUNG_SESSION_FILE", str(f))
    async with Client(build_adapter("other", str(repo))) as c:
        sent = await c.call_tool("send", {"to": "desktop/blog", "body": "hi"})
        assert "error" not in sent.data
    set_home(monkeypatch, desktop_home)
    monkeypatch.setenv("KAMPUNG_SESSION_FILE", str(blog_file))
    async with Client(build_adapter("other", str(blog_repo))) as c:
        got = await c.call_tool("inbox", {})
    assert got.data["messages"][0]["from"] == f"{data['mailbox']}#{data['suffix']}"
    assert db_query(server, "SELECT count(*) FROM sessions WHERE agent='other'") == [
        (1,)
    ]
    assert db_query(
        server, "SELECT count(*) FROM sessions WHERE agent='claude-code'"
    ) == [(1,)]


async def _end(server: LiveServer, data: dict[str, object]) -> None:
    server.admin.end_session(int(str(data["session"])), "admin:cli")


async def test_session_file_mode_retries_after_host_reregisters(
    home: Path, server: LiveServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = git_repo(tmp_path, "bc")
    f = tmp_path / "s.json"
    data = _host_session(repo, f)
    monkeypatch.setenv("KAMPUNG_SESSION_FILE", str(f))

    def revive() -> None:
        time.sleep(0.5)
        f.write_text(json.dumps(hook.register("other", str(repo), True, "blueclaw-y")))

    async with Client(build_adapter("other", str(repo))) as c:
        assert "peers" in (await c.call_tool("list_peers", {})).data
        await _end(server, data)
        t = threading.Thread(target=revive)
        t.start()
        start = time.monotonic()
        r = await c.call_tool("list_peers", {})
        elapsed = time.monotonic() - start
        t.join()
    assert "error" not in r.data
    assert elapsed < 2.5  # change detection, not the 3 s timeout


async def test_session_file_mode_gives_up_inline(
    home: Path, server: LiveServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = git_repo(tmp_path, "bc")
    f = tmp_path / "s.json"
    data = _host_session(repo, f)
    monkeypatch.setenv("KAMPUNG_SESSION_FILE", str(f))
    async with Client(build_adapter("other", str(repo))) as c:
        assert "peers" in (await c.call_tool("list_peers", {})).data
        await _end(server, data)
        start = time.monotonic()
        r = await c.call_tool("list_peers", {})
        elapsed = time.monotonic() - start
    assert r.data["error"] == "session_ended"
    assert 2.5 < elapsed < 6


async def test_session_file_mode_missing_file_is_inline_error(
    home: Path, server: LiveServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "nope.json"
    monkeypatch.setenv("KAMPUNG_SESSION_FILE", str(missing))
    async with Client(build_adapter("other", str(tmp_path))) as c:
        r = await c.call_tool("inbox", {})
    assert r.data["error"] == "session_file"
    assert str(missing) in r.data["hint"]


def test_session_file_mode_starts_no_wake_poller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KAMPUNG_SESSION_FILE", str(tmp_path / "s.json"))
    mcp = build_adapter("other", str(tmp_path))
    assert not any(isinstance(m, _WakePoller) for m in mcp.middleware)


def test_session_file_ignored_for_other_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KAMPUNG_SESSION_FILE", str(tmp_path / "s.json"))
    mcp = build_adapter("claude-code", str(tmp_path))
    assert any(isinstance(m, _WakePoller) for m in mcp.middleware)


async def test_session_file_mode_attachment_missing_file_is_inline_error(
    home: Path, server: LiveServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "nope.json"
    monkeypatch.setenv("KAMPUNG_SESSION_FILE", str(missing))
    async with Client(build_adapter("other", str(tmp_path))) as c:
        r = await c.call_tool(
            "send", {"to": "desktop/blog", "body": "x", "attachment": "a.txt"}
        )
    assert r.data["error"] == "session_file"
    assert str(missing) in r.data["hint"]


async def test_session_file_mode_outbox_follows_the_files_cwd(
    home: Path, server: LiveServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_repo = git_repo(tmp_path, "bc")
    other_dir = tmp_path / "elsewhere"
    other_dir.mkdir()
    f = tmp_path / "s.json"
    _host_session(host_repo, f)
    monkeypatch.setenv("KAMPUNG_SESSION_FILE", str(f))
    box = outbox.outbox_dir(str(host_repo))
    box.mkdir(parents=True, exist_ok=True)
    (box / "a.txt").write_text("hello")
    async with Client(build_adapter("other", str(other_dir))) as c:
        r = await c.call_tool(
            "send", {"to": "laptop/bc", "body": "x", "attachment": "a.txt"}
        )
    assert "error" not in r.data, r.data
    assert not (box / "a.txt").exists()
