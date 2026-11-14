"""Kampung hook script and shared client library. Standard library only.

Claude Code runs this file in place (`kampung-mcp setup` points the hooks at
it), on every hook event, so it must start fast and must never block or break
the agent: every failure is logged and swallowed.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import ipaddress
import json
import math
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TextIO, TypedDict

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
# How a turn Claude Code starts by itself arrives as a UserPromptSubmit: a
# wake from the FileChanged hook, or one of its own background task notices.
TASK_PREFIX = "<task-notification>"
# A wake file with no session file is kept this long: SessionStart creates it
# before the register call writes the session file. Known limit: a session
# whose register failed and that then sat untouched for over a day loses its
# wake file until its next SessionStart.
WAKE_ORPHAN_AGE = 86400.0
AGENTS = ("claude-code", "codex")
CODEX_END_DEADLINE = 2.5  # Codex cancels SessionEnd hooks at 3 s
SESSION_FILE_MAX_AGE = 7 * 86400  # Codex files have no ancestry to sweep by
CODEX_START_CONTEXT = (
    "Kampung is connected. Call the kampung inbox tool once now to check for "
    "peer messages."
)
# Said on a typed prompt while the session has no summary, at most once per
# SUMMARY_REMINDER_INTERVAL: the server instructions alone never got it called.
SUMMARY_REMINDER = (
    "Kampung: this session has no summary, so peers cannot tell what it is "
    "working on. Call the kampung set_summary tool with a few words on your "
    "current task (never secrets or the user's words verbatim)."
)
SUMMARY_REMINDER_INTERVAL = 600.0
STOP_REASON = (
    "{n} Kampung message(s) waiting. Call the kampung inbox tool to read them."
)
# Claude Code shows a Stop block to the user as "Stop hook error: <reason>".
STOP_BLOCK_REASON = f"This is mail, not a failure. {STOP_REASON}"
LOG_MAX_BYTES = 1024 * 1024  # hooks.log rolls to hooks.log.1 at this size


class HookError(Exception):
    """Anything that should make a hook give up quietly."""


class Wake(Exception):
    """Raised by the wake hook with the text for the model. `main` prints it
    to stderr and exits 2, which makes Claude Code start a turn."""


class InsecureServerError(HookError):
    """The configured server would get the machine key over plain http."""


class HttpStatusError(HookError):
    """The server answered, with a 5xx."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


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


class LookupFailed(Exception):
    """The process table could not be read (ps timed out or is missing), as
    opposed to the process being gone. The sweep must not delete on this."""


