"""Kampung hook script and shared client library. Standard library only.

Claude Code runs this file in place (`kampung-mcp setup` points the hooks at
it), on every hook event, so it must start fast and must never block or break
the agent: every failure is logged and swallowed.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TextIO

Ancestor = tuple[int, str]

_NAME_BAD = re.compile(r"[^A-Za-z0-9._-]")

HTTP_TIMEOUT = 2.0
HOOK_DEADLINE = 4.0
NUDGE_INTERVAL = 30.0  # seconds between mid-turn peeks per session
# A Codex turn mark with no hook activity for this long counts as idle: an
# interrupted turn may never fire Stop.
TURN_STALE = 600.0
CHANNEL_PREFIX = "<channel"  # how a Channels push arrives as a UserPromptSubmit
WAKE_PREFIX = "[kampung]"  # how a `codex queue` wake arrives as a UserPromptSubmit
AGENTS = ("claude-code", "codex")
CODEX_END_DEADLINE = 2.5  # Codex cancels SessionEnd hooks at 3 s
SESSION_FILE_MAX_AGE = 7 * 86400  # Codex files have no ancestry to sweep by
CODEX_START_CONTEXT = (
    "Kampung is connected. Call the kampung inbox tool once now to check for "
    "peer messages."
)
STOP_REASON = (
    "{n} Kampung message(s) waiting. Call the kampung inbox tool to read them."
)


class HookError(Exception):
    """Anything that should make a hook give up quietly."""


class InsecureServerError(HookError):
    """The configured server would get the machine key over plain http."""


# Plain http is only for loopback, whose traffic never leaves the machine.
# Tailscale addresses need --allow-insecure-http: 100.64.0.0/10 is also the
# carrier-grade NAT range, so off Tailscale it can reach a host inside the
# ISP, and a *.ts.net name falls back to public DNS when Tailscale is off.
_PLAIN_HTTP_NETS = tuple(ipaddress.ip_network(n) for n in ("127.0.0.0/8", "::1/128"))


def _plain_http_ok(host: str) -> bool:
    """Checked as written, never resolved: a name's DNS answer can change."""
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(address in net for net in _PLAIN_HTTP_NETS)


