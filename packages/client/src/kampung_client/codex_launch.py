"""Find a Codex window's thread at launch, before its first prompt.

Codex gives the adapter its thread id only with the first tool call, so a
freshly opened window would be unlisted and unwakeable until someone typed
in it. The shared app-server daemon already holds the window's thread, and
its control socket lists it. This asks the socket, read-only, and picks the
thread that is this window's. Any failure means no launch thread: the adapter
then binds on the first tool call, as before.

The control socket speaks JSON-RPC over WebSocket on a Unix socket. Codex
marks that protocol experimental, so everything here fails open.
"""

import base64
import json
import os
import socket
import struct
import time
from pathlib import Path
from typing import Any

from . import hook

TRY_BUDGET = 3.0  # seconds one look at the socket may take, start to finish
# The window's thread appears only once the folder-trust prompt is answered,
# which can take a person a while, so the adapter keeps looking this long.
LAUNCH_WINDOW = 120.0
LAUNCH_DELAY = 1.0  # seconds between looks
# A thread created before the adapter started belongs to another window or an
# earlier run; this much slack allows for clocks rounding to the second.
CREATED_SLACK = 5.0


class ControlError(Exception):
    """The control socket is missing, refused, or answered something else."""


def control_socket() -> Path:
    base = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
    return Path(base) / "app-server-control" / "app-server-control.sock"