def _ps_lookup(pid: int) -> ProcessInfo | None:
    try:
        result = subprocess.run(
            ["ps", "-o", "ppid=,lstart=,comm=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
            # Hooks and the adapter may run with different locales, and
            # lstart is locale-formatted; pin it so start times compare equal.
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise LookupFailed(pid) from e
    # No such process is exit 1 with nothing on stderr (macOS and procps);
    # anything else (killed, denied by a sandbox) says nothing about the pid.
    if result.returncode != 0 and (result.returncode != 1 or result.stderr):
        raise LookupFailed(pid)
    out = result.stdout.split()
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
            raise LookupFailed(pid)
        try:
            entry = _ProcessEntry()
            entry.dwSize = ctypes.sizeof(_ProcessEntry)
            ok = _k32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                if entry.th32ProcessID == pid:
                    start = _win_start(pid)
                    if start is None:  # alive, but its start time is unreadable
                        raise LookupFailed(pid)
                    return int(entry.th32ParentProcessID), start, entry.szExeFile
                ok = _k32.Process32NextW(snap, ctypes.byref(entry))
            return None
        finally:
            _k32.CloseHandle(snap)


def _lookup(pid: int) -> ProcessInfo | None:
    """The process, or None if it is gone. Raises LookupFailed."""
    if sys.platform == "win32":
        return _win_lookup(pid)
    return _ps_lookup(pid)


def _lookup_or_none(pid: int) -> ProcessInfo | None:
    try:
        return _lookup(pid)
    except LookupFailed:
        return None


def _process(pid: int) -> tuple[int, str] | None:
    """(parent pid, start time) for a live process, or None."""
    info = _lookup_or_none(pid)
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
    return _walk(os.getpid() if pid is None else pid, _lookup_or_none)


SCRATCH_REPO = "scratch"
_TEMP_DIRS = (
    "/tmp",
    "/var/tmp",
    "/private/tmp",
    "/var/folders",
    "/private/var/folders",
)


def _norm(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def _temp_roots() -> list[str]:
    """The system temp folders, resolved: where eval and throwaway runs live."""
    found = [tempfile.gettempdir(), *_TEMP_DIRS]
    found += [os.environ[v] for v in ("TMPDIR", "TEMP", "TMP") if os.environ.get(v)]
    return [_norm(p) for p in found]


def _is_scratch(cwd: str) -> bool:
    """The home folder itself, or anywhere under a temp folder."""
    here = _norm(cwd)
    if here == _norm(str(Path.home())):
        return True
    return any(
        here == root or here.startswith(root.rstrip(os.sep) + os.sep)
        for root in map(_norm, _temp_roots())
    )


def _folder(cwd: str) -> tuple[str, bool]:
    """The folder a mailbox belongs to: the git worktree root, or the folder
    itself outside git. The bool says whether it is a git worktree."""
    try:
        top = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            # The adapter's stdin belongs to the MCP server. A child inheriting
            # it hangs at startup on Windows, past the timeout below.
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if top.returncode == 0 and top.stdout.strip():
            return top.stdout.strip(), True
    except (OSError, subprocess.SubprocessError):
        pass
    return cwd, False


MAX_BRANCH_CHARS = 100  # the server's limit; cut here so a long name never fails
_DETACHED = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _head_file(cwd: str) -> Path | None:
    """The HEAD file of the git checkout holding cwd: .git/HEAD, or for a
    worktree or submodule (.git is a file) the HEAD its gitdir line names."""
    folder = Path(cwd)
    for d in (folder, *folder.parents):
        dot_git = d / ".git"
        if dot_git.is_dir():
            return dot_git / "HEAD"
        if dot_git.is_file():
            line = dot_git.read_text(encoding="utf-8").strip()
            if not line.startswith("gitdir:"):
                return None
            return (d / line[len("gitdir:") :].strip()) / "HEAD"
    return None


def git_branch(cwd: str) -> str | None:
    """The git branch of cwd's checkout, read from HEAD without running git
    (hooks have 2 seconds, and git starts slowly on Windows). None outside
    git or on anything unexpected; never raises."""
    try:
        head_file = _head_file(cwd)
        if head_file is None:
            return None
        head = head_file.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None
    if head.startswith("ref:"):
        ref = head[len("ref:") :].strip()
        name = ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ref
        if name.startswith("refs/"):
            name = name[len("refs/") :]
    elif _DETACHED.fullmatch(head):
        name = f"detached {head[:7]}"
    else:
        return None
    name = name.strip()[:MAX_BRANCH_CHARS]
    return name if name and name.isprintable() else None


def client_version() -> str | None:
    """This client's version. hook.py runs as a file, not as part of the
    package, so it asks the installed distribution."""
    try:
        return importlib.metadata.version("kampung-mcp")
    except Exception:
        return None


def _pins_path() -> Path:
    return cache_dir() / "mailboxes.json"


def _pins() -> dict[str, str]:
    """Folder -> mailbox name, as first registered. A damaged file counts as
    empty: hooks fail open."""
    try:
        data = json.loads(_pins_path().read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str)}


def _computed_name(folder: str, in_git: bool, cwd: str) -> str:
    if not in_git and _is_scratch(cwd):
        return SCRATCH_REPO
    name = os.path.basename(os.path.normpath(folder))
    cleaned = _NAME_BAD.sub("-", name)
    cleaned = re.sub(r"^[^A-Za-z0-9]+", "", cleaned)[:64]
    return cleaned or "root"


def repo_name(cwd: str) -> str:
    """The mailbox repo name. A folder keeps the name it first registered
    with (see pin_mailbox), so a later change to the rules below never moves
    it to another mailbox and strands its mail. Otherwise: the git worktree's
    directory name, made valid; outside git, the folder's own name, except
    the home folder and temp folders (eval and throwaway runs), which share
    the machine's one scratch mailbox so they do not each leave one behind."""
    return _mailbox(cwd)[1]


def _mailbox(cwd: str) -> tuple[str, str]:
    """The folder's pin key and its mailbox name, from one look at git."""
    folder, in_git = _folder(cwd)
    key = _norm(folder)
    pinned = _pins().get(key)
    return key, pinned if pinned is not None else _computed_name(folder, in_git, cwd)


def pin_mailbox(key: str, repo: str) -> None:
    """Record the folder's mailbox name after its first registration. Scratch
    is never pinned: home and temp folders are throwaway, and pinning each
    would grow the file without end. Written atomically; two hooks racing
    write the same value."""
    if repo == SCRATCH_REPO:
        return
    pins = _pins()
    if key in pins:
        return
    pins[key] = repo
    path = _pins_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(pins, indent=1, sort_keys=True))
    os.replace(tmp, path)


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


# Idle wake (Claude Code): the adapter writes a fresh token into the session's
# wake file, Claude Code runs the FileChanged hook because SessionStart asked
# it to watch that file, and the hook answers through the ack file. The wake
# file existing is the one switch: only SessionStart with --idle-wake makes it.


def wake_dir() -> Path:
    return cache_dir() / "wake"


def _wake_name(agent_session_id: str) -> str:
    safe = _NAME_BAD.sub("_", agent_session_id)
    # "", "." and ".." would name the folder itself or its parent.
    return safe if safe.strip(".") else f"_{safe}"


def wake_path(agent_session_id: str) -> Path:
    return wake_dir() / _wake_name(agent_session_id)


def ack_path(agent_session_id: str) -> Path:
    return wake_dir() / f"{_wake_name(agent_session_id)}.ack"


def ensure_wake_file(agent_session_id: str) -> Path:
    """Create the session's wake file, empty, if it is missing. An existing
    file is left alone, content and times: a change would fire the watch."""
    path = wake_path(agent_session_id)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)  # a folder made by an older run or umask
    except OSError:
        pass
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
    return path