def server_url_problem(url: str, allow_insecure_http: bool = False) -> str | None:
    """Why the machine key must not be sent to `url`, or None if it may."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname  # lower-cased, IPv6 brackets removed
    except ValueError:  # a malformed IPv6 literal
        return f"{url!r} is not a valid server URL."
    if parts.scheme not in ("https", "http") or not host:
        return f"{url!r} is not an https:// or http:// server URL."
    if parts.scheme == "http" and not allow_insecure_http and not _plain_http_ok(host):
        return (
            f"Refusing to send the key over plain http to {host}; use https:// "
            "(see the tls profile), or pass --allow-insecure-http to setup for "
            "a network you trust, such as Tailscale."
        )
    return None


def config_dir() -> Path:
    return Path.home() / ".config" / "kampung"


def cache_dir() -> Path:
    return Path.home() / ".cache" / "kampung"


def sessions_dir() -> Path:
    return cache_dir() / "sessions"


def _read_config() -> dict[str, object]:
    path = config_dir() / "config.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise HookError(f"cannot read {path}: {e}") from e
    if not isinstance(data, dict) or not data.get("server") or not data.get("key"):
        raise HookError(f"{path} needs both server and key")
    return data


def load_config() -> dict[str, str]:
    """Server and key, refusing a server the key must not be sent to (this
    also covers a config.json edited by hand or written by an older version)."""
    data = _read_config()
    server = str(data["server"]).rstrip("/")
    problem = server_url_problem(server, data.get("allow_insecure_http") is True)
    if problem is not None:
        raise InsecureServerError(problem)
    return {"server": server, "key": str(data["key"])}


def insecure_http_warning() -> str | None:
    """A warning when config.json relies on allow_insecure_http, else None."""
    try:
        data = _read_config()
    except HookError:
        return None
    server = str(data["server"]).rstrip("/")
    relied_on = server_url_problem(server) and not server_url_problem(server, True)
    if data.get("allow_insecure_http") is not True or not relied_on:
        return None
    return (
        f"Warning: allow_insecure_http is set, so the machine key goes to "
        f"{server} over plain http, where anyone on the network path can copy it."
    )


ProcessInfo = tuple[int, str, str]  # (parent pid, start time, exe name)

# Skipped in a chain: agents run hook commands through a shell (Git Bash on
# Windows, /bin/sh elsewhere), so a hook's parent is a short-lived shell that
# the adapter's chain never has.
SHELLS = frozenset(
    {"bash.exe", "sh.exe", "cmd.exe", "powershell.exe", "pwsh.exe"}
    | {"sh", "bash", "zsh", "dash"}
)
# Skipped at the bottom of a chain only: a Windows venv's Scripts\python.exe is
# a launcher that runs the real interpreter as its child.
LAUNCHERS = frozenset({"python.exe", "pythonw.exe"})
MAX_ANCESTORS = 64


def _ps_lookup(pid: int) -> ProcessInfo | None:
    try:
        out = subprocess.run(
            ["ps", "-o", "ppid=,lstart=,comm=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
            # Hooks and the adapter may run with different locales, and
            # lstart is locale-formatted; pin it so start times compare equal.
            env={**os.environ, "LC_ALL": "C"},
        ).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return None
    # lstart is five words in the C locale; comm is a path (login shells
    # start with "-") and may contain spaces.
    if len(out) < 7:
        return None
    name = os.path.basename(" ".join(out[6:])).lstrip("-")
    return int(out[0]), " ".join(out[1:6]), name


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    _k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [
        ctypes.POINTER(wintypes.FILETIME)
    ] * 4
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _SNAPPROCESS = 0x2
    _QUERY_LIMITED = 0x1000
    _INVALID_HANDLE = wintypes.HANDLE(-1).value

    def _win_start(pid: int) -> str | None:
        handle = _k32.OpenProcess(_QUERY_LIMITED, False, pid)
        if not handle:
            return None
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not _k32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return None
            created = times[0]
            return str(created.dwHighDateTime << 32 | created.dwLowDateTime)
        finally:
            _k32.CloseHandle(handle)

    def _win_lookup(pid: int) -> ProcessInfo | None:
        snap = _k32.CreateToolhelp32Snapshot(_SNAPPROCESS, 0)
        if not snap or snap == _INVALID_HANDLE:
            return None
        try:
            entry = _ProcessEntry()
            entry.dwSize = ctypes.sizeof(_ProcessEntry)
            ok = _k32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                if entry.th32ProcessID == pid:
                    start = _win_start(pid)
                    if start is None:
                        return None
                    return int(entry.th32ParentProcessID), start, entry.szExeFile
                ok = _k32.Process32NextW(snap, ctypes.byref(entry))
            return None
        finally:
            _k32.CloseHandle(snap)


def _lookup(pid: int) -> ProcessInfo | None:
    if sys.platform == "win32":
        return _win_lookup(pid)
    return _ps_lookup(pid)


def _process(pid: int) -> tuple[int, str] | None:
    """(parent pid, start time) for a live process, or None."""
    info = _lookup(pid)
    return None if info is None else (info[0], info[1])


def _walk(pid: int, lookup: Callable[[int], ProcessInfo | None]) -> list[Ancestor]:
    """(pid, start time) of every ancestor of `pid`, nearest first, without pid 1.

    Shells are left out, and so are Python launchers below the first kept
    ancestor. Stops at a pid loop (Windows reuses pids) or after MAX_ANCESTORS.
    """
    info = lookup(pid)
    chain: list[Ancestor] = []
    seen = {pid}
    current = info[0] if info else 0
    while current > 1 and current not in seen and len(seen) <= MAX_ANCESTORS:
        seen.add(current)
        parent = lookup(current)
        if parent is None:
            break
        name = parent[2].lower()
        if name not in SHELLS and (chain or name not in LAUNCHERS):
            chain.append((current, parent[1]))
        current = parent[0]
    return chain


def ancestors(pid: int | None = None) -> list[Ancestor]:
    """(pid, start time) of every ancestor of `pid`, nearest first, without pid 1."""
    return _walk(os.getpid() if pid is None else pid, _lookup)


def repo_name(cwd: str) -> str:
    """The mailbox repo name: the git worktree's directory name, made valid."""
    name = os.path.basename(os.path.abspath(cwd))
    try:
        top = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if top.returncode == 0 and top.stdout.strip():
            name = os.path.basename(top.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    cleaned = _NAME_BAD.sub("-", name)
    cleaned = re.sub(r"^[^A-Za-z0-9]+", "", cleaned)[:64]
    return cleaned or "root"


def _session_path(agent_session_id: str) -> Path:
    safe = _NAME_BAD.sub("_", agent_session_id)
    return sessions_dir() / f"{safe}.json"


def write_session_file(agent_session_id: str, data: dict[str, object]) -> Path:
    path = _session_path(agent_session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)
    return path


def read_session_file(agent_session_id: str) -> dict[str, object] | None:
    try:
        data = json.loads(_session_path(agent_session_id).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def delete_session_file(agent_session_id: str) -> None:
    try:
        _session_path(agent_session_id).unlink()
    except FileNotFoundError:
        pass


def list_session_files() -> list[dict[str, object]]:
    files = []
    for path in sorted(sessions_dir().glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            files.append(data)
    return files


def sweep_stale_files() -> int:
    """Delete session files whose nearest recorded ancestor is no longer alive."""
    removed = 0
    now = time.time()
    for path in sessions_dir().glob("*.json"):
        try:
            data = json.loads(path.read_text())
            if data.get("agent") == "codex":
                if now - float(data.get("written_at", now)) > SESSION_FILE_MAX_AGE:
                    path.unlink(missing_ok=True)
                    removed += 1
                continue
            pid, start = data["ancestors"][0]
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            continue
        live = _process(int(pid))
        if live is None or live[1] != start:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Hand a redirect back as an HTTPError: urllib would re-send the
    Authorization header wherever it points, plain http included."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def open_url(request: urllib.request.Request, timeout: float) -> Any:
    """urlopen, without following redirects."""
    return _OPENER.open(request, timeout=timeout)


def api(
    method: str,
    path: str,
    body: dict[str, object] | None = None,
    timeout: float = 2.0,
) -> tuple[int, dict[str, object]]:
    """Call the Kampung server. 4xx comes back as (status, body); 5xx and
    network failures raise HookError."""
    config = load_config()
    request = urllib.request.Request(
        config["server"] + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {config['key']}",
            "Content-Type": "application/json",
        },
    )
    try:
        with open_url(request, timeout) as response:
            return response.status, _json(response.read())
    except urllib.error.HTTPError as e:
        if e.code >= 500:
            raise HookError(f"{method} {path}: HTTP {e.code}") from e
        return e.code, _json(e.read())
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise HookError(f"{method} {path}: {e}") from e


def _json(raw: bytes) -> dict[str, object]:
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def register(
    agent: str,
    cwd: str,
    hooks: bool,
    agent_session_id: str | None = None,
    timeout: float = 2.0,
    ancestry: bool = True,
) -> dict[str, object]:
    """Register (or revive) a session and return its session-file contents.

    Without `ancestry` the file records no ancestors: Codex files are adopted
    by session id, and every Codex process shares one long-lived daemon.
    """
    body: dict[str, object] = {
        "agent": agent,
        "cwd": cwd,
        "repo": repo_name(cwd),
        "hooks": hooks,
    }
    if agent_session_id is not None:
        body["agent_session_id"] = agent_session_id
    status, data = api("POST", "/v1/sessions", body, timeout)
    if status != 200:
        raise HookError(f"register failed: HTTP {status} {data.get('error')}")
    return {
        "session": str(data["session"]),
        "suffix": data["suffix"],
        "mailbox": data["mailbox"],
        "cwd": cwd,
        "written_at": time.time(),
        "hooks": hooks,
        "agent": agent,
        "agent_session_id": agent_session_id,
        "ancestors": [list(a) for a in ancestors()] if ancestry else [],
    }


# Hook events --------------------------------------------------------------

Event = dict[str, Any]


class _Clock:
    """Per-run deadline, so retries never push a hook past Claude Code's limit."""

    def __init__(self, seconds: float) -> None:
        self._end = time.monotonic() + seconds

    def timeout(self) -> float:
        left = self._end - time.monotonic()
        if left <= 0:
            raise HookError("hook deadline passed")
        return min(HTTP_TIMEOUT, left)


def _agent(event: Event) -> str:
    """The agent this hook runs for: `main` stores its --agent argument here."""
    return str(event.get("_kampung_agent", "claude-code"))


def _register_and_save(event: Event, clock: _Clock) -> dict[str, object]:
    sid = str(event["session_id"])
    agent = _agent(event)
    data = register(
        agent, str(event["cwd"]), True, sid, clock.timeout(), agent != "codex"
    )
    write_session_file(sid, data)
    return data


def _session(event: Event) -> dict[str, object]:
    data = read_session_file(str(event["session_id"]))
    if data is None:
        raise HookError("no session file for this session")
    return data


def _call(
    event: Event, clock: _Clock, method: str, action: str, revive: bool
) -> dict[str, object]:
    """Call /v1/sessions/{id}/{action}; on 409 re-register (revive) and retry once."""
    data = (
        _session(event)
        if not revive
        else (
            read_session_file(str(event["session_id"]))
            or _register_and_save(event, clock)
        )
    )
    for attempt in range(2):
        path = f"/v1/sessions/{data['session']}/{action}"
        status, body = api(method, path, None, clock.timeout())
        if status == 200:
            return body
        if status == 409 and attempt == 0:
            data = _register_and_save(event, clock)
            continue
        raise HookError(f"{action}: HTTP {status} {body.get('error')}")
    raise HookError(f"{action}: gave up")


def on_session_start(event: Event, clock: _Clock) -> dict[str, object] | None:
    sweep_stale_files()
    _register_and_save(event, clock)
    if _agent(event) != "codex":
        return None
    # The Codex adapter learns its session only from a tool call; this one
    # binds it early, so idle wake works, and reads anything already waiting.
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": CODEX_START_CONTEXT,
        }
    }


