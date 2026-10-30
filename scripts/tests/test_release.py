import json
from pathlib import Path

import pytest

import release

ROOT = Path(__file__).resolve().parents[2]


def _project(tmp_path: Path, version: str = "0.1.0") -> Path:
    for rel in release.PYPROJECTS:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f'[project]\nname = "x"\nversion = "{version}"\n')
    for rel in release.VERSION_MODULES:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f'"""Doc."""\n\n__version__ = "{version}"\n')
    (tmp_path / release.SERVER_JSON).write_text(
        json.dumps({"version": version, "packages": [{"version": version}]})
    )
    return tmp_path


def test_the_real_repo_has_one_consistent_version() -> None:
    assert release.get_current_version(ROOT)


def test_bump_writes_every_version(tmp_path: Path) -> None:
    root = _project(tmp_path)
    release.bump_versions(root, "0.2.0", dry_run=False)
    assert set(release.read_versions(root).values()) == {"0.2.0"}
    assert len(release.read_versions(root)) == 6


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    root = _project(tmp_path)
    release.bump_versions(root, "0.2.0", dry_run=True)
    assert set(release.read_versions(root).values()) == {"0.1.0"}


def test_disagreeing_versions_stop_the_release(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / release.VERSION_MODULES[1]).write_text('__version__ = "0.0.9"\n')
    with pytest.raises(ValueError, match="disagree"):
        release.get_current_version(root)


@pytest.mark.parametrize(
    "bump,want", [("patch", "1.2.4"), ("minor", "1.3.0"), ("major", "2.0.0")]
)
def test_calculate_new_version(bump: str, want: str) -> None:
    assert release.calculate_new_version("1.2.3", bump) == want


CHANGELOG = """# Changelog

## [Unreleased]

### Added

- A thing.
"""


def test_stamp_changelog_moves_unreleased_under_the_version() -> None:
    out = release.stamp_changelog(CHANGELOG, "0.2.0", "2026-10-30")
    assert "## [Unreleased]\n\n## [0.2.0] - 2026-10-30\n\n### Added" in out
    assert release.version_body(out, "0.2.0") == "### Added\n\n- A thing."
    assert out.rstrip().endswith(
        "[0.2.0]: https://github.com/jztan/kampung-mcp/releases/tag/v0.2.0"
    )


def test_an_empty_unreleased_section_stops_the_release() -> None:
    with pytest.raises(ValueError, match="empty"):
        release.stamp_changelog("## [Unreleased]\n\n### Added\n", "0.2.0", "d")


def test_the_real_changelog_can_be_stamped() -> None:
    text = (ROOT / "CHANGELOG.md").read_text()
    release.stamp_changelog(text, "9.9.9", "2026-10-30")


def test_contributors_are_kept_verbatim_with_wrapped_lines() -> None:
    section = (
        "### Fixed\n\n- A bug.\n\n### Contributors\n\n"
        "- @alice reported the crash\n  ([#3](https://x/3))\n- @bob: tested it\n"
    )
    body, ack = release.split_contributors(section)
    assert "Contributors" not in body and "A bug." in body
    assert "Thanks to **@alice**" in ack and "([#3](https://x/3))" in ack
    assert "Thanks to **@bob**" in ack


def test_unparseable_contributors_stop_the_release() -> None:
    with pytest.raises(ValueError):
        release.split_contributors("### Contributors\n\nthanks everyone\n")


def test_notes_file_checks(tmp_path: Path) -> None:
    path = tmp_path / "notes.md"
    release.write_notes_file(path, "v0.2.0: Big", "Thanks to **@alice** ...")
    ack = "Thanks to **@alice** for contributing:\n- x"
    assert release.load_approved_notes(path, "0.2.0", ack)[0] == "v0.2.0: Big"
    with pytest.raises(release.NotesFileError, match="titled"):
        release.load_approved_notes(path, "0.2.1", ack)
    release.write_notes_file(path, "v0.2.0: Big", "no credits here")
    with pytest.raises(release.NotesFileError, match="credits"):
        release.load_approved_notes(path, "0.2.0", ack)


def test_release_body_has_the_install_lines() -> None:
    body = release.build_release_body("## Highlights\n\nNew.", "", "0.2.0")
    assert "uv tool install kampung-mcp==0.2.0" in body
    assert "KAMPUNG_IMAGE_TAG=0.2.0" in body


def test_parse_title() -> None:
    assert release.parse_title("TITLE: v1.0.0: Hi\nbody", "v1.0.0") == (
        "v1.0.0: Hi",
        "body",
    )
    assert release.parse_title("no title", "v1.0.0") == ("v1.0.0", "no title")


@pytest.mark.parametrize(
    "current,wanted,ok",
    [
        ("0.1.0", "0.1.0", True),  # the first release ships the version as is
        ("0.1.0", "0.2.0", True),
        ("0.1.0", "1.0.0", True),
        ("0.2.0", "0.1.0", False),  # never backwards
        ("0.1.0", "0.1", False),
        ("0.1.0", "v0.1.0", False),
    ],
)
def test_explicit_version(current: str, wanted: str, ok: bool) -> None:
    if ok:
        assert release.explicit_version(current, wanted, set()) == wanted
    else:
        with pytest.raises(ValueError):
            release.explicit_version(current, wanted, set())


def test_explicit_version_refuses_an_existing_tag() -> None:
    with pytest.raises(ValueError, match="already"):
        release.explicit_version("0.1.0", "0.1.0", {"v0.1.0"})