def write_wake_token(agent_session_id: str) -> str | None:
    """Put a fresh random token in the session's wake file, which is what
    wakes it. Never creates the file: without one, idle wake is off."""
    path = wake_path(agent_session_id)
    if not path.is_file():
        return None
    token = secrets.token_hex(16)
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    os.replace(tmp, path)
    return token


def write_ack(agent_session_id: str) -> str:
    """Copy the wake file's token to the ack file, which tells the adapter
    that Claude Code ran the wake hook for that token. Returns the token."""
    token = read_token(wake_path(agent_session_id))
    path = ack_path(agent_session_id)
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    try:
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    return token


def read_token(path: Path) -> str:
    try:
        return path.read_text().strip()
    except (OSError, ValueError):
        return ""


def delete_wake_files(agent_session_id: str) -> None:
    for path in (wake_path(agent_session_id), ack_path(agent_session_id)):
        try:
            path.unlink()
        except OSError:
            pass


def idle_wake_on(agent: str, agent_session_id: object) -> bool:
    """Whether this session can be woken through its wake file."""
    return (
        agent == "claude-code"
        and isinstance(agent_session_id, str)
        and wake_path(agent_session_id).is_file()
    )


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
    """Delete session files whose nearest recorded ancestor is no longer alive,
    with their wake files, and wake files left without a session file.
    Returns the session files removed."""
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
        try:
            live = _lookup(int(pid))
        except LookupFailed:
            continue  # unknown is not dead: keep the file
        if live is None or live[1] != start:
            path.unlink(missing_ok=True)
            delete_wake_files(path.stem)
            removed += 1
    _sweep_orphan_wake_files(now)
    return removed