def _turn_path(sid: str) -> Path:
    return cache_dir() / "turn" / _NAME_BAD.sub("_", sid)


def mark_turn(sid: str) -> None:
    """Mark a Codex session as mid-turn, for the waker (see turn_active)."""
    path = _turn_path(sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def end_turn(sid: str) -> None:
    _turn_path(sid).unlink(missing_ok=True)


def turn_active(sid: str) -> bool:
    """True while a Codex turn runs. The waker skips `codex queue` then:
    Codex would run the queued prompt after the turn, when the nudge or the
    Stop hook has already had the mail read."""
    try:
        return time.time() - _turn_path(sid).stat().st_mtime < TURN_STALE
    except OSError:
        return False


def on_user_prompt(event: Event, clock: _Clock) -> None:
    if _agent(event) == "codex":
        mark_turn(str(event["session_id"]))
    # A turn started by a Channels push also fires UserPromptSubmit, with the
    # notification as the prompt. That is not the user typing, so it must not
    # reset the loop guard.
    # A `codex queue` wake is the same case for Codex.
    if str(event.get("prompt", "")).lstrip().startswith((CHANNEL_PREFIX, WAKE_PREFIX)):
        return
    # Registers when the session file is missing (SessionStart failed earlier):
    # a deliberate departure from fail-open, so the window recovers.
    _call(event, clock, "POST", "human-turn", revive=True)


def _nudge_path(event: Event) -> Path:
    return (
        cache_dir() / "nudge" / f"{_NAME_BAD.sub('_', str(event['session_id']))}.json"
    )


def _nudge_state(path: Path) -> dict[str, float]:
    try:
        data = json.loads(path.read_text())
        return {"checked": float(data["checked"]), "notified": float(data["notified"])}
    except (OSError, ValueError, KeyError, TypeError):
        return {"checked": 0.0, "notified": 0.0}


def _save_nudge_state(path: Path, state: dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, path)


def on_post_tool_use(event: Event, clock: _Clock) -> dict[str, object] | None:
    """Mid-turn nudge: tell a busy agent about new wakeable mail, at most once
    per new message and at most one server check every NUDGE_INTERVAL."""
    if _agent(event) == "codex":
        mark_turn(str(event["session_id"]))  # keeps a long turn from going stale
    path = _nudge_path(event)
    state = _nudge_state(path)
    if str(event.get("tool_name", "")).startswith("mcp__kampung__"):
        # The agent is already talking to Kampung; whatever it read is handled.
        _save_nudge_state(path, {"checked": time.time(), "notified": 0.0})
        return None
    now = time.time()
    if 0 <= now - state["checked"] < NUDGE_INTERVAL:
        return None
    # Record the attempt first, so a slow or unreachable server costs at most
    # one check per NUDGE_INTERVAL instead of a timeout on every tool call.
    _save_nudge_state(path, {"checked": now, "notified": state["notified"]})
    body = _call(event, clock, "GET", "peek", revive=False)
    wakeable = int(str(body.get("wakeable", 0)))
    notify = wakeable > state["notified"]
    _save_nudge_state(path, {"checked": time.time(), "notified": float(wakeable)})
    if not notify:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": STOP_REASON.format(n=wakeable),
        }
    }


