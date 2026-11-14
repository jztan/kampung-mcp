"""`kampung-mcp doctor`: find what the fail-open hooks would never report.

Reads only: it writes no file and registers no session, and it never prints
the machine key.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import stat
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import __version__, codex_launch, hook, setup

OK = "ok"
WARN = "WARN"
FAIL = "FAIL"

Query = Callable[[list[str]], tuple[int, str]]
HTTP_TIMEOUT = 5  # seconds
MCP_TIMEOUT = 30  # seconds; `claude mcp get` starts the adapter to test it
LOG_WINDOW = timedelta(hours=24)
# Logged whenever a window outlives its session file; says nothing is broken.
LOG_EXPECTED = "no session file for this session"
LOG_LINE_MAX = 120
MCP_BROKEN = re.compile(r"failed to connect|enabled:\s*false", re.IGNORECASE)
REGISTERED_COMMAND = re.compile(r"^\s*command:\s*(.+?)\s*$", re.I | re.M)
MISSING_MODULE = re.compile(r"No module named '([^']+)'")
# What the adapter imports at start, and the version it would run.
INTERPRETER_PROBE = (
    "import kampung_client, kampung_client.adapter; print(kampung_client.__version__)"
)
# The plain hook command in its PowerShell form: interpreter, script, flags.
POWERSHELL_HOOK = re.compile(
    r"& '((?:[^']|'')+)' '((?:[^']|'')+)'((?: --[a-z-]+(?: [a-z-]+)?)*); exit 0"
)
# Seconds a wake may wait for its answer before doctor calls it missed. The
# adapter gives up on one after two rounds of 5.
WAKE_SETTLE = 15.0


@dataclass(frozen=True)
class Check:
    status: str
    name: str
    detail: str
    fix: str = ""


def default_query(cmd: list[str]) -> tuple[int, str]:
    """Exit code and output of an agent CLI; OSError if it can't be asked."""
    program = shutil.which(cmd[0])
    if program is None:
        raise FileNotFoundError(cmd[0])
    try:
        done = subprocess.run(
            [program, *cmd[1:]],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=MCP_TIMEOUT,
        )
    except subprocess.TimeoutExpired as e:
        raise TimeoutError(str(e)) from e
    return done.returncode, done.stdout + done.stderr


def _config_check() -> tuple[Check, dict[str, str] | None]:
    try:
        config = hook.load_config()
    except hook.InsecureServerError as e:
        return Check(FAIL, "config", str(e), "Run kampung-mcp setup again."), None
    except hook.HookError as e:
        return Check(FAIL, "config", str(e), "Run kampung-mcp setup."), None
    details = []
    fixes = []
    insecure = hook.insecure_http_warning()
    if insecure is not None:
        details.append(insecure.removeprefix("Warning: "))
    path = hook.config_dir() / "config.json"
    if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        details.append(f"{path} is readable by other users")
        fixes.append(f"Run chmod 600 {shlex.quote(str(path))}.")
    if details:
        return Check(WARN, "config", "; ".join(details), " ".join(fixes)), config
    return Check(OK, "config", config["server"]), config


def _health(server: str) -> tuple[int, str]:
    """Status of /health and the version it reports ("" if it reports none)."""
    request = urllib.request.Request(f"{server}/health")
    try:
        with hook.open_url(request, HTTP_TIMEOUT) as response:
            body = hook._json(response.read())
            return int(response.status), str(body.get("version", ""))
    except urllib.error.HTTPError as e:
        return e.code, ""


def _server_checks(config: dict[str, str]) -> list[Check]:
    server = config["server"]
    try:
        status, version = _health(server)
    except (urllib.error.URLError, OSError) as e:
        fix = "Check that the server is up and the address is right."
        if "CERTIFICATE" in str(e):
            fix = "Set SSL_CERT_FILE to a bundle with the network's certificate."
        return [Check(FAIL, "server", f"cannot reach {server}: {e}", fix)]
    if status != 200:
        return [
            Check(
                FAIL,
                "server",
                f"{server}/health answered HTTP {status}",
                "Is this a Kampung server? Check the proxy in front of it.",
            )
        ]
    return [
        Check(OK, "server", f"{server} is reachable"),
        _key_check(server, config["key"]),
        _version_check(version),
    ]


