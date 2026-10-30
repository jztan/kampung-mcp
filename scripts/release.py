#!/usr/bin/env python3
"""
Release automation for Kampung using gitflow.

Usage:
    uv run python scripts/release.py [patch|minor|major] [--dry-run]

Examples:
    uv run python scripts/release.py minor           # 0.1.0 -> 0.2.0
    uv run python scripts/release.py patch --dry-run # preview, save a notes draft
    uv run python scripts/release.py --version 0.1.0 # an exact version

A release ships three things from one version tag:
    - the client `kampung-mcp` to PyPI, and its MCP Registry entry;
    - the server image to ghcr.io/jztan/kampung-mcp (amd64 and arm64);
    - a GitHub release with the approved notes.

Gitflow:
    1. Start from develop
    2. Pre-flight: clean tree, formatting, lint, types, tests, dependency audit
    3. Create release/vX.Y.Z and bump every version (both packages, both
       __version__ strings, server.json, uv.lock), stamp the CHANGELOG and
       regenerate the client README that PyPI shows
    4. Draft and approve release notes with `claude -p` (persisted for
       recovery). `--dry-run` saves the draft to release_notes_vX.Y.Z.md for
       editing in any app; the real run publishes that file as approved
    5. Merge to master, push the tag (starts publish-pypi.yml)
    6. Wait for publish-pypi.yml, then for the version on PyPI
    7. Create the GitHub release (only once `uv tool install` works)
    8. Publish server.json to the MCP Registry
    9. Merge back to develop through the ruleset (the merge commit passes the
       commit-policy check on the release branch first), delete the branch
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path

PACKAGE_NAME = "kampung-mcp"
GITHUB_REPO = "jztan/kampung-mcp"
IMAGE = "ghcr.io/jztan/kampung-mcp"
REGISTRY_NAME = "io.github.jztan/kampung-mcp"
REGISTRY_URL = (
    f"https://registry.modelcontextprotocol.io/v0/servers?search={REGISTRY_NAME}"
)
# The remote that points at GITHUB_REPO, whatever it is named; set in main().
REMOTE = "origin"

PYPROJECTS = ("packages/server/pyproject.toml", "packages/client/pyproject.toml")
VERSION_MODULES = (
    "packages/server/src/kampung_server/__init__.py",
    "packages/client/src/kampung_client/__init__.py",
)
SERVER_JSON = "server.json"
VERSIONED_FILES = (*PYPROJECTS, *VERSION_MODULES, SERVER_JSON, "CHANGELOG.md")
# The page PyPI shows. A package cannot read a file outside its own folder (a
# symlink breaks the sdist), so each release regenerates it from the root README.
CLIENT_README = "packages/client/README.md"
BLOB_URL = f"https://github.com/{GITHUB_REPO}/blob/master/"
RAW_URL = f"https://raw.githubusercontent.com/{GITHUB_REPO}/master/"
RELATIVE_PATH = r"(?:\./)?((?:docs/|[A-Z_]+\.md)[^)\"]*)"


@dataclass
class ReleaseConfig:
    bump_type: str
    dry_run: bool
    project_root: Path
    hotfix: bool = False


def run_command(
    cmd: list[str],
    *,
    check: bool = True,
    capture_output: bool = True,
    dry_run: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command; in dry-run mode only print it."""
    if dry_run:
        print(f"  [DRY-RUN] Would run: {shlex.join(cmd)}")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    result = subprocess.run(
        cmd, capture_output=capture_output, text=True, check=False, env=env
    )
    if check and result.returncode != 0:
        print(f"Error running command: {shlex.join(cmd)}")
        print(f"  stdout: {result.stdout}")
        print(f"  stderr: {result.stderr}")
        sys.exit(1)
    return result


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


def read_versions(project_root: Path) -> dict[str, str]:
    """Every version string the release bumps, by file."""
    found: dict[str, str] = {}
    for rel in PYPROJECTS:
        text = (project_root / rel).read_text(encoding="utf-8")
        m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
        if m:
            found[rel] = m.group(1)
    for rel in VERSION_MODULES:
        text = (project_root / rel).read_text(encoding="utf-8")
        m = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
        if m:
            found[rel] = m.group(1)
    data = json.loads((project_root / SERVER_JSON).read_text(encoding="utf-8"))
    found[SERVER_JSON] = data["version"]
    for i, package in enumerate(data.get("packages", [])):
        found[f"{SERVER_JSON} packages[{i}]"] = package["version"]
    return found