class _Conn:
    def __init__(self, path: Path, deadline: float) -> None:
        # Resolved first: Codex links this path to a short one under /tmp,
        # because Unix socket paths are limited to about 104 bytes.
        target = os.path.realpath(path)
        self.sock = socket.socket(socket.AF_UNIX)
        self.deadline = deadline
        self.next_id = 0
        self._tick()
        try:
            self.sock.connect(target)
            key = base64.b64encode(os.urandom(16)).decode()
            self.sock.sendall(
                (
                    "GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                    f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                    "Sec-WebSocket-Version: 13\r\n\r\n"
                ).encode()
            )
            head = b""
            while b"\r\n\r\n" not in head:
                head += self._exact(1)
                if len(head) > 8192:
                    raise ControlError("handshake too long")
            if b" 101 " not in head.split(b"\r\n", 1)[0]:
                raise ControlError("no WebSocket upgrade")
        except OSError as e:
            self.sock.close()
            raise ControlError(str(e)) from e
        except ControlError:
            self.sock.close()
            raise

    def _tick(self) -> None:
        """Hold every read and write to the one deadline, however many
        notifications the daemon sends meanwhile."""
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise ControlError("out of time")
        self.sock.settimeout(left)

    def _exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            self._tick()
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ControlError("socket closed")
            buf += chunk
        return buf

    def _send(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode()
        mask = os.urandom(4)
        head = bytes([0x81])
        if len(data) < 126:
            head += bytes([0x80 | len(data)])
        elif len(data) < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", len(data))
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", len(data))
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(head + mask + masked)

    def _frame(self) -> tuple[int, bytes]:
        b1, b2 = self._exact(2)
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._exact(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._exact(8))[0]
        mask = self._exact(4) if b2 & 0x80 else b"\0\0\0\0"
        data = self._exact(n)
        return b1 & 0x0F, bytes(b ^ mask[i % 4] for i, b in enumerate(data))

    def _message(self) -> dict[str, Any]:
        parts: list[bytes] = []
        while True:
            op, data = self._frame()
            if op == 0x8:
                raise ControlError("socket closed by the daemon")
            if op in (0x9, 0xA):  # ping, pong: not ours to answer here
                continue
            parts.append(data)
            if op in (0x1, 0x0) and data is not None:
                try:
                    message = json.loads(b"".join(parts))
                except ValueError:
                    continue  # a fragmented message: read on
                if isinstance(message, dict):
                    return message
                parts = []

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.next_id += 1
        rid = self.next_id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        while True:
            message = self._message()
            if "method" in message or message.get("id") != rid:
                continue  # a notification, a request to us, or not ours
            if "error" in message:
                raise ControlError(f"{method}: {message['error']}")
            result = message.get("result")
            return result if isinstance(result, dict) else {}

    def notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def close(self) -> None:
        self.sock.close()


def _open(path: Path, deadline: float) -> _Conn:
    conn = _Conn(path, deadline)
    try:
        conn.call("initialize", {"clientInfo": {"name": "kampung", "version": "1"}})
        conn.notify("initialized")
    except (ControlError, OSError):
        conn.close()
        raise
    return conn


def probe_socket(path: Path | None = None) -> str:
    """For doctor: "answers", "not running" (no daemon behind the socket:
    gone, or a file left behind), or "broken" (a daemon that will not talk)."""
    try:
        _open(path or control_socket(), time.monotonic() + TRY_BUDGET).close()
        return "answers"
    except ControlError as e:
        cause = e.__cause__
        if isinstance(cause, (FileNotFoundError, ConnectionRefusedError)):
            return "not running"
        return "broken"
    except OSError:
        return "broken"


def loaded_threads(path: Path, budget: float = TRY_BUDGET) -> list[dict[str, Any]]:
    """Every thread the daemon has loaded, as thread/read returns it. One that
    goes away between the list and the read is skipped. The whole call ends
    within `budget` seconds or raises ControlError."""
    conn = _open(path, time.monotonic() + budget)
    try:
        ids: list[str] = []
        cursor: str | None = None
        for _ in range(50):  # pages; a daemon holds few threads
            page = conn.call("thread/loaded/list", {"cursor": cursor} if cursor else {})
            ids += [i for i in page.get("data", []) if isinstance(i, str)]
            cursor = page.get("nextCursor")
            if not isinstance(cursor, str):
                break
        threads = []
        for tid in ids:
            try:
                found = conn.call("thread/read", {"threadId": tid}).get("thread")
            except ControlError:
                continue
            if isinstance(found, dict):
                threads.append(found)
        return threads
    finally:
        conn.close()


def _same_folder(a: object, b: str) -> bool:
    return isinstance(a, str) and hook._norm(a) == hook._norm(b)


def _fresh(t: dict[str, Any], cwd: str, not_before: float) -> bool:
    """A thread nobody has prompted yet, opened in this folder since this
    adapter started. `turns` cannot tell: Codex fills it only on request, so
    an empty preview (the first prompt's text) stands for "no prompt yet"."""
    source = t.get("source")
    return (
        _same_folder(t.get("cwd"), cwd)
        and not t.get("preview")
        and not t.get("ephemeral")
        and not t.get("parentThreadId")
        and not (isinstance(source, dict) and "subAgent" in source)
        and isinstance(t.get("createdAt"), (int, float))
        and t["createdAt"] >= not_before
    )


def pick_launch_thread(
    threads: list[dict[str, Any]], cwd: str, claimed: set[str], not_before: float
) -> str | None:
    """This window's thread: fresh (see _fresh), not bound by any other
    window or hook; the newest, if several. Two windows opened in one folder
    within seconds can still pick the same one (accepted: the other binds on
    its first prompt, and no mail is lost)."""
    fresh = [
        t
        for t in threads
        if isinstance(t.get("id"), str)
        and t["id"] not in claimed
        and _fresh(t, cwd, not_before)
    ]
    if not fresh:
        return None
    return str(max(fresh, key=lambda t: t["createdAt"])["id"])


def claimed_ids() -> set[str]:
    """Agent session ids that already have a session file: bound elsewhere."""
    try:
        return {p.stem for p in hook.sessions_dir().glob("*.json")}
    except OSError:
        return set()


def launch_thread_once(cwd: str, not_before: float) -> str | None:
    """One look at the socket: this window's thread id, or None. Never raises
    for a socket problem, and ends within TRY_BUDGET seconds."""
    path = control_socket()
    if not path.exists():
        return None
    try:
        return pick_launch_thread(loaded_threads(path), cwd, claimed_ids(), not_before)
    except (ControlError, OSError):
        return None