def _key_check(server: str, key: str) -> Check:
    try:
        # No session 0 exists: a good key gets 404, a bad one never gets past 401.
        status = setup._status(f"{server}/v1/sessions/0/peek", key)
    except setup.SetupError as e:
        return Check(FAIL, "key", str(e))
    if status == 404:
        return Check(OK, "key", "the server accepts this machine's key")
    if status == 401:
        return Check(
            FAIL,
            "key",
            "the server rejected this machine's key",
            "Run kampung key rotate on the server, then kampung-mcp set-key here.",
        )
    return Check(FAIL, "key", f"unexpected answer from the server (HTTP {status})")


def _version_check(server_version: str) -> Check:
    if not server_version:
        return Check(
            WARN, "version", f"client {__version__}, server did not say which it runs"
        )
    detail = f"client {__version__}, server {server_version}"
    if __version__.split(".")[:2] == server_version.split(".")[:2]:
        return Check(OK, "version", detail)
    return Check(
        WARN,
        "version",
        detail,
        "Upgrade the older side: uv tool upgrade kampung-mcp here, or pull the "
        "new image on the server.",
    )


def _our_items(data: dict[str, Any], event: str) -> list[dict[str, Any]]:
    """Our hook items on one event, in order."""
    items = []
    for entry in data.get("hooks", {}).get(event, []):
        inner = entry.get("hooks") if isinstance(entry, dict) else None
        for item in inner if isinstance(inner, list) else []:
            if setup._is_ours(item):
                items.append(item)
    return items


def _our_commands(data: dict[str, Any]) -> dict[str, str]:
    """Our hook command for each event that has one."""
    commands = {}
    for event in setup.EVENTS:
        for item in _our_items(data, event):
            commands[event] = str(item["command"])
    return commands


def _try_hook(command: str) -> tuple[str, str]:
    """(status, detail) of running a hook command with an event it ignores.

    `|| true` is cut off first: it is what hides a broken hook from the agent.
    `; exit 0` does the same in the PowerShell form.
    """
    powershell = POWERSHELL_HOOK.fullmatch(command)
    if powershell:
        args = [part.replace("''", "'") for part in powershell.groups()[:2]]
        args += powershell.group(3).split()
    else:
        try:
            args = shlex.split(command)
        except ValueError as e:
            return FAIL, f"cannot read the hook command: {e}"
        if "||" in args:
            args = args[: args.index("||")]
    if len(args) < 2:
        return FAIL, f"unexpected hook command: {command}"
    script = Path(args[1])
    if not script.exists():
        return FAIL, f"the hook script {script} is gone"
    try:
        done = subprocess.run(
            args,
            input="{}",
            capture_output=True,
            text=True,
            timeout=setup.HOOK_TIMEOUT,
        )
    except OSError as e:
        return FAIL, f"cannot run {args[0]}: {e}"
    except subprocess.TimeoutExpired:
        return FAIL, f"the hook took over {setup.HOOK_TIMEOUT} seconds"
    if done.returncode != 0:
        return FAIL, f"the hook failed with exit {done.returncode}"
    if script.resolve() != Path(hook.__file__).resolve():
        return WARN, f"hooks run {script}, but this command runs {hook.__file__}"
    return OK, ""


def _hooks_check(agent: str, commands: dict[str, str], fix: str) -> Check:
    name = f"{agent} hooks"
    missing = [event for event in setup.EVENTS if event not in commands]
    if missing:
        return Check(FAIL, name, f"missing for {', '.join(missing)}", fix)
    for command in sorted(set(commands.values())):
        status, detail = _try_hook(command)
        if status != OK:
            if status == WARN:
                fix = f"Expected from a checkout; if not, {fix[0].lower()}{fix[1:]}"
            return Check(status, name, detail, fix)
    detail = f"all {len(setup.EVENTS)} events run"
    if agent == "codex":
        detail += "; Codex also needs them approved once with /hooks"
    return Check(OK, name, detail)


def _has_flag(item: dict[str, Any], flag: str) -> bool:
    """Whether a hook item's command passes this flag, in either shell's form
    (`--wake;` in one, `--idle-wake ||` in the other)."""
    command = str(item.get("command", ""))
    return re.search(rf"(?<!\S){re.escape(flag)}(?![\w-])", command) is not None


def _idle_wake_installed(data: dict[str, Any]) -> bool:
    """Whether setup's idle wake entries are there: SessionStart makes the wake
    file, FileChanged has the wake hook and the plain one, CwdChanged has the
    plain one. Read by shape, not by path, so hooks written by another install
    of the client count (the hooks check names that case)."""
    changed = _our_items(data, "FileChanged")
    return (
        any(_has_flag(i, "--idle-wake") for i in _our_items(data, "SessionStart"))
        and any(
            i.get("asyncRewake") is True and _has_flag(i, "--wake") for i in changed
        )
        and any(i.get("asyncRewake") is not True for i in changed)
        and bool(_our_items(data, "CwdChanged"))
    )