def _sweep_orphan_wake_files(now: float) -> None:
    try:
        paths = list(wake_dir().iterdir())
    except OSError:
        return  # no wake folder: idle wake was never on
    for path in paths:
        name = path.name.removesuffix(".ack")
        try:
            if (sessions_dir() / f"{name}.json").exists():
                continue
            if now - path.stat().st_mtime > WAKE_ORPHAN_AGE:
                path.unlink()
        except OSError:
            continue


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
            raise HttpStatusError(f"{method} {path}: HTTP {e.code}", e.code) from e
        return e.code, _json(e.read())
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise HookError(f"{method} {path}: {e}") from e


def _json(raw: bytes) -> dict[str, object]:
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def in_background(agent: str) -> bool:
    """Whether this runs in a Claude Code background session (claude --bg,
    /background, a background agent). Claude Code sets CLAUDE_JOB_DIR only
    there; such a session cannot get Channels push. Only Claude Code counts:
    a codex started from that session's shell inherits the variable."""
    return agent == "claude-code" and bool(os.environ.get("CLAUDE_JOB_DIR"))


def register(
    agent: str,
    cwd: str,
    hooks: bool,
    agent_session_id: str | None = None,
    timeout: float = 2.0,
    ancestry: bool = True,
    report_branch: bool = True,
) -> dict[str, object]:
    """Register (or revive) a session and return its session-file contents.

    Without `report_branch` the branch is left as the server has it: the
    adapter cannot tell a session's folder (every Codex window shares one
    daemon), so only the hooks report it.

    Without `ancestry` the file records no ancestors: Codex files are adopted
    by session id, and every Codex process shares one long-lived daemon.
    """
    key, repo = _mailbox(cwd)
    body: dict[str, object] = {
        "agent": agent,
        "cwd": cwd,
        "repo": repo,
        "hooks": hooks,
    }
    if agent_session_id is not None:
        body["agent_session_id"] = agent_session_id
    background = in_background(agent)
    if background:
        # Sent only when set: a server before 0.4 ignores it either way.
        body["background"] = True
    if idle_wake_on(agent, agent_session_id):
        body["idle_wake"] = True  # likewise: a server before 0.5 ignores it
    # A server before 0.6 ignores both.
    branch = git_branch(cwd) or ""
    if report_branch:
        body["branch"] = branch
    version = client_version()
    if version is not None:
        body["client_version"] = version
    status, data = api("POST", "/v1/sessions", body, timeout)
    if status != 200:
        raise HookError(f"register failed: HTTP {status} {data.get('error')}")
    try:
        pin_mailbox(key, repo)
    except OSError:
        pass  # a later registration pins it; never fail a hook over this
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
        "background": background,
        # Only a server that keeps the branch echoes it; with an older one the
        # branch counts as unsent, so it goes again once the server is new.
        **({"branch_sent": branch} if report_branch and "branch" in data else {}),
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
    event: Event,
    clock: _Clock,
    method: str,
    action: str,
    revive: bool,
    body: dict[str, object] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, object]:
    """Call /v1/sessions/{id}/{action}; on 409 or 404 re-register and retry once.

    409: the session ended, and re-registering revives it. 404: the server
    deleted it long after it ended (or the database was restored), and
    re-registering gives the window a fresh one."""
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
        if query:
            path += "?" + urllib.parse.urlencode(query)
        status, answer = api(method, path, body, clock.timeout())
        if status == 200:
            return answer
        if status in (404, 409) and attempt == 0:
            data = _register_and_save(event, clock)
            continue
        raise HookError(f"{action}: HTTP {status} {answer.get('error')}")
    raise HookError(f"{action}: gave up")


