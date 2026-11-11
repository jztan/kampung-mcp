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
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import hook

Runner = Callable[[list[str]], int]
Version = tuple[int, int, int]
EVENTS = ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd", "PostToolUse")
# Claude Code only, and only with idle wake: the watched wake file changed, and
# the working directory changed (which drops the watch, so a hook renews it).
IDLE_WAKE_EVENTS = ("FileChanged", "CwdChanged")
# The first Claude Code where a SessionStart hook's watchPaths and an
# asyncRewake FileChanged hook wake an idle session, also one in the background.
IDLE_WAKE_FLOOR: Version = (2, 1, 321)
VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")
VERSION_TIMEOUT = 10.0  # seconds for `claude --version`
OUR_HOOK = re.compile(r"kampung_hook\.py|kampung_client[\\/]+hook\.py")
SAFE_PS_ARG = re.compile(r"(?!--$)[\w./:=-]+")  # needs no quoting in PowerShell
HOOK_TIMEOUT = 5  # seconds; also lifts Claude Code's 1.5 s SessionEnd budget
CODEX_END_TIMEOUT = 3  # seconds; Codex caps SessionEnd hooks at 3
CONNECTOR_NAME = "kampung"  # the name the docs tell people to give the connector


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
    """HTTP status of a GET; a redirect comes back as its own status."""
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        with hook.open_url(urllib.request.Request(url, headers=headers), 5) as r:
            return int(r.status)
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, OSError) as e:
        raise SetupError(f"Could not reach {url}: {e}") from e


def _adapter_cmd(agent: str) -> list[str]:
    return [sys.executable, "-m", "kampung_client", "adapter", "--agent", agent]


def _write_config(server: str, key: str, allow_insecure_http: bool) -> None:
    d = hook.config_dir()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    path = d / "config.json"
    data: dict[str, object] = {"server": server, "key": key}
    if allow_insecure_http:
        data["allow_insecure_http"] = True
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.chmod(path, 0o600)


def hook_command(hook_path: str, *flags: str) -> str:
    """Run the installed hook.py in place, so it always matches the adapter.

    `|| true`: if the install moves, Python exits 2 for the missing file, and
    Claude Code reads exit 2 from a hook as "block".
    """
    words = [shlex.quote(sys.executable), shlex.quote(hook_path), *flags]
    return " ".join(words) + " || true"


def powershell_hook_command(hook_path: str, *flags: str) -> str:
    """The same in-place hook for Windows, where Claude Code may have no bash.

    PowerShell 5.1 cannot parse `||`, and `exit 0` does its job here.
    """
    words = [_ps_quote(sys.executable), _ps_quote(hook_path), *flags]
    return "& " + " ".join(words) + "; exit 0"


def wake_hook_command(hook_path: str) -> str:
    """The FileChanged wake hook: the one command whose exit 2 must get through,
    since that is what wakes the session.

    Only the hook's own exit 2 does. A moved install (Python exits 2 for the
    missing file) and any other failure still end in exit 0.
    """
    python, script = shlex.quote(sys.executable), shlex.quote(hook_path)
    return (
        f"[ -f {script} ] || exit 0; {python} {script} --wake; "
        "[ $? -eq 2 ] && exit 2; exit 0"
    )


def powershell_wake_hook_command(hook_path: str) -> str:
    """The wake hook for Windows, in PowerShell 5.1 syntax."""
    python, script = _ps_quote(sys.executable), _ps_quote(hook_path)
    return (
        f"if (-not (Test-Path -LiteralPath {script})) {{ exit 0 }}; "
        f"& {python} {script} --wake; "
        "if ($LASTEXITCODE -eq 2) { exit 2 }; exit 0"
    )