def get_current_version(project_root: Path) -> str:
    """The one current version, refusing to go on if the files disagree."""
    versions = read_versions(project_root)
    expected = len(PYPROJECTS) + len(VERSION_MODULES) + 1
    if len(versions) < expected:
        missing = sorted(set(VERSIONED_FILES[:-1]) - set(versions))
        raise ValueError(f"no version found in: {', '.join(missing)}")
    distinct = set(versions.values())
    if len(distinct) != 1:
        detail = ", ".join(f"{k}={v}" for k, v in sorted(versions.items()))
        raise ValueError(f"versions disagree: {detail}")
    return distinct.pop()


def find_remote(listing: str) -> str:
    """The remote in `git remote -v` output whose URL is this repository."""
    url = re.compile(rf"github\.com[:/]{re.escape(GITHUB_REPO)}(\.git)?/?$")
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) >= 2 and url.search(parts[1]):
            return parts[0]
    raise ValueError(f"no git remote points at {GITHUB_REPO}")


def calculate_new_version(current: str, bump_type: str) -> str:
    parts = current.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"invalid version: {current}")
    major, minor, patch = map(int, parts)
    if bump_type == "major":
        return f"{major + 1}.0.0"
    if bump_type == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def explicit_version(current: str, wanted: str, tags: set[str]) -> str:
    """Check a version named with --version.

    It may equal the current version (a first release ships it as is) but
    never go backwards, and it must not have a tag already."""
    parts = wanted.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"--version must look like 1.2.3, not {wanted!r}")
    if tuple(map(int, parts)) < tuple(map(int, current.split("."))):
        raise ValueError(f"--version {wanted} is lower than the current {current}")
    if f"v{wanted}" in tags:
        raise ValueError(f"v{wanted} is already tagged")
    return wanted


def bump_versions(project_root: Path, new_version: str, dry_run: bool) -> None:
    """Write new_version into both packages, both modules and server.json."""
    for rel in PYPROJECTS:
        _sub_file(project_root / rel, r'^(version\s*=\s*)"[^"]+"', new_version, dry_run)
    for rel in VERSION_MODULES:
        _sub_file(
            project_root / rel, r'^(__version__\s*=\s*)"[^"]+"', new_version, dry_run
        )
    path = project_root / SERVER_JSON
    data = json.loads(path.read_text(encoding="utf-8"))
    data["version"] = new_version
    for package in data.get("packages", []):
        package["version"] = new_version
    if dry_run:
        print(f"  [DRY-RUN] Would set {SERVER_JSON} to {new_version}")
    else:
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"  ✓ Updated {SERVER_JSON}")


def _sub_file(path: Path, pattern: str, new_version: str, dry_run: bool) -> None:
    text = path.read_text(encoding="utf-8")
    new_text, count = re.subn(
        pattern, f'\\1"{new_version}"', text, count=1, flags=re.MULTILINE
    )
    if count != 1:
        raise ValueError(f"no version line in {path}")
    if dry_run:
        print(f"  [DRY-RUN] Would set {path.name} to {new_version}")
    else:
        path.write_text(new_text, encoding="utf-8")
        print(f"  ✓ Updated {path}")


def render_client_readme(readme: str) -> str:
    """Return the root README with relative links made absolute for PyPI."""
    readme = re.sub(rf'src="{RELATIVE_PATH}"', rf'src="{RAW_URL}\1"', readme)
    return re.sub(rf"\]\({RELATIVE_PATH}\)", rf"]({BLOB_URL}\1)", readme)


def sync_client_readme(project_root: Path, dry_run: bool) -> None:
    """Regenerate the README that ships to PyPI from the root README."""
    if dry_run:
        print(f"  [DRY-RUN] Would regenerate {CLIENT_README}")
        return
    readme = (project_root / "README.md").read_text(encoding="utf-8")
    (project_root / CLIENT_README).write_text(
        render_client_readme(readme), encoding="utf-8"
    )
    print(f"  ✓ Updated {CLIENT_README}")


def update_changelog(project_root: Path, new_version: str, dry_run: bool) -> None:
    """Turn [Unreleased] into [new_version] - today, above a fresh [Unreleased].

    An empty [Unreleased] stops the release: stamping a placeholder would
    publish empty notes.
    """
    path = project_root / "CHANGELOG.md"
    content = path.read_text(encoding="utf-8")
    new_content = stamp_changelog(content, new_version, date.today().isoformat())
    if dry_run:
        print(f"  [DRY-RUN] Would stamp CHANGELOG.md with {new_version}")
    else:
        path.write_text(new_content, encoding="utf-8")
        print("  ✓ Updated CHANGELOG.md")


