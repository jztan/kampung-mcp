"""Binding a Codex window at launch, through the daemon's control socket, so
it is listed and wakeable before its first prompt."""

import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from client_testlib import LiveServer, RawMCP, git_repo, peer_send
from kampung_client import codex_launch, hook
from test_client_codex_wake import WAKE, fake_codex, wait_for_calls

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="Codex on Windows has no launch binding"
)


def _recv_exact(conn: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise EOFError
        buf += chunk
    return buf


def _read_frame(conn: socket.socket) -> bytes:
    b1, b2 = _recv_exact(conn, 2)
    n = b2 & 0x7F
    if n == 126:
        n = struct.unpack(">H", _recv_exact(conn, 2))[0]
    elif n == 127:
        n = struct.unpack(">Q", _recv_exact(conn, 8))[0]
    mask = _recv_exact(conn, 4) if b2 & 0x80 else b"\0\0\0\0"
    data = _recv_exact(conn, n)
    return bytes(b ^ mask[i % 4] for i, b in enumerate(data))


def _write_frame(conn: socket.socket, text: str) -> None:
    data = text.encode()
    head = bytes([0x81])
    if len(data) < 126:
        head += bytes([len(data)])
    else:
        head += bytes([126]) + struct.pack(">H", len(data))
    conn.sendall(head + data)


class FakeDaemon:
    """The control socket of a Codex app-server daemon, as far as the adapter
    uses it: initialize, thread/loaded/list (two pages) and thread/read."""

    def __init__(self, home: Path, threads: list[dict[str, Any]]) -> None:
        self.threads = threads
        self.unreadable = {"gone-1"}
        self.flood = False  # send notifications forever instead of answering
        self.dir = tempfile.mkdtemp(prefix="kcs-", dir="/tmp")
        self.path = os.path.join(self.dir, "s")
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.bind(self.path)
        self.sock.listen()
        link = home / ".codex" / "app-server-control" / "app-server-control.sock"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(self.path)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            request = b""
            while b"\r\n\r\n" not in request:
                request += conn.recv(1)
            key = [
                line.split(b":", 1)[1].strip()
                for line in request.split(b"\r\n")
                if line.lower().startswith(b"sec-websocket-key")
            ][0]
            accept = base64.b64encode(
                hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest()
            )
            conn.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n"
            )
            while True:
                ids = [t["id"] for t in self.threads] + sorted(self.unreadable)
                msg = json.loads(_read_frame(conn))
                if "id" not in msg:
                    continue
                method, params = msg["method"], msg.get("params") or {}
                if self.flood and method != "initialize":
                    note = json.dumps({"method": "thread/status", "params": {}})
                    while True:
                        _write_frame(conn, note)
                if method == "initialize":
                    result: Any = {"userAgent": "fake"}
                elif method == "thread/loaded/list":
                    page = 0 if params.get("cursor") is None else 1
                    half = len(ids) // 2
                    result = {
                        "data": ids[:half] if page == 0 else ids[half:],
                        "nextCursor": "p2" if page == 0 else None,
                    }
                elif method == "thread/read":
                    found = [t for t in self.threads if t["id"] == params["threadId"]]
                    if not found:
                        _write_frame(
                            conn,
                            json.dumps(
                                {
                                    "id": msg["id"],
                                    "error": {"code": -32600, "message": "not loaded"},
                                }
                            ),
                        )
                        continue
                    result = {"thread": found[0]}
                else:
                    result = {}
                _write_frame(conn, json.dumps({"id": msg["id"], "result": result}))
        except (EOFError, OSError, IndexError, ValueError):
            pass
        finally:
            conn.close()

    def close(self) -> None:
        self.sock.close()
        shutil.rmtree(self.dir, ignore_errors=True)


def thread(
    tid: str, cwd: Path, created: float, preview: str = "", **extra: Any
) -> dict[str, Any]:
    """A thread as Codex 0.167's thread/read returns it: `turns` is always
    empty (filled only with includeTurns); a used thread has a preview."""
    return {
        "id": tid,
        "cwd": str(cwd),
        "createdAt": int(created),
        "status": {"type": "idle"},
        "turns": [],
        "preview": preview,
        "originator": "codex-tui",
        "source": "vscode",
        "ephemeral": False,
        "parentThreadId": None,
        **extra,
    }