def claude_hook_items(
    idle_wake: bool, windows: bool
) -> dict[str, list[dict[str, Any]]]:
    """Our hook items for each Claude Code event: exactly what setup writes.

    With idle wake, SessionStart creates the wake file and has Claude Code
    watch it, FileChanged runs the wake hook (the only asyncRewake entry) and
    then the plain hook, and that one and CwdChanged return the watch again
    whenever Claude Code would drop it.
    """
    command = powershell_hook_command if windows else hook_command
    wake_command = powershell_wake_hook_command if windows else wake_hook_command

    def item(text: str, **extra: Any) -> dict[str, Any]:
        made = {"type": "command", "command": text, **extra, "timeout": HOOK_TIMEOUT}
        if windows:
            made["shell"] = "powershell"
        return made

    plain = command(hook.__file__)
    items = {event: [item(plain)] for event in EVENTS}
    if idle_wake:
        items["SessionStart"] = [item(command(hook.__file__, "--idle-wake"))]
        items["FileChanged"] = [
            item(wake_command(hook.__file__), asyncRewake=True),
            item(plain),
        ]
        items["CwdChanged"] = [item(plain)]
    return items


def parse_claude_version(text: str) -> Version | None:
    """The first x.y.z in what `claude --version` printed."""
    found = VERSION.search(text)
    if found is None:
        return None
    major, minor, patch = (int(part) for part in found.groups())
    return major, minor, patch