def on_stop(event: Event, clock: _Clock) -> dict[str, object] | None:
    result: dict[str, object] | None = None
    try:
        if not event.get("stop_hook_active"):
            body = _call(event, clock, "GET", "peek", revive=False)
            wakeable = int(str(body.get("wakeable", 0)))
            if wakeable > 0:
                result = {"decision": "block", "reason": STOP_REASON.format(n=wakeable)}
    finally:
        # A block keeps the turn going; anything else ends it.
        if result is None and _agent(event) == "codex":
            end_turn(str(event["session_id"]))
    return result


def on_session_end(event: Event, clock: _Clock) -> None:
    sid = str(event["session_id"])
    _nudge_path(event).unlink(missing_ok=True)
    end_turn(sid)
    data = _session(event)
    # Local cleanup first: if the HTTP call outlives the deadline, the file
    # must not be left behind for an ended session.
    delete_session_file(sid)
    api("POST", f"/v1/sessions/{data['session']}/end", None, clock.timeout())


HANDLERS: dict[str, Callable[[Event, _Clock], dict[str, object] | None]] = {
    "SessionStart": on_session_start,
    "UserPromptSubmit": on_user_prompt,
    "Stop": on_stop,
    "PostToolUse": on_post_tool_use,
    "SessionEnd": on_session_end,
}