def watch_output(event: Event, name: str) -> dict[str, object] | None:
    """What a hook prints to make Claude Code watch this session's wake file,
    or None when the session has none."""
    sid = event.get("session_id")
    if not isinstance(sid, str) or not idle_wake_on(_agent(event), sid):
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": name,
            "watchPaths": [str(wake_path(sid))],
        }
    }


def on_session_start(event: Event, clock: _Clock) -> dict[str, object] | None:
    sweep_stale_files()  # first: it may remove this session's files from a dead run
    watch = None
    if _agent(event) != "codex" and event.get("_kampung_idle_wake"):
        ensure_wake_file(str(event["session_id"]))
        watch = watch_output(event, "SessionStart")
        # The watch does not depend on the server: main prints it even when
        # the register call below fails or outlives the deadline.
        event["_kampung_output"] = watch
    _register_and_save(event, clock)
    if _agent(event) != "codex":
        return watch
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


def _current_branch(event: Event) -> tuple[dict[str, object] | None, str]:
    """The session file and the branch of the session's registered folder
    (the event's cwd only when there is no file yet): the same folder as the
    cwd and mailbox peers see, never where the session has cd'd since."""
    data = read_session_file(str(event["session_id"]))
    cwd = data.get("cwd") if data else None
    if not isinstance(cwd, str) or not cwd:
        cwd = event.get("cwd")
    branch = git_branch(cwd) if isinstance(cwd, str) and cwd else None
    return data, branch or ""


def _mark_branch_sent(sid: str, branch: str) -> None:
    data = read_session_file(sid)
    if data is None or data.get("branch_sent") == branch:
        return
    try:
        write_session_file(sid, {**data, "branch_sent": branch})
    except OSError:
        pass  # sent again next time; never fail a hook over this


def _summary_reminder(sid: str, answer: dict[str, object]) -> dict[str, object] | None:
    """A reminder to call set_summary, when the server says this session has
    none (an older server sends no summary key: never) and none was given
    in the last SUMMARY_REMINDER_INTERVAL."""
    if "summary" not in answer or answer["summary"] is not None:
        return None
    data = read_session_file(sid)
    if data is None:
        return None
    last = data.get("summary_reminded")
    now = time.time()
    if isinstance(last, (int, float)) and 0 <= now - last < SUMMARY_REMINDER_INTERVAL:
        return None
    try:
        write_session_file(sid, {**data, "summary_reminded": now})
    except OSError:
        return None  # unrecorded, it would repeat on every prompt
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": SUMMARY_REMINDER,
        }
    }


def on_user_prompt(event: Event, clock: _Clock) -> dict[str, object] | None:
    if _agent(event) == "codex":
        mark_turn(str(event["session_id"]))
    # A turn started by a Channels push also fires UserPromptSubmit, with the
    # notification as the prompt. That is not the user typing, so it must not
    # reset the loop guard.
    # A `codex queue` wake is the same case for Codex, and so is a turn
    # Claude Code starts by itself (a file wake, a background task notice).
    prompt = str(event.get("prompt", "")).lstrip()
    if prompt.startswith((CHANNEL_PREFIX, WAKE_PREFIX, TASK_PREFIX)):
        return None
    # Registers when the session file is missing (SessionStart failed earlier):
    # a deliberate departure from fail-open, so the window recovers.
    _, branch = _current_branch(event)
    answer = _call(
        event, clock, "POST", "human-turn", revive=True, body={"branch": branch}
    )
    if "branch" in answer:  # a server before 0.6 does not keep it
        _mark_branch_sent(str(event["session_id"]), branch)
    return _summary_reminder(str(event["session_id"]), answer)


Seen = str | int | None  # what was last announced: a message id, or a count


