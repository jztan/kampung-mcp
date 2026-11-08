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

CALL_TIMEOUT = 2.0  # seconds for each request on the control socket
LAUNCH_TRIES = 10
LAUNCH_DELAY = 0.5  # seconds between tries: the thread may appear just after us


class ControlError(Exception):
    """The control socket is missing, refused, or answered something else."""


def control_socket() -> Path:
    base = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
    return Path(base) / "app-server-control" / "app-server-control.sock"


class _Conn:
    def __init__(self, path: Path, timeout: float) -> None:
        # Resolved first: Codex links this path to a short one under /tmp,
        # because Unix socket paths are limited to about 104 bytes.
        target = os.path.realpath(path)
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(timeout)
        self.next_id = 0
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

    def _exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
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
            if message.get("id") != rid:
                continue  # a notification, or another request's answer
            if "error" in message:
                raise ControlError(f"{method}: {message['error']}")
            result = message.get("result")
            return result if isinstance(result, dict) else {}

    def notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def close(self) -> None:
        self.sock.close()


def _open(path: Path, timeout: float) -> _Conn:
    conn = _Conn(path, timeout)
    try:
        conn.call("initialize", {"clientInfo": {"name": "kampung", "version": "1"}})
        conn.notify("initialized")
    except (ControlError, OSError):
        conn.close()
        raise
    return conn


def socket_answers(path: Path | None = None, timeout: float = CALL_TIMEOUT) -> bool:
    """Whether the control socket accepts a client (for doctor)."""
    try:
        _open(path or control_socket(), timeout).close()
        return True
    except (ControlError, OSError):
        return False


def loaded_threads(path: Path, timeout: float = CALL_TIMEOUT) -> list[dict[str, Any]]:
    """Every thread the daemon has loaded, as thread/read returns it. One that
    goes away between the list and the read is skipped."""
    conn = _open(path, timeout)
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


def pick_launch_thread(
    threads: list[dict[str, Any]], cwd: str, claimed: set[str]
) -> str | None:
    """This window's thread: in this folder, no turns yet, not bound by any
    other window or hook; the newest, if several. Two windows opened in one
    folder within seconds can still pick the same one (accepted: the other
    binds on its first prompt, and no mail is lost)."""
    fresh = [
        t
        for t in threads
        if _same_folder(t.get("cwd"), cwd)
        and not t.get("turns")
        and isinstance(t.get("id"), str)
        and t["id"] not in claimed
    ]
    if not fresh:
        return None
    newest = max(fresh, key=lambda t: t.get("createdAt") or 0)
    return str(newest["id"])


def claimed_ids() -> set[str]:
    """Agent session ids that already have a session file: bound elsewhere."""
    try:
        return {p.stem for p in hook.sessions_dir().glob("*.json")}
    except OSError:
        return set()


def find_launch_thread(
    cwd: str, tries: int = LAUNCH_TRIES, delay: float = LAUNCH_DELAY
) -> str | None:
    """This window's thread id, or None when the socket cannot say."""
    path = control_socket()
    for attempt in range(tries):
        if attempt:
            time.sleep(delay)
        if not path.exists():
            continue
        try:
            found = pick_launch_thread(loaded_threads(path), cwd, claimed_ids())
        except (ControlError, OSError):
            continue
        if found is not None:
            return found
    return None
