"""Kampung hook script and shared client library. Standard library only.

`kampung-mcp setup` copies this file to ~/.config/kampung/kampung_hook.py and
Claude Code runs it on every hook event, so it must start fast and must never
block or break the agent: every failure is logged and swallowed.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TextIO

Ancestor = tuple[int, str]

_NAME_BAD = re.compile(r"[^A-Za-z0-9._-]")

HTTP_TIMEOUT = 2.0
HOOK_DEADLINE = 4.0
STOP_REASON = (
    "{n} Kampung message(s) waiting. Call the kampung inbox tool to read them."
)


class HookError(Exception):
    """Anything that should make a hook give up quietly."""


def config_dir() -> Path:
    return Path.home() / ".config" / "kampung"


def cache_dir() -> Path:
    return Path.home() / ".cache" / "kampung"


def sessions_dir() -> Path:
    return cache_dir() / "sessions"


def load_config() -> dict[str, str]:
    path = config_dir() / "config.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise HookError(f"cannot read {path}: {e}") from e
    if not isinstance(data, dict) or not data.get("server") or not data.get("key"):
        raise HookError(f"{path} needs both server and key")
    return {"server": str(data["server"]).rstrip("/"), "key": str(data["key"])}


def _process(pid: int) -> tuple[int, str] | None:
    """(parent pid, start time) for a live process, or None."""
    try:
        out = subprocess.run(
            ["ps", "-o", "ppid=,lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
            # Hooks and the adapter may run with different locales, and
            # lstart is locale-formatted; pin it so start times compare equal.
            env={**os.environ, "LC_ALL": "C"},
        ).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return None
    if len(out) < 2:
        return None
    return int(out[0]), " ".join(out[1:])


def ancestors(pid: int | None = None) -> list[Ancestor]:
    """(pid, start time) of every ancestor of `pid`, nearest first, without pid 1."""
    info = _process(os.getpid() if pid is None else pid)
    chain: list[Ancestor] = []
    current = info[0] if info else 0
    while current > 1:
        parent = _process(current)
        if parent is None:
            break
        chain.append((current, parent[1]))
        current = parent[0]
    return chain


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
    for path in sessions_dir().glob("*.json"):
        try:
            data = json.loads(path.read_text())
            pid, start = data["ancestors"][0]
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            continue
        live = _process(int(pid))
        if live is None or live[1] != start:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


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
        with urllib.request.urlopen(request, timeout=timeout) as response:
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
) -> dict[str, object]:
    """Register (or revive) a session and return its session-file contents."""
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
        "ancestors": [list(a) for a in ancestors()],
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


def _register_and_save(event: Event, clock: _Clock) -> dict[str, object]:
    sid = str(event["session_id"])
    data = register("claude-code", str(event["cwd"]), True, sid, clock.timeout())
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


def on_session_start(event: Event, clock: _Clock) -> None:
    sweep_stale_files()
    _register_and_save(event, clock)


def on_user_prompt(event: Event, clock: _Clock) -> None:
    # Registers when the session file is missing (SessionStart failed earlier):
    # a deliberate departure from fail-open, so the window recovers.
    _call(event, clock, "POST", "human-turn", revive=True)


def on_stop(event: Event, clock: _Clock) -> dict[str, object] | None:
    if event.get("stop_hook_active"):
        return None
    body = _call(event, clock, "GET", "peek", revive=False)
    wakeable = int(str(body.get("wakeable", 0)))
    if wakeable > 0:
        return {"decision": "block", "reason": STOP_REASON.format(n=wakeable)}
    return None


def on_session_end(event: Event, clock: _Clock) -> None:
    sid = str(event["session_id"])
    try:
        data = _session(event)
        api("POST", f"/v1/sessions/{data['session']}/end", None, clock.timeout())
    finally:
        delete_session_file(sid)


HANDLERS: dict[str, Callable[[Event, _Clock], dict[str, object] | None]] = {
    "SessionStart": on_session_start,
    "UserPromptSubmit": on_user_prompt,
    "Stop": on_stop,
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
    """Run one hook event. Always exits 0; prints at most one JSON object."""
    clock = _Clock(HOOK_DEADLINE)
    name = "unknown"
    try:
        event = json.load(stdin or sys.stdin)
        if not isinstance(event, dict):
            raise HookError("hook input is not a JSON object")
        name = str(event.get("hook_event_name", "unknown"))
        handler = HANDLERS.get(name)
        if handler is not None:
            load_config()  # report a broken config before anything else
            output = handler(event, clock)
            if output is not None:
                print(json.dumps(output))
    except Exception as e:  # noqa: BLE001 - a hook must never fail the agent
        _log(name, e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