def _claude_version(query: Query) -> setup.Version | None:
    try:
        code, output = query(["claude", "--version"])
    except OSError:
        return None
    return setup.parse_claude_version(output) if code == 0 else None


def _last_wake(f: dict[str, object]) -> str:
    """How the session's last file wake went: "confirmed", "missed", or ""
    (none sent yet, one still in flight, or no wake file at all)."""
    sid = f.get("agent_session_id")
    if not isinstance(sid, str):
        return ""
    path = hook.wake_path(sid)
    token = hook.read_token(path)
    if not token:
        return ""
    if hook.read_token(hook.ack_path(sid)) == token:
        return "confirmed"
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return ""
    return "missed" if age > WAKE_SETTLE else ""


def _idle_wake_check(data: dict[str, Any], query: Query) -> Check:
    """Whether an idle Claude Code session wakes for mail without Channels."""
    name = "idle wake"
    version = _claude_version(query)
    floor = setup._dotted(setup.IDLE_WAKE_FLOOR)
    older = ""  # set when Claude Code is below the floor
    if version is not None and version < setup.IDLE_WAKE_FLOOR:
        older = f"Claude Code {setup._dotted(version)} is older than {floor}"
    if not _idle_wake_installed(data):
        if version is None:
            return Check(
                WARN,
                name,
                "off: could not read the Claude Code version",
                "Run kampung-mcp setup again once claude --version works.",
            )
        if older:
            return Check(
                WARN,
                name,
                f"off: {older}",
                "Upgrade Claude Code, then run kampung-mcp setup again.",
            )
        return Check(
            WARN,
            name,
            "off: the idle wake hooks are not installed",
            "Run kampung-mcp setup again.",
        )
    if older:
        return Check(
            WARN, name, f"{older}; idle wake may not work", "Upgrade Claude Code."
        )
    files = [f for f in _open_sessions() if f.get("agent") == "claude-code"]
    wakes = [_last_wake(f) for f in files]
    missed = [
        f"{f.get('mailbox')}#{f.get('suffix')}"
        for f, wake in zip(files, wakes)
        if wake == "missed"
    ]
    if missed:
        return Check(
            WARN,
            name,
            f"{', '.join(missed)}: the last wake was not confirmed",
            "Idle wake needs the Claude Code terminal CLI (not the VS Code "
            "extension or the desktop app). Restart the session; see hooks.log.",
        )
    return Check(
        OK,
        name,
        f"on; {len(files)} open session(s), {wakes.count('confirmed')} with a "
        "confirmed wake",
    )


def _mcp_check(agent: str, program: str, query: Query, fix: str) -> Check | None:
    """The MCP registration; None when the agent has none at all."""
    name = f"{agent} mcp"
    try:
        code, output = query([program, "mcp", "get", "kampung"])
    except FileNotFoundError:
        return Check(WARN, name, f"{program} is not on PATH, so this went unchecked")
    except OSError as e:
        return Check(WARN, name, f"could not ask {program}: {e}")
    if code != 0:
        return None
    registered = REGISTERED_COMMAND.search(output)
    problem = (
        _interpreter_problem(name, registered.group(1).strip(), query)
        if registered is not None
        else None
    )
    if problem is not None and problem.status == FAIL:
        return problem
    if MCP_BROKEN.search(output):
        # A version warning must not turn a broken registration into a pass.
        detail = f"registered, but {program} reports it as not working"
        if problem is not None:
            return Check(FAIL, name, f"{detail}; {problem.detail}", problem.fix)
        return Check(FAIL, name, detail, fix)
    return problem or Check(OK, name, f"registered with {program}")


