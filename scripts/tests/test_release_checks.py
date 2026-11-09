from pathlib import Path

import pytest

import release
import release_checks as rc

A = "a" * 40
B = "b" * 40


def test_recorded_checks_are_no_longer_missing(tmp_path: Path) -> None:
    for check in rc.CHECKS:
        rc.record(tmp_path, check, A, "2026-11-09")
    assert rc.missing(tmp_path, A) == []


def test_a_record_counts_only_for_its_own_commit(tmp_path: Path) -> None:
    for check in rc.CHECKS:
        rc.record(tmp_path, check, A, "2026-11-09")
    assert rc.missing(tmp_path, B) == list(rc.CHECKS)


def test_no_record_file_means_every_check_is_missing(tmp_path: Path) -> None:
    assert rc.missing(tmp_path, A) == list(rc.CHECKS)


def test_one_check_recorded_leaves_the_other_missing(tmp_path: Path) -> None:
    rc.record(tmp_path, "docs-review", A, "2026-11-09")
    assert rc.missing(tmp_path, A) == ["upgrade-rehearsal"]


def test_an_unknown_check_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        rc.record(tmp_path, "vibes", A, "2026-11-09")


def test_malformed_lines_are_ignored(tmp_path: Path) -> None:
    (tmp_path / rc.RECORD).write_text(f"garbage\n\ndocs-review\n{A}\n")
    rc.record(tmp_path, "upgrade-rehearsal", A, "2026-11-09")
    assert rc.missing(tmp_path, A) == ["docs-review"]


def test_status_exits_1_and_names_what_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc.record(tmp_path, "docs-review", A, "2026-11-09")
    assert rc.status(tmp_path, A) == 1
    out = capsys.readouterr().out
    assert "docs-review: ok" in out and "upgrade-rehearsal: missing" in out
    rc.record(tmp_path, "upgrade-rehearsal", A, "2026-11-09")
    assert rc.status(tmp_path, A) == 0


def test_the_gate_warns_on_a_dry_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert release.require_checks(tmp_path, A, dry_run=True) == list(rc.CHECKS)
    assert "aaaaaaa" in capsys.readouterr().out


def test_the_gate_stops_a_real_run_and_names_each_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc.record(tmp_path, "upgrade-rehearsal", A, "2026-11-09")
    with pytest.raises(SystemExit) as stop:
        release.require_checks(tmp_path, A, dry_run=False)
    assert stop.value.code == 1
    out = capsys.readouterr().out
    assert "docs-review" in out and "aaaaaaa" in out
    assert "upgrade-rehearsal" not in out.split("Missing", 1)[-1]


def test_the_gate_passes_when_both_are_recorded(tmp_path: Path) -> None:
    for check in rc.CHECKS:
        rc.record(tmp_path, check, A, "2026-11-09")
    assert release.require_checks(tmp_path, A, dry_run=False) == []