def _log(name: str, error: BaseException) -> None:
    try:
        cache_dir().mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with open(cache_dir() / "hooks.log", "a") as f:
            f.write(f"{stamp} {name}: {type(error).__name__}: {error}\n")
    except OSError:
        pass


def main(argv: list[str] | None = None, stdin: TextIO | None = None) -> int:
    """Run one hook event. Always exits 0; prints at most one JSON object.

    The handler runs in a daemon thread so that anything the HTTP timeouts do
    not bound (DNS lookups, ps, git) is still cut off at the deadline.
    Unknown arguments are ignored, so a stray `|| true` cannot break a hook.
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--agent", default="claude-code")
    args, _ = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
    agent = args.agent if args.agent in AGENTS else "claude-code"
    deadline = HOOK_DEADLINE
    name = "unknown"
    result: list[dict[str, object] | None] = []
    errors: list[BaseException] = []
    try:
        event = json.load(stdin or sys.stdin)
        if not isinstance(event, dict):
            raise HookError("hook input is not a JSON object")
        name = str(event.get("hook_event_name", "unknown"))
        handler = HANDLERS.get(name)
        if handler is None:
            return 0
        event["_kampung_agent"] = agent
        if agent == "codex" and name == "SessionEnd":
            deadline = CODEX_END_DEADLINE
        clock = _Clock(deadline)
        load_config()  # report a broken config before anything else

        def run() -> None:
            try:
                result.append(handler(event, clock))
            except BaseException as e:  # noqa: BLE001 - reported below
                errors.append(e)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(deadline)
        if worker.is_alive():
            if agent == "codex" and name == "Stop" and "session_id" in event:
                end_turn(str(event["session_id"]))  # no block printed: turn over
            raise HookError("hook deadline passed")
        if errors:
            raise errors[0]
        if result and result[0] is not None:
            print(json.dumps(result[0]))
    except Exception as e:  # noqa: BLE001 - a hook must never fail the agent
        _log(name, e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