def stamp_changelog(content: str, new_version: str, today: str) -> str:
    body = unreleased_body(content)
    if body is None:
        raise ValueError("CHANGELOG.md has no [Unreleased] section")
    headings_only = all(
        not line.strip() or line.lstrip().startswith("#") for line in body.split("\n")
    )
    if headings_only:
        raise ValueError("the [Unreleased] section of CHANGELOG.md is empty")
    stamped = re.sub(
        r"^## \[Unreleased\]\s*\n",
        f"## [Unreleased]\n\n## [{new_version}] - {today}\n\n",
        content,
        count=1,
        flags=re.MULTILINE | re.IGNORECASE,
    )
    link = (
        f"[{new_version}]: https://github.com/{GITHUB_REPO}/releases/tag/v{new_version}"
    )
    if link not in stamped:
        first = re.search(r"^\[[\d.]+\]: https://", stamped, re.MULTILINE)
        if first:
            stamped = stamped[: first.start()] + link + "\n" + stamped[first.start() :]
        else:
            stamped = stamped.rstrip() + "\n\n" + link + "\n"
    return stamped


def unreleased_body(content: str) -> str | None:
    m = re.search(
        r"^## \[Unreleased\]\s*\n(.*?)(?=^## \[|\Z)",
        content,
        re.IGNORECASE | re.DOTALL | re.MULTILINE,
    )
    return m.group(1).strip() if m else None


def version_body(content: str, version: str) -> str | None:
    m = re.search(
        rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|^\[[\d.]+\]: |\Z)",
        content,
        re.DOTALL | re.MULTILINE,
    )
    return m.group(1).strip() if m else None


# ---------------------------------------------------------------------------
# Contributors: copied into the notes verbatim, never rewritten by the LLM
# ---------------------------------------------------------------------------


def split_contributors(section: str) -> tuple[str, str]:
    """(body without ### Contributors, a 'Thanks to' block for the notes)."""
    m = re.search(r"^### Contributors\s*\n(.*?)(?=^#{2,3} |\Z)", section, re.M | re.S)
    if not m:
        return section.strip(), ""
    body = (section[: m.start()] + section[m.end() :]).strip()
    authors: dict[str, list[str]] = {}
    for entry in _contributor_entries(m.group(1)):
        parsed = _parse_contributor_entry(entry)
        if parsed:
            authors.setdefault(parsed[0], []).append(parsed[1])
    if not authors:
        # Dropping the section and returning no credits would publish the
        # release with its contributors silently deleted.
        raise ValueError(
            "CHANGELOG has a '### Contributors' section but no entry could be "
            "parsed; entries must be list items starting with '- @handle'."
        )
    lines: list[str] = []
    for author, items in authors.items():
        lines.append(f"Thanks to **{author}** for contributing:")
        lines.extend(f"- {item}" for item in items)
        lines.append("")
    return body, "\n".join(lines).strip()


def _contributor_entries(text: str) -> list[str]:
    """One string per list item, joining wrapped continuation lines."""
    entries: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- "):
            entries.append(stripped[2:].strip())
        elif entries:
            entries[-1] = f"{entries[-1]} {stripped}"
    return entries


def _parse_contributor_entry(entry: str) -> tuple[str, str] | None:
    handle = re.match(r"(@\S+?)[\s:,-]+(.+)", entry, re.DOTALL)
    if handle:
        return handle.group(1), handle.group(2).strip()
    named = re.match(r"([A-Za-z][\w.\-]*)\s*[:,]\s*(.+)", entry, re.DOTALL)
    if named:
        return named.group(1), named.group(2).strip()
    return None


# ---------------------------------------------------------------------------
# Release notes: drafted by `claude -p`, approved by a human, persisted
# ---------------------------------------------------------------------------


def notes_file_path(project_root: Path, new_version: str) -> Path:
    return project_root / f"release_notes_v{new_version}.md"


def install_section(new_version: str) -> str:
    return f"""## Install

Client, on each machine:

```bash
uv tool install {PACKAGE_NAME}=={new_version}
kampung-mcp setup --server https://kampung.example.com
```

Server image (amd64 and arm64): set `KAMPUNG_IMAGE_TAG={new_version}` in `.env`, then

```bash
docker compose pull && docker compose --profile tls up -d --wait
```

## Links
- [PyPI package](https://pypi.org/project/{PACKAGE_NAME}/{new_version}/)
- [Server image](https://github.com/{GITHUB_REPO}/pkgs/container/kampung-mcp)
- [MCP Registry]({REGISTRY_URL})
- [Full changelog](https://github.com/{GITHUB_REPO}/blob/master/CHANGELOG.md)
"""


def build_release_body(generated: str, acknowledgements: str, new_version: str) -> str:
    ack = f"\n\n## Contributors\n\n{acknowledgements}" if acknowledgements else ""
    return f"{generated.strip()}{ack}\n\n{install_section(new_version)}"


