"""`kampung-mcp try`: a local trial server in Docker, connected in one step."""

import json
from pathlib import Path

import pytest

from kampung_client import cli, trial

URL = "http://127.0.0.1:8803"


class FakeDocker:
    """Answers `docker <args>` from a script and records every call."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.running: bool | None = None  # None: no container yet
        self.machines: list[str] = []
        self.daemon_up = True
        self.run_fails = False
        self.published: int | None = None  # host port of an existing container
        self.remove_fails = False

    def __call__(self, args: list[str]) -> trial.Done:
        self.calls.append(args)
        if args[0] == "info":
            return trial.Done(0 if self.daemon_up else 1, "", "Cannot connect")
        if args[0] == "inspect":
            if self.running is None:
                return trial.Done(1, "", "No such object")
            return trial.Done(0, "true\n" if self.running else "false\n", "")
        if args[0] == "run":
            if self.run_fails:
                return trial.Done(125, "", "pull access denied\nmanifest unknown")
            self.running = True
            return trial.Done(0, "abc123\n", "")
        if args[0] == "start":
            self.running = True
            return trial.Done(0, "", "")
        if args[:5] == ["exec", trial.CONTAINER, "kampung", "key", "list"]:
            out = "".join(f"{m}  2026-11-14T00:00:00Z  active\n" for m in self.machines)
            return trial.Done(0, out, "")
        if args[:4] == ["exec", trial.CONTAINER, "kampung", "key"]:
            self.machines.append(args[5])
            return trial.Done(0, f"kpg_{args[4]}_key\n", "Machine created.")
        if args[:2] == ["port", trial.CONTAINER]:
            if self.published is None:
                return trial.Done(1, "", "No such container")
            return trial.Done(0, f"127.0.0.1:{self.published}\n", "")
        if args[:3] == ["rm", "-f", trial.CONTAINER]:
            if self.remove_fails:
                return trial.Done(1, "", "Cannot connect")
            self.running = None
            return trial.Done(0, "", "")
        if args[:3] == ["volume", "rm", "-f"]:
            return trial.Done(1 if self.remove_fails else 0, "", "")
        raise AssertionError(f"unexpected docker call: {args}")

    def first(self, verb: str) -> list[str]:
        return next(c for c in self.calls if c[0] == verb)


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, list[str]]] = []

    def __call__(self, server: str, key: str, agents: list[str]) -> list[str]:
        self.calls.append((server, key, agents))
        return ["Saved server and key."]


def _start(
    docker: FakeDocker,
    setup_fn: Recorder,
    agents: list[str] | None = None,
    healthy: bool = True,
    on_path: tuple[str, ...] = ("claude", "codex"),
) -> list[str]:
    return trial.start(
        port=8803,
        agents=agents,
        docker=docker,
        wait_health=lambda url, timeout: healthy,
        setup_fn=setup_fn,
        version="0.7.0",
        hostname="Kevins-MacBook.local",
        which=lambda prog: f"/usr/bin/{prog}" if prog in on_path else None,
    )


def test_start_runs_the_matching_image_locked_down_on_loopback(home: Path) -> None:
    docker, setup_fn = FakeDocker(), Recorder()
    _start(docker, setup_fn)
    run = docker.first("run")
    assert run[-1] == "ghcr.io/jztan/kampung-mcp:0.7.0"
    assert ["-p", "127.0.0.1:8803:8000"] == run[run.index("-p") : run.index("-p") + 2]
    assert f"{trial.VOLUME}:/data" in run
    for flag in ("--read-only", "--cap-drop", "--security-opt"):
        assert flag in run
    assert "KAMPUNG_FORWARDED_ALLOW_IPS=127.0.0.1" in run


def test_start_makes_a_key_and_hands_it_to_setup_without_printing_it(
    home: Path,
) -> None:
    docker, setup_fn = FakeDocker(), Recorder()
    lines = _start(docker, setup_fn)
    assert setup_fn.calls == [(URL, "kpg_add_key", ["claude-code", "codex"])]
    assert ["exec", trial.CONTAINER, "kampung", "key", "add", "kevins-macbook"] in (
        docker.calls
    )
    assert not any("kpg_add_key" in line for line in lines)


def test_start_names_the_mailboxes_in_its_next_steps(home: Path) -> None:
    lines = _start(FakeDocker(), Recorder())
    assert any("kevins-macbook/" in line for line in lines)


def test_a_rerun_reuses_the_container_and_rotates_the_key(home: Path) -> None:
    docker, setup_fn = FakeDocker(), Recorder()
    docker.running = False
    docker.machines = ["kevins-macbook"]
    _start(docker, setup_fn)
    assert not any(c[0] == "run" for c in docker.calls)
    assert ["start", trial.CONTAINER] in docker.calls
    assert setup_fn.calls[0][1] == "kpg_rotate_key"


def test_agents_default_to_what_is_on_path(home: Path) -> None:
    setup_fn = Recorder()
    _start(FakeDocker(), setup_fn, on_path=("codex",))
    assert setup_fn.calls[0][2] == ["codex"]


def test_no_agent_on_path_is_refused_before_docker(home: Path) -> None:
    docker = FakeDocker()
    with pytest.raises(trial.TryError, match="neither claude nor codex"):
        _start(docker, Recorder(), on_path=())
    assert docker.calls == []


def test_docker_not_running_is_refused(home: Path) -> None:
    docker = FakeDocker()
    docker.daemon_up = False
    with pytest.raises(trial.TryError, match="Docker is not running"):
        _start(docker, Recorder())


def test_a_failed_image_pull_says_why(home: Path) -> None:
    docker = FakeDocker()
    docker.run_fails = True
    with pytest.raises(trial.TryError, match="manifest unknown"):
        _start(docker, Recorder())


def test_a_server_that_never_answers_points_at_its_logs(home: Path) -> None:
    with pytest.raises(trial.TryError, match="docker logs kampung-try"):
        _start(FakeDocker(), Recorder(), healthy=False)


def test_a_machine_connected_elsewhere_is_left_alone(home: Path) -> None:
    config = home / ".config" / "kampung" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"server": "https://k.example.com", "key": "x"}))
    docker, setup_fn = FakeDocker(), Recorder()
    with pytest.raises(trial.TryError, match="https://k.example.com"):
        _start(docker, setup_fn)
    assert docker.calls == [] and setup_fn.calls == []


def test_a_machine_already_on_the_trial_server_may_rerun(home: Path) -> None:
    config = home / ".config" / "kampung" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"server": URL + "/", "key": "x"}))
    setup_fn = Recorder()
    _start(FakeDocker(), setup_fn)
    assert len(setup_fn.calls) == 1


def _connect(home: Path, server: str) -> None:
    config = home / ".config" / "kampung" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"server": server, "key": "x"}))


def test_stop_removes_the_container_its_mail_and_the_setup(home: Path) -> None:
    _connect(home, URL)
    docker, undone = FakeDocker(), []
    lines = trial.stop(docker, lambda: undone.append(1) or ["Uninstalled."], 8803)
    assert ["rm", "-f", trial.CONTAINER] in docker.calls
    assert ["volume", "rm", "-f", trial.VOLUME] in docker.calls
    assert undone == [1] and "Uninstalled." in lines


def test_stop_leaves_a_machine_connected_elsewhere_alone(home: Path) -> None:
    _connect(home, "https://k.example.com")
    docker, undone = FakeDocker(), []
    lines = trial.stop(docker, lambda: undone.append(1) or [], 8803)
    assert undone == []
    assert ["rm", "-f", trial.CONTAINER] in docker.calls
    assert any("left alone" in line for line in lines)


def test_stop_with_docker_down_changes_nothing(home: Path) -> None:
    _connect(home, URL)
    docker, undone = FakeDocker(), []
    docker.daemon_up = False
    with pytest.raises(trial.TryError, match="Nothing was changed"):
        trial.stop(docker, lambda: undone.append(1) or [], 8803)
    assert undone == []
    assert not any(c[0] in ("rm", "volume") for c in docker.calls)


def test_a_failed_removal_is_reported_not_claimed(home: Path) -> None:
    _connect(home, URL)
    docker = FakeDocker()
    docker.remove_fails = True
    with pytest.raises(trial.TryError, match="Could not remove") as raised:
        trial.stop(docker, lambda: ["Uninstalled."], 8803)
    assert "Uninstalled." in str(raised.value)
    assert "Removed the" not in str(raised.value)


def test_stop_finds_a_trial_on_another_port(home: Path) -> None:
    _connect(home, "http://127.0.0.1:9900")
    docker, undone = FakeDocker(), []
    docker.published = 9900
    trial.stop(docker, lambda: undone.append(1) or [], 8803)  # --port left off
    assert undone == [1]


def test_next_steps_name_a_custom_port_in_the_stop_command() -> None:
    assert trial.next_steps("box", URL, ["codex"])[-1].endswith("try --stop")
    custom = trial.next_steps("box", "http://127.0.0.1:9900", ["codex"])
    assert custom[-1].endswith("try --stop --port 9900")


@pytest.mark.parametrize(
    ("agents", "where"),
    [
        (["claude-code"], "Claude Code in two repo folders"),
        (["codex"], "Codex in two repo folders"),
        (["claude-code", "codex"], "Claude Code in one repo folder and Codex"),
    ],
)
def test_next_steps_fit_the_agents_installed(agents: list[str], where: str) -> None:
    lines = trial.next_steps("box", URL, agents)
    assert any(where in line for line in lines)
    assert any("/hooks" in line for line in lines) == ("codex" in agents)


@pytest.mark.parametrize(
    ("hostname", "name"),
    [
        ("Kevins-MacBook.local", "kevins-macbook"),
        ("DESKTOP-1A2B3C", "desktop-1a2b3c"),
        ("my_box", "my_box"),
        ("-weird name!", "weird-name"),
        ("", "laptop"),
        ("x" * 80, "x" * 64),
    ],
)
def test_machine_name_from_hostname(hostname: str, name: str) -> None:
    assert trial.machine_name(hostname) == name


def test_cli_reports_a_try_error_and_exits_1(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(**kwargs: object) -> list[str]:
        raise trial.TryError("Docker is not running.")

    monkeypatch.setattr(trial, "start_default", refuse)
    assert cli.main(["try"]) == 1
    assert "Docker is not running." in capsys.readouterr().err


def test_cli_stop(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(trial, "stop_default", lambda port: ["Removed."])
    assert cli.main(["try", "--stop"]) == 0
    assert "Removed." in capsys.readouterr().out