def new_mail(body: dict[str, object], seen: Seen) -> tuple[bool, Seen]:
    """Whether a peek shows wakeable mail not announced yet, and what to
    remember as announced. A server that sends `newest` (the newest wakeable
    message id) catches one message replacing another between peeks, which
    leaves the count unchanged; older servers send only the count."""
    wakeable = int(str(body.get("wakeable", 0)))
    newest = body.get("newest")
    if isinstance(newest, str):
        return wakeable > 0 and newest != seen, newest
    count = seen if isinstance(seen, int) else 0
    return wakeable > count, wakeable


class NudgeState(TypedDict):
    checked: float
    notified: Seen


def _nudge_path(event: Event) -> Path:
    return (
        cache_dir() / "nudge" / f"{_NAME_BAD.sub('_', str(event['session_id']))}.json"
    )


def _nudge_state(path: Path) -> NudgeState:
    try:
        data = json.loads(path.read_text())
        checked = float(data["checked"])
        notified = data.get("notified")
    except (OSError, ValueError, KeyError, TypeError):
        return {"checked": 0.0, "notified": None}
    if not math.isfinite(checked):
        checked = 0.0  # json.loads accepts NaN and Infinity
    if isinstance(notified, float) and notified.is_integer():
        notified = int(notified)  # a count written by an older client
    if not isinstance(notified, (str, int)) or isinstance(notified, bool):
        notified = None
    return {"checked": checked, "notified": notified}


def _save_nudge_state(path: Path, state: NudgeState) -> None:
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
    if str(event.get("tool_name", "")).startswith("mcp__kampung__"):
        # The agent is already talking to Kampung; whatever it read is handled.
        _save_nudge_state(path, {"checked": time.time(), "notified": None})
        return None
    state = _nudge_state(path)
    now = time.time()
    if 0 <= now - state["checked"] < NUDGE_INTERVAL:
        return None
    # Record the attempt first, so a slow or unreachable server costs at most
    # one check per NUDGE_INTERVAL instead of a timeout on every tool call.
    _save_nudge_state(path, {"checked": now, "notified": state["notified"]})
    body = _call(event, clock, "GET", "peek", revive=False)
    wakeable = int(str(body.get("wakeable", 0)))
    notify, seen = new_mail(body, state["notified"])
    _save_nudge_state(path, {"checked": time.time(), "notified": seen})
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
            # The branch rides on this peek only when it changed, so an agent's
            # own `git switch` shows by the end of its turn.
            data, branch = _current_branch(event)
            changed = data is not None and data.get("branch_sent") != branch
            query = {"branch": branch} if changed else None
            body = _call(event, clock, "GET", "peek", revive=False, query=query)
            if changed and "branch" in body:  # an older server ignores it
                _mark_branch_sent(str(event["session_id"]), branch)
            wakeable = int(str(body.get("wakeable", 0)))
            if wakeable > 0:
                reason = STOP_BLOCK_REASON.format(n=wakeable)
                result = {"decision": "block", "reason": reason}
    finally:
        # A block keeps the turn going; anything else ends it.
        if result is None and _agent(event) == "codex":
            end_turn(str(event["session_id"]))
    return result


def peek_only(event: Event, clock: _Clock) -> dict[str, object] | None:
    """One peek for the session in the session file, or None when there is no
    file or the server refuses. Never registers: a file change must not bring
    an ended session back."""
    data = read_session_file(str(event["session_id"]))
    if data is None:
        return None
    path = f"/v1/sessions/{data['session']}/peek"
    status, body = api("GET", path, None, clock.timeout())
    return body if status == 200 else None


def on_cwd_changed(event: Event, clock: _Clock) -> dict[str, object] | None:
    # Claude Code drops the watch when the working directory changes.
    return watch_output(event, "CwdChanged")


def _same_file(a: str, b: Path) -> bool:
    def norm(path: str) -> str:
        return os.path.normcase(os.path.realpath(path))

    return norm(a) == norm(str(b))