def build_raw_release_body(body: str, acknowledgements: str, new_version: str) -> str:
    return build_release_body(
        f"## What's new in v{new_version}\n\n{body}", acknowledgements, new_version
    )


NOTES_GENERATION_TIMEOUT = 120
NOTES_DENIED_TOOLS = "Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch,Task,TodoWrite"
# Keep `claude -p` from loading CLAUDE.md files and MCP servers it does not need.
NOTES_CONTEXT_FLAGS = [
    "--setting-sources",
    "",
    "--strict-mcp-config",
    "--mcp-config",
    '{"mcpServers":{}}',
]

# A .format() template: it must contain no literal braces.
RELEASE_NOTES_PROMPT = """\
You are writing the GitHub release notes for Kampung {tag}. Kampung is a
self-hosted server plus a client that lets coding-agent sessions (Claude Code,
Codex) on different machines send each other short messages over MCP.

Rewrite the changelog section below into release notes for people deciding
whether to upgrade. Respond with the notes only; do not use any tools.

Output contract (follow exactly):
- First line: `TITLE: {tag}: <short headline, at most 8 words>`
- Then a `## Highlights` section: 2-4 short paragraphs, at most 150 words in
  total. Lead with what is new and why a user would care.
- Then a `## Changes` section: one-line bullets grouped under `### Server`,
  `### Client` and `### Deployment` (omit empty groups). No source file paths,
  function names or other internals.

Hard rules:
- Use only facts present in the changelog section. Never invent features,
  benefits or numbers, and keep every number exactly as written.
- Never use em dashes. Use a comma, colon or parentheses instead.
- Plain markdown. No H1 headings. Do not add Install, Links or Contributors
  sections; they are appended separately.

Changelog section for {tag}:

{changelog_section}
"""


def parse_title(output: str, tag: str) -> tuple[str, str]:
    lines = output.splitlines()
    first = lines[0].strip() if lines else ""
    if first.upper().startswith("TITLE:"):
        title = first[len("TITLE:") :].strip()
        if title:
            return title, "\n".join(lines[1:]).strip()
    return tag, output