def _interpreter_problem(name: str, python: str, query: Query) -> Check | None:
    """Run the registered interpreter the way the adapter starts: an agent
    shows only "connection closed" when the import fails, so name the cause."""
    if "python" not in Path(python).name.lower():
        return None  # not what setup registers; nothing to say about it
    checkout = _checkout_of(python)
    fix = (
        f"Run uv sync in {checkout}."
        if checkout is not None
        else "Run uv tool upgrade kampung-mcp, then kampung-mcp setup."
    )
    try:
        code, output = query([python, "-c", INTERPRETER_PROBE])
    except FileNotFoundError:
        return Check(
            FAIL,
            name,
            f"the registered interpreter {python} does not exist",
            "Run kampung-mcp setup --server URL with the flags you used before.",
        )
    except OSError as e:
        return Check(WARN, name, f"could not run {python}: {e}")
    if code != 0:
        missing = MISSING_MODULE.search(output)
        cause = (
            f"No module named '{missing.group(1)}'"
            if missing
            else (output.strip().splitlines() or [f"exit {code}"])[-1][:LOG_LINE_MAX]
        )
        return Check(
            FAIL, name, f"the adapter cannot start under {python}: {cause}", fix
        )
    version = output.strip().splitlines()[-1] if output.strip() else ""
    if version != __version__:
        return Check(
            WARN,
            name,
            f"{python} runs kampung-mcp {version or 'of an unknown version'}, "
            f"but this doctor is {__version__}",
            fix,
        )
    return None


def _checkout_of(python: str) -> Path | None:
    """The folder holding the .venv the interpreter is in, if any: a checkout
    run with uv, whose dependencies lag until uv sync."""
    parts = Path(python).parts
    if ".venv" not in parts:
        return None
    return Path(*parts[: parts.index(".venv")])


def _agent_checks(
    agent: str, program: str, hooks_file: Path, query: Query
) -> list[Check]:
    """Checks for one agent, or nothing if it was never set up on this machine."""
    fix = f"Run kampung-mcp setup --server URL --agent {agent}."
    mcp = _mcp_check(agent, program, query, fix)
    try:
        data = setup._load_hooks_file(hooks_file)
    except setup.SetupError as e:
        return [c for c in (Check(FAIL, f"{agent} hooks", str(e)), mcp) if c]
    commands = _our_commands(data)
    registered = mcp is not None and mcp.status != WARN
    if not commands and not registered:
        return []
    if mcp is None:
        mcp = Check(FAIL, f"{agent} mcp", f"not registered with {program}", fix)
    if agent == "codex" and setup._windows():  # no control socket to bind by
        return [_hooks_check(agent, commands, fix), mcp]
    if agent == "codex":
        return [_hooks_check(agent, commands, fix), mcp, _codex_launch_check()]
    if not commands:  # no hooks of ours: the hooks check says so, once
        return [_hooks_check(agent, commands, fix), mcp]
    return [_hooks_check(agent, commands, fix), _idle_wake_check(data, query), mcp]


def _url_denied(pattern: object, url: str) -> bool:
    """Whether a deniedMcpServers serverUrl pattern (with * wildcards) covers
    url. Like Claude Code, scheme and host ignore case and paths keep it;
    ports are not treated specially, which is close enough for a check."""
    if not isinstance(pattern, str) or not pattern:
        return False
    if pattern.rstrip("/").count("/") == 2:  # no path: Claude matches any path
        pattern = pattern.rstrip("/") + "/*"

    def wild(part: str) -> str:
        return ".*".join(re.escape(piece) for piece in part.split("*"))

    scheme, sep, rest = pattern.partition("://")
    if not sep:
        return False  # not a URL pattern; Claude Code would not match it either
    host, slash, path = rest.partition("/")
    regex = f"(?i:{wild(scheme + sep + host)}){wild(slash + path)}"
    return re.fullmatch(regex, url) is not None


def _connector_check(server: str) -> Check:
    """Whether Claude Code is kept from loading the Claude app connector, which
    would act as <name>/claude in this window and take the phone's mail."""
    name = "claude-code connector"
    fix = "Rerun kampung-mcp setup --server URL with the flags you used before."
    try:
        data = setup._load_claude_settings()
    except setup.SetupError as e:
        return Check(WARN, name, str(e))
    if data.get("disableClaudeAiConnectors") is True:
        return Check(OK, name, "Claude Code loads no claude.ai connectors")
    denied = [d for d in data.get("deniedMcpServers", []) if isinstance(d, dict)]
    url = f"{server.rstrip('/')}/mcp"
    if any(_url_denied(d.get("serverUrl"), url) for d in denied):
        return Check(OK, name, f"a claude.ai connector to {server} is denied")
    names = [
        d["serverName"]
        for d in denied
        if str(d.get("serverName", "")).startswith("claude.ai ")
    ]
    if names:
        return Check(
            WARN,
            name,
            f"only {', '.join(names)} is denied, so a Kampung connector under "
            "another name still loads here and can take the phone's mail",
            fix,
        )
    return Check(
        WARN,
        name,
        "nothing denies the Claude app connector, so if you added one, every "
        "window loads it as a second kampung that can take the phone's mail",
        fix,
    )


