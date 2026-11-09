#!/usr/bin/env python3
"""Record the release checks that only a person or a long run can do.

`release.py` refuses to tag a commit unless every check below is recorded
for that exact commit. A record is one line in `.release-checks` at the
repo root (gitignored): `<check> <full sha> <date>`. Any new commit needs
the checks again, because the record names the commit, not the branch.

Usage:
    uv run python scripts/release_checks.py status
    uv run python scripts/release_checks.py record docs-review

`upgrade_rehearsal.py` records `upgrade-rehearsal` itself when it passes.
"""

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

CHECKS: tuple[str, ...] = ("docs-review", "upgrade-rehearsal")
RECORD = ".release-checks"
ROOT = Path(__file__).resolve().parent.parent


def record(root: Path, check: str, sha: str, today: str) -> None:
    if check not in CHECKS:
        raise ValueError(f"unknown check {check!r}; expected one of {CHECKS}")
    with (root / RECORD).open("a", encoding="utf-8") as f:
        f.write(f"{check} {sha} {today}\n")


def _recorded(root: Path, sha: str) -> set[str]:
    try:
        lines = (root / RECORD).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return set()
    done = set()
    for line in lines:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == sha:
            done.add(parts[0])
    return done


def missing(root: Path, sha: str) -> list[str]:
    done = _recorded(root, sha)
    return [c for c in CHECKS if c not in done]


def status(root: Path, sha: str) -> int:
    gaps = missing(root, sha)
    for check in CHECKS:
        print(f"{check}: {'missing' if check in gaps else 'ok'} for {sha[:7]}")
    return 1 if gaps else 0


def head_sha(root: Path) -> str:
    done = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show each check for HEAD; exit 1 if any is missing")
    rec = sub.add_parser("record", help="record the docs review as done for HEAD")
    # The rehearsal records itself when it passes; it is never recorded by hand.
    rec.add_argument("check", choices=["docs-review"])
    args = parser.parse_args(argv)
    sha = head_sha(ROOT)
    if args.command == "record":
        record(ROOT, args.check, sha, today())
        print(f"Recorded {args.check} for {sha[:7]}.")
        return 0
    return status(ROOT, sha)


if __name__ == "__main__":
    sys.exit(main())