@pytest.fixture
def daemon_for(home: Path) -> Iterator[Any]:
    made: list[FakeDaemon] = []

    def make(threads: list[dict[str, Any]]) -> FakeDaemon:
        made.append(FakeDaemon(home, threads))
        return made[-1]

    yield make
    for d in made:
        d.close()


def test_pick_takes_the_newest_fresh_unclaimed_thread_in_this_folder(
    tmp_path: Path,
) -> None:
    here, other = tmp_path / "api", tmp_path / "blog"
    threads = [
        thread("before-us", here, 50),
        thread("prompted", here, 300, preview="fix the tests"),
        thread("elsewhere", other, 400),
        thread("claimed", here, 500),
        thread("subagent", here, 600, parentThreadId="p", source={"subAgent": {}}),
        thread("ephemeral", here, 700, ephemeral=True),
        thread("old-fresh", here, 150),
        thread("new", here, 200),
    ]
    pick = codex_launch.pick_launch_thread
    assert pick(threads, str(here), {"claimed"}, not_before=100) == "new"
    assert pick(threads, str(other), set(), not_before=100) == "elsewhere"
    assert pick(threads, str(tmp_path), set(), not_before=100) is None
    assert pick(threads, str(here), {"claimed"}, not_before=900) is None


def test_loaded_threads_reads_every_page_and_skips_unreadable_ones(
    home: Path, tmp_path: Path, daemon_for: Any
) -> None:
    daemon_for([thread("a", tmp_path, 1), thread("b", tmp_path, 2)])
    got = codex_launch.loaded_threads(codex_launch.control_socket())
    assert sorted(t["id"] for t in got) == ["a", "b"]


def test_no_socket_means_no_launch_thread(home: Path, tmp_path: Path) -> None:
    assert codex_launch.launch_thread_once(str(tmp_path), not_before=0) is None


def test_a_daemon_that_never_answers_cannot_hold_a_try_past_its_budget(
    home: Path, tmp_path: Path, daemon_for: Any
) -> None:
    daemon = daemon_for([thread("a", tmp_path, time.time())])
    daemon.flood = True
    started = time.monotonic()
    assert codex_launch.launch_thread_once(str(tmp_path), not_before=0) is None
    assert time.monotonic() - started < codex_launch.TRY_BUDGET + 1


def test_codex_home_moves_the_socket(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "ch"))
    expected = tmp_path / "ch" / "app-server-control" / "app-server-control.sock"
    assert codex_launch.control_socket() == expected


def test_a_new_window_is_listed_and_woken_before_its_first_prompt(
    home: Path, server: LiveServer, tmp_path: Path, daemon_for: Any
) -> None:
    repo = git_repo(tmp_path, "api")
    # An older thread in the folder that no window claimed: never ours.
    daemon = daemon_for([thread("cx-older", repo, time.time() - 600)])
    codex, log = fake_codex(tmp_path)
    mcp = RawMCP(home, repo, "--codex-bin", str(codex), agent="codex")
    try:
        mcp.initialize()
        # The window's own thread appears only after the folder-trust prompt.
        time.sleep(2)
        daemon.threads.append(thread("cx-launch", repo, time.time()))
        end = time.monotonic() + 10
        while time.monotonic() < end and hook.read_session_file("cx-launch") is None:
            time.sleep(0.2)
        data = hook.read_session_file("cx-launch")
        assert data is not None and data["mailbox"] == "laptop/api"
        # No tool call yet, and the waker still wakes the window.
        peer_send(server, f"laptop/api#{data['suffix']}", "review please")
        got = wait_for_calls(log, 1, 8)
        assert got == [["queue", "--thread", "cx-launch", "--message", WAKE]]
        assert hook.read_session_file("cx-older") is None
    finally:
        mcp.close()


def test_a_thread_another_window_claimed_is_left_alone(
    home: Path, tmp_path: Path, daemon_for: Any
) -> None:
    hook.write_session_file("cx-taken", {"session": "1", "hooks": True})
    daemon_for([thread("cx-taken", tmp_path, time.time())])
    assert codex_launch.launch_thread_once(str(tmp_path), not_before=0) is None