def on_file_changed(event: Event, clock: _Clock) -> dict[str, object] | None:
    """Two hooks share this event. The plain one returns the watch again,
    because another hook's own watch list replaces ours. The --wake one (an
    asyncRewake hook) answers a change to this session's wake file: it acks
    the token, then wakes the session when wakeable mail waits."""
    if not event.get("_kampung_wake"):
        return watch_output(event, "FileChanged")
    sid = event.get("session_id")
    changed = event.get("file_path")
    if not isinstance(sid, str) or not isinstance(changed, str):
        return None
    if not idle_wake_on(_agent(event), sid) or not _same_file(changed, wake_path(sid)):
        return None
    # Ack first: any failure from here on exits 0, so a change that was not
    # acked never also wakes, and the adapter's fallback cannot double it.
    write_ack(sid)
    body = peek_only(event, clock)
    wakeable = int(str(body.get("wakeable", 0))) if body else 0
    if wakeable > 0:
        raise Wake(STOP_BLOCK_REASON.format(n=wakeable))
    return None


def on_session_end(event: Event, clock: _Clock) -> None:
    sid = str(event["session_id"])
    _nudge_path(event).unlink(missing_ok=True)
    end_turn(sid)
    delete_wake_files(sid)
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
    "CwdChanged": on_cwd_changed,
    "FileChanged": on_file_changed,
}


def _roll_log(path: Path) -> None:
    try:
        if path.stat().st_size >= LOG_MAX_BYTES:
            os.replace(path, path.with_name("hooks.log.1"))
    except OSError:
        pass  # no log yet, or another hook is rolling or holding it


def _log(name: str, error: BaseException) -> None:
    try:
        cache_dir().mkdir(parents=True, exist_ok=True)
        path = cache_dir() / "hooks.log"
        _roll_log(path)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with open(path, "a") as f:
            f.write(f"{stamp} {name}: {type(error).__name__}: {error}\n")
    except OSError:
        pass


def main(argv: list[str] | None = None, stdin: TextIO | None = None) -> int:
    """Run one hook event. Prints at most one JSON object and exits 0, except
    that the wake hook exits 2 with its reason on stderr (see Wake).

    A handler may leave a result in event["_kampung_output"] before it calls
    the server; that result is printed when the handler then fails or passes
    the deadline.

    The handler runs in a daemon thread so that anything the HTTP timeouts do
    not bound (DNS lookups, ps, git) is still cut off at the deadline.
    Unknown arguments are ignored, so a stray `|| true` cannot break a hook.
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--agent", default="claude-code")
    parser.add_argument("--idle-wake", action="store_true")
    parser.add_argument("--wake", action="store_true")
    args, _ = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
    agent = args.agent if args.agent in AGENTS else "claude-code"
    deadline = HOOK_DEADLINE
    name = "unknown"
    result: list[dict[str, object] | None] = []
    errors: list[BaseException] = []
    event: Any = None
    printed = False
    try:
        event = json.load(stdin or sys.stdin)
        if not isinstance(event, dict):
            raise HookError("hook input is not a JSON object")
        name = str(event.get("hook_event_name", "unknown"))
        handler = HANDLERS.get(name)
        if handler is None:
            return 0
        event.pop("_kampung_output", None)  # only a handler may set it
        event["_kampung_agent"] = agent
        event["_kampung_idle_wake"] = args.idle_wake
        event["_kampung_wake"] = args.wake
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
            printed = True
            print(json.dumps(result[0]))
    except Wake as wake:
        try:
            print(str(wake), file=sys.stderr)
        except (OSError, ValueError):
            return 0  # nowhere to put the reason: do not wake without one
        return 2
    except Exception as e:  # noqa: BLE001 - a hook must never fail the agent
        _log(name, e)
        pending = event.get("_kampung_output") if isinstance(event, dict) else None
        if pending is not None and not printed:
            try:
                print(json.dumps(pending))
            except Exception:  # noqa: BLE001 - nothing left to report to
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