def _codex_launch_check() -> Check:
    """Whether a new Codex window can be bound at launch: the daemon's control
    socket must answer. Without it, windows still bind on their first prompt."""
    name = "codex launch"
    path = codex_launch.control_socket()
    try:
        exists = path.exists()
    except OSError:  # a sandbox or odd permissions: say so, never crash doctor
        exists = True
    state = codex_launch.probe_socket(path) if exists else "not running"
    if state == "not running":
        return Check(OK, name, "Codex is not running, so this went unchecked")
    if state == "answers":
        return Check(
            OK, name, "new Codex windows are listed and woken before their first prompt"
        )
    return Check(
        WARN,
        name,
        "the Codex control socket does not answer, so a new Codex window is "
        "reachable only after its first prompt",
        "Restart Codex. If it persists, a Codex update changed its control "
        "protocol; windows still bind on their first prompt.",
    )


def _log_check() -> Check:
    path = hook.cache_dir() / "hooks.log"
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return Check(OK, "hooks.log", "no failures logged")
    cutoff = datetime.now(timezone.utc) - LOG_WINDOW
    recent = []
    for line in lines:
        stamp, _, rest = line.partition(" ")
        try:
            logged = datetime.fromisoformat(stamp)
            if logged >= cutoff and LOG_EXPECTED not in rest:
                recent.append(rest)
        except (ValueError, TypeError):  # not a log line, or a stamp without a zone
            continue
    if not recent:
        return Check(OK, "hooks.log", "no recent failures")
    return Check(
        WARN,
        "hooks.log",
        f"{len(recent)} failure(s) in the last 24 hours; latest: "
        f"{recent[-1][:LOG_LINE_MAX]}",
        f"See {path}.",
    )


def _open(f: dict[str, object]) -> bool:
    """Whether the session file's agent process still runs; unknown counts."""
    ancestors = f.get("ancestors")
    if not isinstance(ancestors, list) or not ancestors:
        return True
    first = ancestors[0]
    if not isinstance(first, list) or len(first) != 2:
        return True
    try:
        live = hook._lookup(int(first[0]))
    except (hook.LookupFailed, ValueError, TypeError):
        return True
    return live is not None and live[1] == first[1]


def _open_sessions() -> list[dict[str, object]]:
    """The session files of hooked sessions whose agent still runs."""
    return [f for f in hook.list_session_files() if f.get("hooks") and _open(f)]


def _sessions_check() -> Check:
    """The sessions this machine's hooks registered, with background ones named:
    a background session gets no Channels push and leaves mailbox mail to a
    window in the same mailbox."""
    files = _open_sessions()
    if not files:
        return Check(OK, "sessions", "no open sessions")
    background = [f for f in files if f.get("background") is True]
    windows = len(files) - len(background)
    detail = f"{windows} window(s), {len(background)} background"
    if background:
        names = ", ".join(f"{f.get('mailbox')}#{f.get('suffix')}" for f in background)
        detail += (
            f" ({names}: background sessions get no Channels push and leave "
            "mailbox mail to a window there; with idle wake on they wake like "
            "any other)"
        )
    return Check(OK, "sessions", detail)


def run_checks(query: Query) -> list[Check]:
    config_check, config = _config_check()
    checks = [config_check]
    if config is not None:
        checks += _server_checks(config)
    agents = _agent_checks(
        "claude-code", "claude", setup._claude_settings_path(), query
    ) + _agent_checks("codex", "codex", setup._codex_hooks_path(), query)
    names = [c.name for c in agents]
    if config is not None and "claude-code mcp" in names:
        at = names.index("claude-code mcp") + 1
        agents.insert(at, _connector_check(config["server"]))
    if not agents:
        agents = [
            Check(
                FAIL,
                "agents",
                "neither Claude Code nor Codex is set up on this machine",
                "Run kampung-mcp setup --server URL.",
            )
        ]
    return checks + agents + [_sessions_check(), _log_check()]


def main() -> int:
    """Print every check; exit 1 if any failed. Warnings alone exit 0."""
    checks = run_checks(default_query)
    print(f"kampung-mcp {__version__} doctor")
    for check in checks:
        print(f"{check.status:<5} {check.name}: {check.detail}")
        if check.fix:
            print(f"      Fix: {check.fix}")
    problems = sum(c.status == FAIL for c in checks)
    warnings = sum(c.status == WARN for c in checks)
    if problems or warnings:
        print(f"{problems} problem(s), {warnings} warning(s).")
    else:
        print("All checks passed.")
    return 1 if problems else 0