def read_claude_version() -> Version | None:
    """The installed Claude Code's version; None when it cannot be asked."""
    program = shutil.which("claude")
    if program is None:
        return None
    try:
        done = subprocess.run(
            [program, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=VERSION_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_claude_version(done.stdout) if done.returncode == 0 else None


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def codex_hook_command(hook_path: str) -> str:
    """The same in-place hook, told it runs for Codex.

    The hook ignores arguments it does not know, so this works whether or not
    Codex runs the command through a shell.
    """
    return (
        f"{shlex.quote(sys.executable)} {shlex.quote(hook_path)} --agent codex"
        " || true"
    )


def _windows() -> bool:
    return os.name == "nt"


def _codex_hooks_path() -> Path:
    return Path.home() / ".codex" / "hooks.json"


def _claude_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _load_claude_settings() -> dict[str, Any]:
    """Read and shape-check settings.json before setup changes anything."""
    data = _load_hooks_file(_claude_settings_path())
    if not isinstance(data.get("deniedMcpServers", []), list):
        raise SetupError(
            f"{_claude_settings_path()}: deniedMcpServers should be a list; "
            "fix it and rerun."
        )
    return data


def connector_url_pattern(server: str) -> str:
    """A deniedMcpServers serverUrl pattern for every URL on the Kampung server,
    so the Claude app connector is denied whatever the user named it."""
    parts = urllib.parse.urlsplit(server)
    host = parts.netloc.rpartition("@")[2].lower()
    return f"{parts.scheme.lower()}://{host}/*"


def _deny_connector(data: dict[str, Any], connector_name: str, server: str) -> str:
    """Add the Claude app connector to deniedMcpServers (in place), by its
    name and by the server's URL, so Claude Code never loads a second kampung
    next to the adapter; return the name."""
    name = f"claude.ai {connector_name}"
    denied = data.setdefault("deniedMcpServers", [])
    for entry in ({"serverName": name}, {"serverUrl": connector_url_pattern(server)}):
        if entry not in denied:
            denied.append(entry)
    return name


def _load_hooks_file(path: Path) -> dict[str, Any]:
    """Read and shape-check a hooks file (Claude Code settings.json or Codex
    hooks.json share the shape) before setup changes anything."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text() or "{}")
    except ValueError as e:
        raise SetupError(f"{path} is not valid JSON ({e}); fix it and rerun.") from e
    hooks = data.get("hooks", {}) if isinstance(data, dict) else None
    if not isinstance(data, dict) or not isinstance(hooks, dict):
        raise SetupError(f"{path} has an unexpected shape; fix it and rerun.")
    for event in EVENTS + IDLE_WAKE_EVENTS:
        if not isinstance(hooks.get(event, []), list):
            raise SetupError(
                f"{path}: hooks.{event} should be a list; fix it and rerun."
            )
    return data


def _merge_claude_hooks(data: dict[str, Any], idle_wake: bool) -> Path:
    items = claude_hook_items(idle_wake, _windows())
    return _merge_hooks(_claude_settings_path(), data, items)


def _merge_codex_hooks(data: dict[str, Any]) -> Path:
    command = codex_hook_command(hook.__file__)
    timeouts = {"SessionEnd": CODEX_END_TIMEOUT}
    items = {
        event: [
            {
                "type": "command",
                "command": command,
                "timeout": timeouts.get(event, HOOK_TIMEOUT),
            }
        ]
        for event in EVENTS
    }
    return _merge_hooks(_codex_hooks_path(), data, items)


def _merge_hooks(
    path: Path, data: dict[str, Any], items: dict[str, list[dict[str, Any]]]
) -> Path:
    """Replace our entry for every event, keep everyone else's, back up once.

    Ours go from every event we ever wrote, also one that `items` leaves out:
    that is how a setup without idle wake removes an earlier run's entries.
    """
    if path.exists():
        backup = path.with_name(path.name + ".kampung-backup")
        if not backup.exists():
            shutil.copyfile(path, backup)
    hooks = data.setdefault("hooks", {})
    for event in EVENTS + IDLE_WAKE_EVENTS:
        stripped = (_strip_ours(e) for e in hooks.get(event, []))
        entries = [e for e in stripped if e is not None]
        if event in items:
            entries.append({"hooks": items[event]})
        if entries:
            hooks[event] = entries
        else:
            hooks.pop(event, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(path, data)
    return path


def _is_ours(hook_entry: object) -> bool:
    """Our hook: the installed hook.py, or the copy older setups made."""
    return isinstance(hook_entry, dict) and bool(
        OUR_HOOK.search(str(hook_entry.get("command", "")))
    )


def _strip_ours(entry: object) -> object | None:
    """The entry without our hooks; None if nothing of anyone else's is left.

    A matcher entry may hold someone else's hooks next to ours, so only our
    inner hooks go.
    """
    if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
        return entry
    others = [h for h in entry["hooks"] if not _is_ours(h)]
    if len(others) == len(entry["hooks"]):
        return entry
    return {**entry, "hooks": others} if others else None


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    """Write through a temp file and a rename, so a crash never leaves half a
    file. The rename goes to the resolved path, so a symlinked dotfile stays a
    symlink."""
    target = path.resolve()
    tmp = target.with_name(f"{target.name}.kampung-tmp{os.getpid()}")
    try:
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        if target.exists():
            shutil.copymode(target, tmp)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


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
    return manual_command(add, _windows())


def manual_command(cmd: list[str], windows: bool) -> str:
    """A command line the user can paste: PowerShell on Windows, else POSIX."""
    if not windows:
        return shlex.join(cmd)
    # PowerShell can eat a bare --, so quote it too.
    return " ".join(
        arg if SAFE_PS_ARG.fullmatch(arg) else "'" + arg.replace("'", "''") + "'"
        for arg in cmd
    )


def _check_key(server: str, key: str, allow_insecure_http: bool) -> None:
    # Before any request: a refused server is sent nothing at all.
    problem = hook.server_url_problem(server, allow_insecure_http)
    if problem is not None:
        raise SetupError(problem)
    if _status(f"{server}/health") != 200:
        raise SetupError(
            f"{server}/health did not answer 200; is this a Kampung server?"
        )
    status = _status(f"{server}/v1/sessions/0/peek", key)
    if status == 401:
        raise SetupError("The server rejected this key.")
    if status != 404:
        raise SetupError(f"Unexpected answer from the server (HTTP {status}).")


def set_key(key: str) -> list[str]:
    """Swap in a new key (after `kampung key rotate`), keeping everything else.

    The key is checked against the configured server before it is saved."""
    try:
        config = hook._read_config()
    except hook.HookError as e:
        raise SetupError(f"{e}. Run kampung-mcp setup first.") from e
    server = str(config["server"]).rstrip("/")
    allow = config.get("allow_insecure_http") is True
    _check_key(server, key, allow)
    _write_config(server, key, allow)
    return [
        f"Saved the new key to {hook.config_dir() / 'config.json'}.",
        "Restart Claude Code (and Codex) so running sessions use it.",
    ]


def _dotted(version: Version) -> str:
    return ".".join(str(part) for part in version)


def _idle_wake_line(claude_version: Version | None) -> str:
    """What setup says about idle wake for this Claude Code."""
    if claude_version is None:
        return (
            "Could not read the Claude Code version (claude --version), so idle "
            "wake is off. Run setup again once that works."
        )
    if claude_version < IDLE_WAKE_FLOOR:
        return (
            f"Claude Code {_dotted(claude_version)} is older than "
            f"{_dotted(IDLE_WAKE_FLOOR)}, so idle wake is off. Upgrade Claude "
            "Code and run setup again."
        )
    return (
        "Idle wake is on: an idle Claude Code session, also one in the "
        "background, wakes for mail within a few seconds. Restart open Claude "
        "Code sessions to load it."
    )


def run_setup(
    server: str,
    key: str,
    agents: list[str],
    runner: Runner,
    channels: bool = False,
    allow_insecure_http: bool = False,
    connector_name: str = CONNECTOR_NAME,
    claude_version: Version | None = None,
) -> list[str]:
    """`claude_version` is what `claude --version` said (None: unknown); idle
    wake is installed only from IDLE_WAKE_FLOOR on."""
    server = server.rstrip("/")
    _check_key(server, key, allow_insecure_http)

    claude_settings = _load_claude_settings() if "claude-code" in agents else None
    codex_hooks = (
        _load_hooks_file(_codex_hooks_path())
        if "codex" in agents and not _windows()
        else None
    )

    _write_config(server, key, allow_insecure_http)
    # Older setups copied the hook here; it now runs from the install.
    (hook.config_dir() / "kampung_hook.py").unlink(missing_ok=True)
    lines = [f"Saved server and key to {hook.config_dir() / 'config.json'}."]
    if allow_insecure_http:
        lines.insert(
            0,
            "Warning: --allow-insecure-http is on. Over plain http, anyone on "
            "the network path can copy this machine's key and act as it until "
            "you revoke it.",
        )

    if "claude-code" in agents:
        assert claude_settings is not None
        denied = _deny_connector(claude_settings, connector_name, server)
        idle_wake = claude_version is not None and claude_version >= IDLE_WAKE_FLOOR
        settings = _merge_claude_hooks(claude_settings, idle_wake)
        lines.append(
            f"Claude Code hooks added to {settings}, and the Claude app "
            f"connector ({denied}, or any connector to {server}) is denied "
            "there so a window never loads a second kampung."
        )
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
        lines.append(_idle_wake_line(claude_version))
        if channels:
            lines.append(
                "Channels push is on. Start Claude Code with: claude "
                "--dangerously-load-development-channels server:kampung "
                "(it asks you to confirm each time). Without that flag, mail "
                "still arrives through the hooks."
            )
        elif not idle_wake:
            lines.append(
                "An idle Claude Code session (waiting for you) won't notice mail "
                "until you type. To wake it, run setup again with --channels."
            )
    if "codex" in agents:
        adapter = _adapter_cmd("codex")
        if codex_hooks is None:
            lines.append(
                "Codex on Windows stays polling only for now: it reads mail "
                "when it calls inbox."
            )
        else:
            path = _merge_codex_hooks(codex_hooks)
            lines.append(
                f"Codex hooks added to {path}. Approve them once in Codex with "
                "/hooks; until then Codex only polls."
            )
            codex_bin = shutil.which("codex")
            if codex_bin is None:
                lines.append(
                    "codex was not found on PATH, so idle Codex sessions will "
                    "not be woken; run setup again once it is installed."
                )
            else:
                adapter += ["--codex-bin", codex_bin]
                lines.append(
                    "Idle Codex sessions wake on new mail. A closed or /clear'ed "
                    "Codex thread can still be woken and run unattended; see "
                    "docs/clients.md."
                )
        manual = _register(
            ["codex", "mcp", "remove", "kampung"],
            ["codex", "mcp", "add", "kampung", "--"] + adapter,
            runner,
        )
        lines.append(
            "Codex MCP server registered."
            if manual is None
            else f"Could not register with Codex. Run this yourself: {manual}"
        )
    removed = hook.sweep_stale_files()
    if removed:
        lines.append(f"Removed {removed} stale session file(s).")
    return lines
