"""Binding a Codex window at launch, through the daemon's control socket, so
it is listed and wakeable before its first prompt."""

import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from client_testlib import LiveServer, RawMCP, git_repo, home_env, peer_send
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


class _Closed(Exception):
    pass


def _read_frame(conn: socket.socket) -> bytes:
    b1, b2 = _recv_exact(conn, 2)
    n = b2 & 0x7F
    if n == 126:
        n = struct.unpack(">H", _recv_exact(conn, 2))[0]
    elif n == 127:
        n = struct.unpack(">Q", _recv_exact(conn, 8))[0]
    mask = _recv_exact(conn, 4) if b2 & 0x80 else b"\0\0\0\0"
    data = _recv_exact(conn, n)
    if b1 & 0x0F == 0x8:
        raise _Closed
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
        self.closes = 0  # close frames received
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
        except _Closed:
            self.closes += 1
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


def test_candidates_are_fresh_threads_in_this_folder_newest_first(
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
    pick = codex_launch.launch_candidates
    # Ids here are not UUIDv7, so the daemon's createdAt decides the time.
    assert pick(threads, str(here), not_before=100) == ["claimed", "new", "old-fresh"]
    assert pick(threads, str(other), not_before=100) == ["elsewhere"]
    assert pick(threads, str(tmp_path), not_before=100) == []
    assert pick(threads, str(here), not_before=900) == []


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


def test_a_thread_another_adapter_claimed_is_left_alone(
    home: Path, tmp_path: Path, daemon_for: Any
) -> None:
    taken, free = uuid7(time.time() + 1), uuid7(time.time())
    holder = hold_claim(home, taken)
    try:
        daemon_for([thread(taken, tmp_path, 0), thread(free, tmp_path, 0)])
        # The newest is held by another adapter, so the next one is ours.
        assert codex_launch.launch_thread_once(str(tmp_path), not_before=0) == free
    finally:
        holder.kill()
        holder.wait()


def uuid7(at: float) -> str:
    """A UUIDv7-shaped thread id for a moment, as Codex makes them."""
    ms = int(at * 1000)
    rand = os.urandom(10).hex()
    return (
        f"{ms:012x}"[:8]
        + "-"
        + f"{ms:012x}"[8:]
        + "-7"
        + rand[:3]
        + "-8"
        + rand[3:6]
        + "-"
        + rand[6:18]
    )


def test_a_thread_id_carries_its_creation_time() -> None:
    at = 1794153889.132
    assert codex_launch.thread_created_at(uuid7(at)) == pytest.approx(at, abs=0.001)
    assert codex_launch.thread_created_at("cx-launch") is None


def test_candidates_go_by_the_id_time_not_the_daemons_created_at(
    tmp_path: Path,
) -> None:
    now = time.time()
    old = uuid7(now - 600)
    new = uuid7(now)
    # The daemon reports a fresh createdAt for a thread opened long ago.
    threads = [thread(old, tmp_path, now + 5), thread(new, tmp_path, now)]
    assert codex_launch.launch_candidates(threads, str(tmp_path), now - 5) == [new]


_HOLDER = """
import sys, time
from kampung_client import codex_launch
print(codex_launch.claim_thread(sys.argv[1]), flush=True)
time.sleep(float(sys.argv[2]))
"""


def hold_claim(home: Path, tid: str, seconds: float = 30) -> "subprocess.Popen[str]":
    """Another adapter process holding a claim; returns once it answered."""
    env = {**os.environ, **home_env(home)}
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, tid, str(seconds)],
        stdout=subprocess.PIPE,
        env=env,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "True"
    return proc


def test_a_claim_held_by_another_process_is_refused_until_it_exits(
    home: Path,
) -> None:
    holder = hold_claim(home, "t-other")
    try:
        assert not codex_launch.claim_thread("t-other")
    finally:
        holder.kill()
        holder.wait()
    # The kernel dropped the dead process's lock; the file left behind is free.
    assert codex_launch.claim_thread("t-other")
    assert codex_launch.claim_thread("t-other")  # still ours


def test_an_empty_claim_file_left_behind_is_free(home: Path) -> None:
    claims = home / ".cache" / "kampung" / "codex-claims"
    claims.mkdir(parents=True)
    (claims / "t-empty").write_text("")
    assert codex_launch.claim_thread("t-empty")


def test_two_processes_racing_for_one_claim_get_one_winner(home: Path) -> None:
    env = {**os.environ, **home_env(home)}
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _HOLDER, "t-race", "2"],
            stdout=subprocess.PIPE,
            env=env,
            text=True,
        )
        for _ in range(6)
    ]
    answers = [p.communicate()[0].strip() for p in procs]
    assert sorted(answers) == ["False"] * 5 + ["True"]


def test_a_claim_is_released_when_a_tool_call_bound_first(home: Path) -> None:
    assert codex_launch.claim_thread("t-mine")
    assert not codex_launch.adopt("t-mine", lambda tid: False)
    holder = hold_claim(home, "t-mine", 1)  # free for another window again
    holder.wait()
    assert codex_launch.claim_thread("t-mine")
    assert codex_launch.adopt("t-mine", lambda tid: True)


def test_each_look_ends_with_a_close_frame(
    home: Path, tmp_path: Path, daemon_for: Any
) -> None:
    daemon = daemon_for([thread(uuid7(time.time()), tmp_path, time.time())])
    codex_launch.loaded_threads(codex_launch.control_socket())
    end = time.monotonic() + 2
    while time.monotonic() < end and daemon.closes == 0:
        time.sleep(0.05)
    assert daemon.closes == 1


def test_the_windows_own_hook_session_is_adopted_and_woken(
    home: Path, server: LiveServer, tmp_path: Path, daemon_for: Any
) -> None:
    # The sequence seen on a Mac: Codex runs the SessionStart hook as the
    # thread starts, so its session file exists before the adapter looks.
    from test_client_codex_wake import codex_start

    repo = git_repo(tmp_path, "api")
    tid = uuid7(time.time())
    hooked = codex_start(home, repo, sid=tid)
    daemon_for([thread(tid, repo, time.time())])
    codex, log = fake_codex(tmp_path)
    mcp = RawMCP(home, repo, "--codex-bin", str(codex), agent="codex")
    try:
        mcp.initialize()
        time.sleep(3)  # the adapter finds and binds the thread
        peer_send(server, f"laptop/api#{hooked['suffix']}", "review please")
        got = wait_for_calls(log, 1, 10)
        assert got == [["queue", "--thread", tid, "--message", WAKE]]
        again = hook.read_session_file(tid)
        assert again is not None and again["session"] == hooked["session"]
    finally:
        mcp.close()


def test_a_claim_still_being_written_is_not_taken(home: Path) -> None:
    # Another adapter has made the claim file and holds it, but has not
    # written its pid yet: that claim is live, not stale.
    claims = home / ".cache" / "kampung" / "codex-claims"
    claims.mkdir(parents=True)
    path = claims / "t-new"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, os, sys, time\n"
            "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "print('held', flush=True)\n"
            "time.sleep(30)\n",
            str(path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "held"
        assert path.read_text() == ""
        assert not codex_launch.claim_thread("t-new")
    finally:
        child.kill()
        child.wait()