def generate_release_notes(
    new_version: str, changelog_section: str, steering: str | None = None
) -> tuple[str, str]:
    """Draft notes with `claude -p`; RuntimeError on any failure."""
    tag = f"v{new_version}"
    prompt = RELEASE_NOTES_PROMPT.format(tag=tag, changelog_section=changelog_section)
    if steering:
        prompt += f"\nAdditional guidance from the maintainer: {steering}\n"
    try:
        result = subprocess.run(
            ["claude", "-p", "--disallowedTools", NOTES_DENIED_TOOLS]
            + NOTES_CONTEXT_FLAGS,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=NOTES_GENERATION_TIMEOUT,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("'claude' CLI not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"claude -p timed out after {exc.timeout}s") from exc
    if result.returncode != 0:
        raise RuntimeError(f"claude -p exited {result.returncode}: {result.stderr}")
    output = result.stdout.strip()
    if not output:
        raise RuntimeError("claude -p returned empty output")
    return parse_title(output, tag)


def write_notes_file(path: Path, title: str, body: str) -> None:
    path.write_text(f"<!-- title: {title} -->\n{body.strip()}\n", encoding="utf-8")


def read_notes_file(path: Path) -> tuple[str | None, str]:
    content = path.read_text(encoding="utf-8")
    m = re.match(r"<!-- title: (.*?) -->\n", content)
    if m:
        return m.group(1), content[m.end() :].strip()
    return None, content.strip()


class NotesFileError(ValueError):
    """A pre-written notes file cannot be published for this version."""


def load_approved_notes(
    path: Path, new_version: str, acknowledgements: str
) -> tuple[str, str]:
    """Check a hand-approved notes file before anything is branched or tagged."""
    tag = f"v{new_version}"
    if not path.is_file():
        raise NotesFileError(f"notes file not found: {path}")
    title, body = read_notes_file(path)
    if not body:
        raise NotesFileError(f"notes file {path} has an empty body")
    if title is None or not (title == tag or title.startswith(f"{tag}:")):
        raise NotesFileError(
            f"notes file {path} is titled {title!r}; this release is {tag}. "
            f"Its first line must be `<!-- title: {tag}: <headline> -->`."
        )
    missing = [
        name
        for name in re.findall(r"^Thanks to \*\*(.+?)\*\*", acknowledgements, re.M)
        if name not in body
    ]
    if missing:
        raise NotesFileError(f"notes file {path} drops credits: {', '.join(missing)}")
    return title, body


def approve_release_notes(
    new_version: str, body: str, acknowledgements: str, notes_path: Path
) -> None:
    """[y]es / [e]dit in $EDITOR / [r]egenerate / [f]allback.

    Never blocks the release on the LLM: a failed draft falls back to the
    changelog."""
    tag = f"v{new_version}"
    interactive = sys.stdin.isatty()
    steering: str | None = None
    while True:
        print("  Drafting release notes (claude -p)...")
        try:
            title, generated = generate_release_notes(new_version, body, steering)
        except RuntimeError as exc:
            print(f"  ⚠ Notes generation failed: {exc}")
            if (
                interactive
                and _ask("  [r]etry / [f]all back to the changelog? ", "rf") == "r"
            ):
                continue
            write_notes_file(
                notes_path,
                tag,
                build_raw_release_body(body, acknowledgements, new_version),
            )
            return
        notes = build_release_body(generated, acknowledgements, new_version)
        print("\n" + "-" * 60 + f"\n  Title: {title}\n" + "-" * 60)
        print(notes)
        print("-" * 60)
        if not interactive:
            print("  ⚠ Not a terminal: accepting the draft.")
            write_notes_file(notes_path, title, notes)
            return
        choice = _ask("  [y]es publish / [e]dit / [r]egenerate / [f]allback? ", "yerf")
        if choice == "y":
            write_notes_file(notes_path, title, notes)
            return
        if choice == "e":
            write_notes_file(notes_path, title, notes)
            editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
            subprocess.run([*shlex.split(editor), str(notes_path)], check=False)
            print(f"  ✓ Using the edited notes in {notes_path}")
            return
        if choice == "f":
            write_notes_file(
                notes_path,
                tag,
                build_raw_release_body(body, acknowledgements, new_version),
            )
            return
        steering = input("  Any guidance? (enter to skip) ").strip() or None


def _ask(prompt: str, choices: str) -> str:
    while True:
        answer = input(prompt).strip().lower()
        if len(answer) == 1 and answer in choices:
            return answer
        print(f"  Please answer one of: {', '.join(choices)}.")


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def preflight_checks(config: ReleaseConfig) -> None:
    print("\n=== Pre-flight Checks ===\n")
    if run_command(["git", "status", "--porcelain"]).stdout.strip():
        print("Error: the working tree is not clean; commit or stash first.")
        sys.exit(1)
    print("  ✓ Working tree is clean")

    branch = run_command(["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    base = "master" if config.hotfix else "develop"
    if config.hotfix and not branch.startswith("hotfix/"):
        print(f"Error: --hotfix needs a hotfix/* branch, not '{branch}'")
        sys.exit(1)
    if not config.hotfix and branch != "develop":
        print(f"Error: start a release from develop, not '{branch}'")
        sys.exit(1)
    run_command(["git", "pull", "--ff-only", REMOTE, base])
    print(f"  ✓ On {branch}, up to date with {REMOTE}/{base}")

    checks = [
        (["uv", "run", "black", "--check", "."], "formatting"),
        (["uv", "run", "flake8", "packages", "scripts"], "lint"),
        (
            ["uv", "run", "mypy", "packages/server/src", "packages/client/src"],
            "types",
        ),
        (["uv", "run", "pytest", "-q"], "tests"),
    ]
    for cmd, label in checks:
        print(f"  Checking {label}...")
        result = run_command(cmd, check=False)
        if result.returncode != 0:
            print(
                f"Error: {label} failed:\n"
                f"{result.stdout[-3000:]}{result.stderr[-2000:]}"
            )
            sys.exit(1)
        print(f"  ✓ {label.capitalize()} OK")

    print("  Auditing dependencies...")
    audit = run_command(["bash", "scripts/audit.sh"], check=False)
    if audit.returncode != 0:
        print(f"Error: dependency audit failed:\n{audit.stdout}{audit.stderr}")
        print("Fix it, or add --ignore-vuln to scripts/audit.sh with a reason.")
        sys.exit(1)
    print("  ✓ No known vulnerabilities")

    if run_command(["gh", "auth", "status"], check=False).returncode != 0:
        print("Error: gh is not signed in; run `gh auth login`.")
        sys.exit(1)
    print("  ✓ gh signed in")
    if run_command(["which", "mcp-publisher"], check=False).returncode != 0:
        print("  ⚠ mcp-publisher not found: the MCP Registry step will be skipped")
        print("    (brew install mcp-publisher)")
    else:
        print("  ✓ mcp-publisher available (it signs in at publish time)")


def commit_version_bump(config: ReleaseConfig, new_version: str) -> None:
    print("\n=== Commit Version Bump ===\n")
    run_command(
        ["git", "add", *VERSIONED_FILES, CLIENT_README, "uv.lock"],
        dry_run=config.dry_run,
    )
    run_command(
        ["git", "commit", "-m", f"chore: bump version to v{new_version}"],
        dry_run=config.dry_run,
    )
    if not config.dry_run:
        print(f"  ✓ Committed v{new_version}")


def merge_to_master_and_tag(
    config: ReleaseConfig, new_version: str, branch: str
) -> None:
    print("\n=== Merge to Master & Tag ===\n")
    tag = f"v{new_version}"
    dry = config.dry_run
    run_command(["git", "checkout", "master"], dry_run=dry)
    run_command(["git", "pull", "--ff-only", REMOTE, "master"], dry_run=dry)
    run_command(
        ["git", "merge", "--no-ff", branch, "-m", f"Merge branch '{branch}'"],
        dry_run=dry,
    )
    run_command(["git", "tag", "-a", tag, "-m", f"Release {tag}"], dry_run=dry)
    run_command(["git", "push", REMOTE, "master"], dry_run=dry)
    # The tag push is what starts publish-pypi.yml.
    run_command(["git", "push", REMOTE, tag], dry_run=dry)
    if not dry:
        print(f"  ✓ master merged and {tag} pushed")


def wait_for_run(
    workflow: str, sha: str, label: str, max_wait: int, branch: str | None = None
) -> bool:
    """Poll a workflow run on `sha` until it finishes; True only on success."""
    start = time.time()
    last: str | None = None
    cmd = [
        "gh",
        "run",
        "list",
        f"--workflow={workflow}",
        "--limit",
        "20",
        "--json",
        "databaseId,status,conclusion,headSha,url",
    ]
    if branch:
        cmd += ["--branch", branch]
    while time.time() - start < max_wait:
        elapsed = int(time.time() - start)
        result = run_command(cmd, check=False)
        try:
            runs = json.loads(result.stdout) if result.returncode == 0 else []
        except json.JSONDecodeError:
            runs = []
        run = next((r for r in runs if r.get("headSha") == sha), None)
        if run is None:
            print(f"  {label}: not started yet ({elapsed}s)")
        elif run["status"] == "completed":
            if run["conclusion"] == "success":
                print(f"  ✓ {label} succeeded ({run['url']})")
                return True
            print(f"  ✗ {label} finished: {run['conclusion']} ({run['url']})")
            print(f"    Logs: gh run view {run['databaseId']} --log-failed")
            return False
        elif run["status"] != last:
            print(f"  {label}: {run['status']} ({elapsed}s)")
            last = run["status"]
        time.sleep(15)
    print(f"  ⚠ Gave up on {label} after {max_wait}s")
    return False


def wait_for_pypi(new_version: str, max_wait: int = 600) -> bool:
    """The JSON API is what the MCP Registry checks, so wait on it."""
    print("\n=== Waiting for PyPI ===\n")
    url = f"https://pypi.org/pypi/{PACKAGE_NAME}/{new_version}/json"
    start = time.time()
    while time.time() - start < max_wait:
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                if resp.status == 200:
                    print(f"  ✓ {PACKAGE_NAME} {new_version} is on PyPI")
                    return True
        except urllib.error.HTTPError as e:
            if e.code != 404:
                print(f"  Unexpected status {e.code}, retrying...")
        except urllib.error.URLError as e:
            print(f"  Network error: {e.reason}, retrying...")
        print(f"  Waiting for PyPI ({int(time.time() - start)}s)")
        time.sleep(15)
    print("  ⚠ Timed out waiting for PyPI")
    return False


def create_github_release(config: ReleaseConfig, new_version: str) -> None:
    print("\n=== GitHub Release ===\n")
    tag = f"v{new_version}"
    notes_path = notes_file_path(config.project_root, new_version)
    if config.dry_run:
        print(f"  [DRY-RUN] Would create the GitHub release {tag} from {notes_path}")
        return
    stored_title, notes = read_notes_file(notes_path)
    result = run_command(
        [
            "gh",
            "release",
            "create",
            tag,
            "--title",
            stored_title or tag,
            "--notes-file",
            str(notes_path),
        ],
        check=False,
    )
    if result.returncode != 0:
        print(f"  ✗ gh release create failed: {result.stderr.strip()}")
        print_recovery(new_version, config)
        sys.exit(1)
    print(f"  ✓ Created the GitHub release {stored_title or tag}")


def publish_mcp_registry(config: ReleaseConfig) -> None:
    """Sign in right before publishing: the registry token is short-lived."""
    print("\n=== MCP Registry ===\n")
    if run_command(["which", "mcp-publisher"], check=False).returncode != 0:
        print("  ⚠ mcp-publisher not found; skipped (brew install mcp-publisher)")
        return
    if config.dry_run:
        print(
            "  [DRY-RUN] Would run: mcp-publisher login github && mcp-publisher publish"
        )
        return
    try:
        login = subprocess.run(["mcp-publisher", "login", "github"], timeout=300)
    except subprocess.TimeoutExpired:
        login = subprocess.CompletedProcess([], 1)
    if login.returncode != 0:
        print(
            "  ⚠ mcp-publisher login failed; run it and `mcp-publisher publish` later"
        )
        return
    result = run_command(["mcp-publisher", "publish"], check=False)
    if result.returncode == 0:
        print(f"  ✓ Published {REGISTRY_NAME}")
    else:
        print(f"  ⚠ MCP Registry publish failed: {result.stderr.strip()}")


def merge_back_to_develop(config: ReleaseConfig, branch: str) -> None:
    """The develop ruleset requires the commit-policy check on the exact commit,
    so the merge commit goes to the release branch first, passes there, and
    only then is pushed to develop."""
    print("\n=== Merge Back to Develop ===\n")
    dry = config.dry_run
    run_command(["git", "checkout", "develop"], dry_run=dry)
    run_command(["git", "pull", "--ff-only", REMOTE, "develop"], dry_run=dry)
    merge = run_command(
        [
            "git",
            "merge",
            "--no-ff",
            branch,
            "-m",
            f"Merge branch '{branch}' into develop",
        ],
        check=False,
        dry_run=dry,
    )
    if merge.returncode != 0:
        print(f"  ✗ Merge conflict merging {branch} into develop. Resolve it, then:")
        print(f"    git commit && git push {REMOTE} HEAD:{branch}")
        print(f"    (wait for Commit policy) && git push {REMOTE} develop")
        sys.exit(1)
    if dry:
        print("  [DRY-RUN] Would push the merge to the release branch, wait for")
        print("  [DRY-RUN] Commit policy, push develop and delete the branch")
        return
    sha = run_command(["git", "rev-parse", "HEAD"]).stdout.strip()
    run_command(["git", "push", REMOTE, f"{sha}:refs/heads/{branch}"])
    if not wait_for_run("commit-policy.yml", sha, "Commit policy", 600, branch):
        print(f"  ✗ develop not pushed. Fix it, then: git push {REMOTE} develop")
        sys.exit(1)
    run_command(["git", "push", REMOTE, "develop"])
    run_command(["git", "branch", "-d", branch])
    run_command(["git", "push", REMOTE, "--delete", branch], check=False)
    print(f"  ✓ develop pushed; {branch} deleted")


def print_recovery(new_version: str, config: ReleaseConfig) -> None:
    tag = f"v{new_version}"
    notes = notes_file_path(config.project_root, new_version)
    kind = "hotfix" if config.hotfix else "release"
    print("\n" + "=" * 60)
    print(f"  RELEASE {tag} INCOMPLETE")
    print("=" * 60)
    print(f"  The tag {tag} and master are pushed. The GitHub release, the MCP")
    print("  Registry entry and the merge back to develop are not done; the")
    print(f"  {kind} branch still exists locally.")
    print()
    print("  Investigate:  gh run list --workflow=publish-pypi.yml --limit 5")
    print("                gh run view <run-id> --log-failed")
    print("  Then either rerun the same tag's run (gh run rerun <run-id>; the")
    print("  PyPI upload skips files already there) and finish by hand:")
    print(f"    gh release create {tag} --notes-file {notes}")
    print("    mcp-publisher login github && mcp-publisher publish")
    print("    and merge the branch into develop through the ruleset,")
    print("  or ship the fix as the next patch.")
    print("=" * 60)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Release automation for Kampung (gitflow)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Release notes, edited outside the terminal:
  uv run python scripts/release.py minor --dry-run  # saves release_notes_vX.Y.Z.md
  # edit that file in any app, then:
  uv run python scripts/release.py minor            # publishes the edited draft
""",
    )
    parser.add_argument("bump_type", choices=["patch", "minor", "major"], nargs="?")
    parser.add_argument(
        "--version",
        dest="version",
        help="release exactly this version, e.g. 0.1.0 for the first release",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--hotfix", action="store_true", help="finish the current hotfix/* branch"
    )
    parser.add_argument(
        "--notes-file", type=Path, help="publish this approved notes file"
    )
    args = parser.parse_args()
    if args.version and (args.bump_type or args.hotfix):
        parser.error("--version replaces the bump type; pass one or the other")
    if args.hotfix:
        bump_type = "patch"
    elif args.bump_type is None and not args.version:
        parser.error("pass patch, minor or major, or --version X.Y.Z")
    else:
        bump_type = args.bump_type or "explicit"

    config = ReleaseConfig(
        bump_type=bump_type,
        dry_run=args.dry_run,
        project_root=Path(__file__).resolve().parent.parent,
        hotfix=args.hotfix,
    )
    os.chdir(config.project_root)
    global REMOTE
    try:
        REMOTE = find_remote(run_command(["git", "remote", "-v"]).stdout)
    except ValueError as exc:
        print(f"Error: {exc}")
        sys.exit(1)
    print("=" * 60 + "\n  Kampung Release (gitflow)\n" + "=" * 60)
    if config.dry_run:
        print("\n  DRY-RUN: nothing is changed, but a notes draft is saved\n")

    preflight_checks(config)
    try:
        current = get_current_version(config.project_root)
        if args.version:
            tags = set(run_command(["git", "tag", "--list"]).stdout.split())
            new_version = explicit_version(current, args.version, tags)
        else:
            new_version = calculate_new_version(current, config.bump_type)
        changelog = (config.project_root / "CHANGELOG.md").read_text(encoding="utf-8")
        stamp_changelog(changelog, new_version, date.today().isoformat())
        body, acknowledgements = split_contributors(unreleased_body(changelog) or "")
    except ValueError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    notes_path = notes_file_path(config.project_root, new_version)
    notes_source = args.notes_file or (notes_path if notes_path.exists() else None)
    approved: tuple[str, str] | None = None
    if notes_source is not None:
        try:
            approved = load_approved_notes(notes_source, new_version, acknowledgements)
        except NotesFileError as exc:
            print(f"Error: {exc}")
            sys.exit(1)
        print(f"\nUsing the approved notes in {notes_source}")

    if config.hotfix:
        branch = run_command(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"]
        ).stdout.strip()
    else:
        branch = f"release/v{new_version}"
        run_command(["git", "checkout", "-b", branch], dry_run=config.dry_run)

    print(f"\n=== Version Bump: {current} -> {new_version} ===\n")
    bump_versions(config.project_root, new_version, config.dry_run)
    update_changelog(config.project_root, new_version, config.dry_run)
    sync_client_readme(config.project_root, config.dry_run)
    run_command(["uv", "lock"], dry_run=config.dry_run)
    commit_version_bump(config, new_version)

    print("\n=== Release Notes ===\n")
    if approved is not None:
        if not config.dry_run:
            write_notes_file(notes_path, *approved)
        print(f"  ✓ Title: {approved[0]}")
    elif config.dry_run:
        try:
            title, generated = generate_release_notes(new_version, body)
            final = build_release_body(generated, acknowledgements, new_version)
        except RuntimeError as exc:
            print(f"  ⚠ {exc}; the release would fall back to the changelog")
            title = f"v{new_version}"
            final = build_raw_release_body(body, acknowledgements, new_version)
        write_notes_file(notes_path, title, final)
        print(f"  Title: {title}\n\n{final}")
        print(f"\n  ✓ Draft saved to {notes_path}; edit it, then run without --dry-run")
    else:
        approve_release_notes(new_version, body, acknowledgements, notes_path)

    merge_to_master_and_tag(config, new_version, branch)

    if not config.dry_run:
        print("\n=== Publish Workflow ===\n")
        tag_sha = run_command(
            ["git", "rev-parse", f"v{new_version}^{{commit}}"]
        ).stdout.strip()
        if not wait_for_run("publish-pypi.yml", tag_sha, "publish-pypi", 3600):
            print_recovery(new_version, config)
            sys.exit(1)
        if not wait_for_pypi(new_version):
            print_recovery(new_version, config)
            sys.exit(1)
    else:
        print("\n  [DRY-RUN] Would wait for publish-pypi.yml and for PyPI")

    create_github_release(config, new_version)
    publish_mcp_registry(config)
    merge_back_to_develop(config, branch)

    if not config.dry_run:
        for stale in config.project_root.glob("release_notes_v*.md"):
            stale.unlink()
        print("\n" + "=" * 60 + f"\n  RELEASE v{new_version} COMPLETE\n")
        print(f"  PyPI:   https://pypi.org/project/{PACKAGE_NAME}/{new_version}/")
        print(f"  Image:  {IMAGE}:{new_version}")
        print(f"  GitHub: https://github.com/{GITHUB_REPO}/releases/tag/v{new_version}")
        print("=" * 60)
    else:
        print("\n" + "=" * 60 + "\n  DRY-RUN COMPLETE: no changes made\n" + "=" * 60)


if __name__ == "__main__":
    main()
