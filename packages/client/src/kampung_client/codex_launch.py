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
        """End with a WebSocket close frame (1000, normal), so the daemon
        does not log a reset for every look."""
        try:
            self.sock.settimeout(0.5)
            mask = os.urandom(4)
            payload = struct.pack(">H", 1000)
            masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            self.sock.sendall(bytes([0x88, 0x80 | len(payload)]) + mask + masked)
        except OSError:
            pass
        finally:
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


def thread_created_at(tid: str) -> float | None:
    """When a thread was made, in seconds, from its id: Codex thread ids are
    UUIDv7, whose first 48 bits are milliseconds since 1970. The daemon's own
    createdAt moves after a thread starts, so it cannot tell new from old.
    None for an id that is not UUIDv7."""
    digits = tid.replace("-", "")
    if len(digits) != 32 or digits[12] != "7":
        return None
    try:
        return int(digits[:12], 16) / 1000
    except ValueError:
        return None


def _created(t: dict[str, Any]) -> float | None:
    from_id = thread_created_at(str(t.get("id")))
    if from_id is not None:
        return from_id
    created = t.get("createdAt")
    return float(created) if isinstance(created, (int, float)) else None


def _fresh(t: dict[str, Any], cwd: str, not_before: float) -> bool:
    """A thread nobody has prompted yet, opened in this folder since this
    adapter started. `turns` cannot tell: Codex fills it only on request, so
    an empty preview (the first prompt's text) stands for "no prompt yet"."""
    source = t.get("source")
    created = _created(t)
    return (
        isinstance(t.get("id"), str)
        and _same_folder(t.get("cwd"), cwd)
        and not t.get("preview")
        and not t.get("ephemeral")
        and not t.get("parentThreadId")
        and not (isinstance(source, dict) and "subAgent" in source)
        and created is not None
        and created >= not_before
    )


def launch_candidates(
    threads: list[dict[str, Any]], cwd: str, not_before: float
) -> list[str]:
    """This window's possible threads, newest first (see _fresh). Which one
    is ours is settled by claim_thread: a hook's session file for a thread is
    not a claim, it is the same window registering early."""
    fresh = [t for t in threads if _fresh(t, cwd, not_before)]
    fresh.sort(key=lambda t: _created(t) or 0, reverse=True)
    return [str(t["id"]) for t in fresh]


def _claims_dir() -> Path:
    return hook.cache_dir() / "codex-claims"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # someone else's live process
    return True


def claim_thread(tid: str) -> bool:
    """Claim a thread for this adapter, so two adapters never bind one.
    A claim is a file holding the adapter's pid, made exclusively; a claim
    whose pid is gone is stale and taken over. True when the claim is ours."""
    path = _claims_dir() / hook._NAME_BAD.sub("_", tid)
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                holder = int(path.read_text().strip() or 0)
            except (OSError, ValueError):
                holder = 0
            if holder == os.getpid():
                return True
            if holder and _alive(holder):
                return False
            try:
                path.unlink()  # stale: its adapter is gone
            except FileNotFoundError:
                pass
            continue
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        return True
    return False


def launch_thread_once(cwd: str, not_before: float) -> str | None:
    """One look at the socket: this window's thread id, claimed, or None.
    Never raises for a socket or file problem, and ends within TRY_BUDGET."""
    path = control_socket()
    if not path.exists():
        return None
    try:
        for tid in launch_candidates(loaded_threads(path), cwd, not_before):
            if claim_thread(tid):
                return tid
    except (ControlError, OSError):
        return None
    return None
