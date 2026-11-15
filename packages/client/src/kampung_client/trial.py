"""`kampung-mcp try`: a throwaway server in Docker on this machine, connected
in one step, and `--stop` to remove it, its mail and the setup it made."""

import json
import re
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from . import __version__, hook, setup

CONTAINER = "kampung-try"
VOLUME = "kampung-try-data"
IMAGE = "ghcr.io/jztan/kampung-mcp"
DEFAULT_PORT = 8803
HEALTH_WAIT = 30.0  # seconds for a fresh container to answer /health
AGENT_PROGRAMS = (("claude-code", "claude"), ("codex", "codex"))
_NOT_NAME = re.compile(r"[^a-z0-9._-]+")


class TryError(Exception):
    pass


@dataclass
class Done:
    returncode: int
    stdout: str
    stderr: str


Docker = Callable[[list[str]], Done]
SetupFn = Callable[[str, str, list[str]], list[str]]


def docker_cli(args: list[str]) -> Done:
    program = shutil.which("docker")
    if program is None:
        raise TryError(
            "Docker is not installed. kampung-mcp try runs the server in Docker; "
            "install Docker Desktop (or Docker Engine) and run it again."
        )
    done = subprocess.run(
        [program, *args], capture_output=True, text=True, encoding="utf-8"
    )
    return Done(done.returncode, done.stdout, done.stderr)


def machine_name(hostname: str) -> str:
    """This machine's name on the trial server, from its hostname."""
    first = hostname.split(".")[0].lower()
    name = _NOT_NAME.sub("-", first).strip("-._")[:64]
    return name or "laptop"


def server_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def _connected_server() -> str | None:
    try:
        data = json.loads((hook.config_dir() / "config.json").read_text())
    except (OSError, ValueError):
        return None
    server = data.get("server") if isinstance(data, dict) else None
    return str(server).rstrip("/") if server else None


