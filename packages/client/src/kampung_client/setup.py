"""`kampung-mcp setup`: connect this machine's agents to a Kampung server."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import hook

Runner = Callable[[list[str]], int]
EVENTS = ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd")
HOOK_TIMEOUT = 5  # seconds; also lifts Claude Code's 1.5 s SessionEnd budget


class SetupError(Exception):
    pass


def default_runner(cmd: list[str]) -> int:
    return subprocess.run(cmd).returncode


def _status(url: str, key: str | None = None) -> int:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=headers), timeout=5
        ) as r:
            return int(r.status)
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, OSError) as e:
        raise SetupError(f"Could not reach {url}: {e}") from e


def _adapter_cmd(agent: str) -> list[str]:
    return [sys.executable, "-m", "kampung_client", "adapter", "--agent", agent]


def _write_config(server: str, key: str) -> None:
    d = hook.config_dir()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    path = d / "config.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"server": server, "key": key}, f)
    os.chmod(path, 0o600)


def _install_hook() -> Path:
    target = hook.config_dir() / "kampung_hook.py"
    shutil.copyfile(hook.__file__, target)
    return target


def _merge_claude_hooks(hook_path: Path) -> Path:
    settings = Path.home() / ".claude" / "settings.json"
    data: dict[str, Any] = {}
    if settings.exists():
        backup = settings.with_name("settings.json.kampung-backup")
        if not backup.exists():
            shutil.copyfile(settings, backup)
        data = json.loads(settings.read_text() or "{}")
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(hook_path))}"
    entry = {
        "hooks": [{"type": "command", "command": command, "timeout": HOOK_TIMEOUT}]
    }
    hooks = data.setdefault("hooks", {})
    for event in EVENTS:
        kept = [e for e in hooks.get(event, []) if not _is_ours(e)]
        hooks[event] = kept + [entry]
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(data, indent=2) + "\n")
    return settings


def _is_ours(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    return any(
        "kampung_hook.py" in str(h.get("command", ""))
        for h in entry.get("hooks", [])
        if isinstance(h, dict)
    )


def _register(remove: list[str], add: list[str], runner: Runner) -> str | None:
    """Run remove (ignored) then add; return a manual command if add failed."""
    try:
        runner(remove)
    except FileNotFoundError:
        pass
    try:
        if runner(add) == 0:
            return None
    except FileNotFoundError:
        pass
    return shlex.join(add)


def run_setup(server: str, key: str, agents: list[str], runner: Runner) -> list[str]:
    server = server.rstrip("/")
    if _status(f"{server}/health") != 200:
        raise SetupError(
            f"{server}/health did not answer 200; is this a Kampung server?"
        )
    status = _status(f"{server}/v1/sessions/0/peek", key)
    if status == 401:
        raise SetupError("The server rejected this key.")
    if status != 404:
        raise SetupError(f"Unexpected answer from the server (HTTP {status}).")

    _write_config(server, key)
    hook_path = _install_hook()
    lines = [f"Saved server and key to {hook.config_dir() / 'config.json'}."]

    if "claude-code" in agents:
        settings = _merge_claude_hooks(hook_path)
        lines.append(f"Claude Code hooks added to {settings}.")
        manual = _register(
            ["claude", "mcp", "remove", "--scope", "user", "kampung"],
            ["claude", "mcp", "add", "--scope", "user", "kampung", "--"]
            + _adapter_cmd("claude-code"),
            runner,
        )
        lines.append(
            "Claude Code MCP server registered. Restart Claude Code to load it."
            if manual is None
            else f"Could not register with Claude Code. Run this yourself: {manual}"
        )
    if "codex" in agents:
        manual = _register(
            ["codex", "mcp", "remove", "kampung"],
            ["codex", "mcp", "add", "kampung", "--"] + _adapter_cmd("codex"),
            runner,
        )
        lines.append(
            "Codex MCP server registered (polling: Codex reads mail when it "
            "calls inbox)."
            if manual is None
            else f"Could not register with Codex. Run this yourself: {manual}"
        )
    removed = hook.sweep_stale_files()
    if removed:
        lines.append(f"Removed {removed} stale session file(s).")
    return lines
