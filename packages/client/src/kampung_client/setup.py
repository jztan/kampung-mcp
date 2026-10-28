"""`kampung-mcp setup`: connect this machine's agents to a Kampung server."""

from __future__ import annotations

import json
import os
import re
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
EVENTS = ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd", "PostToolUse")
OUR_HOOK = re.compile(r"kampung_hook\.py|kampung_client[\\/]+hook\.py")
SAFE_PS_ARG = re.compile(r"(?!--$)[\w./:=-]+")  # needs no quoting in PowerShell
HOOK_TIMEOUT = 5  # seconds; also lifts Claude Code's 1.5 s SessionEnd budget


class SetupError(Exception):
    pass


def default_runner(cmd: list[str]) -> int:
    # shutil.which follows PATHEXT, so it finds claude.cmd on Windows, which
    # CreateProcess alone would not.
    program = shutil.which(cmd[0])
    if program is None:
        raise FileNotFoundError(cmd[0])
    return subprocess.run([program, *cmd[1:]]).returncode


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


def hook_command(hook_path: str) -> str:
    """Run the installed hook.py in place, so it always matches the adapter.

    `|| true`: if the install moves, Python exits 2 for the missing file, and
    Claude Code reads exit 2 from a hook as "block".
    """
    return f"{shlex.quote(sys.executable)} {shlex.quote(hook_path)} || true"


def _claude_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _load_claude_settings() -> dict[str, Any]:
    """Read and shape-check settings.json before setup changes anything."""
    path = _claude_settings_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text() or "{}")
    except ValueError as e:
        raise SetupError(f"{path} is not valid JSON ({e}); fix it and rerun.") from e
    hooks = data.get("hooks", {}) if isinstance(data, dict) else None
    if not isinstance(data, dict) or not isinstance(hooks, dict):
        raise SetupError(f"{path} has an unexpected shape; fix it and rerun.")
    for event in EVENTS:
        if not isinstance(hooks.get(event, []), list):
            raise SetupError(
                f"{path}: hooks.{event} should be a list; fix it and rerun."
            )
    return data


def _merge_claude_hooks(data: dict[str, Any]) -> Path:
    settings = _claude_settings_path()
    if settings.exists():
        backup = settings.with_name("settings.json.kampung-backup")
        if not backup.exists():
            shutil.copyfile(settings, backup)
    command = hook_command(hook.__file__)
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
    """Our hook entry: the installed hook.py, or the copy older setups made."""
    if not isinstance(entry, dict):
        return False
    return any(
        OUR_HOOK.search(str(h.get("command", "")))
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
    return manual_command(add, os.name == "nt")


def manual_command(cmd: list[str], windows: bool) -> str:
    """A command line the user can paste: PowerShell on Windows, else POSIX."""
    if not windows:
        return shlex.join(cmd)
    # PowerShell can eat a bare --, so quote it too.
    return " ".join(
        arg if SAFE_PS_ARG.fullmatch(arg) else "'" + arg.replace("'", "''") + "'"
        for arg in cmd
    )


def run_setup(
    server: str,
    key: str,
    agents: list[str],
    runner: Runner,
    channels: bool = False,
) -> list[str]:
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

    claude_settings = _load_claude_settings() if "claude-code" in agents else None

    _write_config(server, key)
    # Older setups copied the hook here; it now runs from the install.
    (hook.config_dir() / "kampung_hook.py").unlink(missing_ok=True)
    lines = [f"Saved server and key to {hook.config_dir() / 'config.json'}."]

    if "claude-code" in agents:
        assert claude_settings is not None
        settings = _merge_claude_hooks(claude_settings)
        lines.append(f"Claude Code hooks added to {settings}.")
        manual = _register(
            ["claude", "mcp", "remove", "--scope", "user", "kampung"],
            ["claude", "mcp", "add", "--scope", "user", "kampung", "--"]
            + _adapter_cmd("claude-code")
            + (["--channels"] if channels else []),
            runner,
        )
        lines.append(
            "Claude Code MCP server registered. Restart Claude Code to load it."
            if manual is None
            else f"Could not register with Claude Code. Run this yourself: {manual}"
        )
        if channels:
            lines.append(
                "Channels push is on. Start Claude Code with: claude "
                "--dangerously-load-development-channels server:kampung "
                "(it asks you to confirm each time). Without that flag, mail "
                "still arrives through the hooks."
            )
        else:
            lines.append(
                "An idle Claude Code session (waiting for you) won't notice mail "
                "until you type. To wake it, run setup again with --channels."
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