def wait_for_health(url: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def _last_line(text: str) -> str:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[-1] if lines else "no output"


def _run_container(port: int, version: str, docker: Docker) -> None:
    state = docker(["inspect", "--format", "{{.State.Running}}", CONTAINER])
    if state.returncode == 0:
        if state.stdout.strip() != "true":
            started = docker(["start", CONTAINER])
            if started.returncode != 0:
                raise TryError(
                    f"Could not start the {CONTAINER} container: "
                    f"{_last_line(started.stderr)}"
                )
        return
    run = docker(
        [
            "run",
            "-d",
            "--name",
            CONTAINER,
            "--restart",
            "unless-stopped",
            "-p",
            f"127.0.0.1:{port}:8000",
            "-v",
            f"{VOLUME}:/data",
            # Nothing forwards for this server, so trust no X-Forwarded-For.
            "-e",
            "KAMPUNG_FORWARDED_ALLOW_IPS=127.0.0.1",
            "--read-only",
            "--tmpfs",
            "/tmp",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            f"{IMAGE}:{version}",
        ]
    )
    if run.returncode != 0:
        raise TryError(f"Could not start the server: {_last_line(run.stderr)}")


def _machine_key(name: str, docker: Docker) -> str:
    listing = docker(["exec", CONTAINER, "kampung", "key", "list"])
    known = {line.split()[0] for line in listing.stdout.splitlines() if line.strip()}
    verb = "rotate" if name in known else "add"
    made = docker(["exec", CONTAINER, "kampung", "key", verb, name])
    key = made.stdout.strip()
    if made.returncode != 0 or not key:
        raise TryError(f"Could not make a key: {_last_line(made.stderr)}")
    return key.splitlines()[-1]


def start(
    port: int,
    agents: list[str] | None,
    docker: Docker,
    wait_health: Callable[[str, float], bool],
    setup_fn: SetupFn,
    version: str,
    hostname: str,
    which: Callable[[str], str | None],
) -> list[str]:
    url = server_url(port)
    connected = _connected_server()
    if connected is not None and connected != url:
        raise TryError(
            f"This machine is already connected to {connected}, and try would "
            "replace that. Nothing was changed."
        )
    if agents is None:
        agents = [agent for agent, program in AGENT_PROGRAMS if which(program)]
        if not agents:
            raise TryError(
                "Found neither claude nor codex on PATH. Install Claude Code or "
                "Codex first, or name one with --agent."
            )
    if docker(["info", "--format", "{{.ServerVersion}}"]).returncode != 0:
        raise TryError(
            "Docker is not running. Start Docker Desktop (or the docker service) "
            "and run kampung-mcp try again."
        )
    _run_container(port, version, docker)
    if not wait_health(url, HEALTH_WAIT):
        raise TryError(
            f"The server did not answer on {url}/health within "
            f"{HEALTH_WAIT:.0f} seconds. See: docker logs {CONTAINER}"
        )
    name = machine_name(hostname)
    lines = setup_fn(url, _machine_key(name, docker), agents)
    return lines + ["", *next_steps(name, url, agents)]


def next_steps(name: str, url: str, agents: list[str]) -> list[str]:
    if agents == ["codex"]:
        where = "Codex in two repo folders"
    elif agents == ["claude-code"]:
        where = "Claude Code in two repo folders"
    else:
        where = "Claude Code in one repo folder and Codex in another"
    lines = [
        f"A trial server is running at {url} (container {CONTAINER}); this "
        f"machine is {name} on it.",
        f"Try it: start {where} (say ~/src/api and ~/src/blog), then ask the "
        f"one in api to send hello to {name}/blog. Each repo folder is a "
        f"mailbox, {name}/<folder>.",
    ]
    if "codex" in agents:
        lines.append("In Codex, approve the Kampung hooks once with /hooks.")
    stop = "kampung-mcp try --stop"
    if url != server_url(DEFAULT_PORT):
        stop += f" --port {url.rsplit(':', 1)[-1]}"
    lines.append(f"When you are done: {stop}")
    return lines


def stop(docker: Docker, uninstall: Callable[[], list[str]], port: int) -> list[str]:
    """Remove the container and its mail, and undo setup if this machine is
    connected to the trial server; a machine connected elsewhere keeps that.
    Docker is checked first, so a stopped Docker changes nothing."""
    if docker(["info", "--format", "{{.ServerVersion}}"]).returncode != 0:
        raise TryError(
            "Docker is not running, so the trial server cannot be removed. "
            "Start Docker and run kampung-mcp try --stop again. Nothing was "
            "changed."
        )
    trial_urls = {server_url(port)}
    published = _published_port(docker)
    if published is not None:
        trial_urls.add(server_url(published))
    lines = []
    if _connected_server() in trial_urls:
        lines += uninstall()
    else:
        lines.append(
            "This machine is not connected to the trial server, so its hooks "
            "and key were left alone."
        )
    failed = [
        what
        for what, args in (
            (f"the {CONTAINER} container", ["rm", "-f", CONTAINER]),
            (f"the {VOLUME} volume (its mail)", ["volume", "rm", "-f", VOLUME]),
        )
        if docker(args).returncode != 0
    ]
    if failed:
        raise TryError(
            "\n".join(
                lines
                + [
                    f"Could not remove {' or '.join(failed)}; run "
                    "kampung-mcp try --stop again."
                ]
            )
        )
    lines.append(f"Removed the {CONTAINER} container and its mail.")
    return lines


def _published_port(docker: Docker) -> int | None:
    """The host port the trial container is published on, if it exists."""
    done = docker(["port", CONTAINER, "8000/tcp"])
    if done.returncode != 0:
        return None
    for line in done.stdout.splitlines():
        found = re.search(r"127\.0\.0\.1:(\d+)$", line.strip())
        if found:
            return int(found.group(1))
    return None


def _default_setup(server: str, key: str, agents: list[str]) -> list[str]:
    return setup.run_setup(
        server,
        key,
        agents,
        setup.default_runner,
        claude_version=(
            setup.read_claude_version() if "claude-code" in agents else None
        ),
    )


def start_default(port: int, agents: list[str] | None) -> list[str]:
    return start(
        port=port,
        agents=agents,
        docker=docker_cli,
        wait_health=wait_for_health,
        setup_fn=_default_setup,
        version=__version__,
        hostname=socket.gethostname(),
        which=shutil.which,
    )


def stop_default(port: int) -> list[str]:
    return stop(docker_cli, lambda: setup.uninstall(setup.default_runner), port)
